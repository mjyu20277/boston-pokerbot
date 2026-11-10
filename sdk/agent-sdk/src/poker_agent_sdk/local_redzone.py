"""Loopback file viewer for the prebuilt, shared RedZone UI. No external services."""

import argparse
import json
import re
import tempfile
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

from poker_agent_sdk.local_history import HistoryIndex
from poker_agent_sdk.local_replay import replay_hand

ASSETS = Path(__file__).with_name("redzone_assets")
ROUTE = re.compile(r"/api/table/(\d+)(?:/hand/(\d+))?\Z")
MAX_UPLOAD = 1024 * 1024 * 1024


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "history",
        type=Path,
        nargs="?",
        help="History file; omit to choose or drop one in the browser.",
    )
    parser.add_argument(
        "--port", type=int, default=0, help="Local port; default picks a free port."
    )
    parser.add_argument("--no-open", action="store_true", help="Print the URL without opening it.")


def run_from_arguments(arguments: argparse.Namespace) -> None:
    serve(arguments.history, port=arguments.port, open_browser=not arguments.no_open)


def main() -> None:
    parser = argparse.ArgumentParser(description="Watch your poker hands.")
    add_arguments(parser)
    try:
        run_from_arguments(parser.parse_args())
    except (OSError, ValueError, KeyError) as error:
        parser.exit(2, f"{error}\n")


class ReplayServer(ThreadingHTTPServer):
    """Own the current index; replace it only after a new file has fully validated."""

    history: HistoryIndex | None = None
    filename: str | None = None
    revision = 0

    def __init__(
        self, history: HistoryIndex | None, port: int, handler: type[BaseHTTPRequestHandler]
    ):
        self.reading = Lock()
        self.session = uuid4().hex
        super().__init__(("127.0.0.1", port), handler)
        self.history = history
        self.filename = history.path.name if history is not None else None

    def summary(self) -> dict[str, Any]:
        return {
            "session": self.session,
            "filename": self.filename,
            "revision": self.revision,
            "tables": self.history.tables if self.history is not None else [],
        }

    def server_close(self) -> None:
        super().server_close()
        with self.reading:
            if self.history is not None:
                self.history.data.close()
                self.history = None


def make_server(history: HistoryIndex | None = None, port: int = 0) -> ReplayServer:
    class Handler(BaseHTTPRequestHandler):
        server: ReplayServer

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def send_data(self, data: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
                "style-src 'self' 'unsafe-inline'; object-src 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(data)

        def json(self, data: object, status: int = 200) -> None:
            self.send_data(json.dumps(data, allow_nan=False).encode(), "application/json", status)

        def local_request(self, *, allow_navigation: bool = False) -> bool:
            allowed_hosts = {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }
            host = self.headers.get("Host")
            origin = self.headers.get("Origin")
            navigation = (
                allow_navigation
                and self.headers.get("Sec-Fetch-Mode") == "navigate"
                and self.headers.get("Sec-Fetch-Dest") == "document"
            )
            if (
                host not in allowed_hosts
                or origin is not None
                and origin != f"http://{host}"
                or self.headers.get("Sec-Fetch-Site") == "cross-site"
                and not navigation
            ):
                self.json({"error": "Only same-origin localhost requests are allowed."}, 403)
                return False
            return True

        def do_POST(self) -> None:
            # Only a raw browser File is accepted, never a path to read on the host.
            # Browsers cannot submit this content type/header cross-origin without a preflight.
            self.close_connection = True
            if not self.local_request():
                return
            if self.path != "/api/history":
                self.json({"error": "Not found."}, 404)
                return
            if self.headers.get("Content-Type") != "application/octet-stream":
                self.json({"error": "Choose a .jsonl or .jsonl.gz history file."}, 415)
                return
            try:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError as error:
                    raise ValueError("Invalid file length. Choose a history file again.") from error
                if self.headers.get("Transfer-Encoding") or not 0 < length <= MAX_UPLOAD:
                    self.json({"error": "Choose a nonempty history file up to 1 GiB."}, 413)
                    return
                try:
                    filename = unquote(self.headers.get("X-Redzone-Filename", ""), errors="strict")
                except UnicodeError as error:
                    raise ValueError("Invalid filename. Choose a history file again.") from error
                filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
                if not filename or len(filename) > 255 or any(ord(c) < 32 for c in filename):
                    raise ValueError("Invalid filename. Choose a history file again.")
                self.connection.settimeout(30)
                # Read and index on disk. No whole-file buffering, and no saved copy remains.
                with self.server.reading, tempfile.TemporaryDirectory() as directory:
                    source = Path(directory) / "history"
                    with source.open("wb") as output:
                        remaining = length
                        while remaining:
                            chunk = self.rfile.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise ValueError("The file transfer was interrupted. Try again.")
                            output.write(chunk)
                            remaining -= len(chunk)
                    replacement = HistoryIndex(source)
                    previous = self.server.history
                    self.server.history = replacement
                    self.server.filename = filename
                    self.server.revision += 1
                    if previous is not None:
                        previous.data.close()
                    self.json(self.server.summary())
            except (BrokenPipeError, ConnectionResetError):
                pass
            except ValueError as error:
                self.json({"error": f"Could not open this history: {str(error).rstrip('.')}."}, 422)
            except TimeoutError:
                self.json({"error": "The file transfer was interrupted. Try again."}, 422)
            except OSError:
                self.json({"error": "Could not read the file. Please choose it again."}, 422)

        def do_GET(self) -> None:
            # Serialize history reads and replacement so a request sees one complete index.
            with self.server.reading:
                # Following a link to the public shell is safe, including from another site.
                # History APIs and uploads retain the same-origin checks; Host and explicit
                # Origin validation also remain in force for the shell itself.
                if not self.local_request(
                    allow_navigation=self.path in {"/", "/app.js", "/app.css"}
                ):
                    return
                self.respond()

        def respond(self) -> None:
            try:
                request = urlsplit(self.path)
                if request.path == "/api/history":
                    self.json(self.server.summary())
                elif match := ROUTE.fullmatch(request.path):
                    query = parse_qs(request.query, keep_blank_values=True)
                    revision = query.get("revision")
                    session = query.get("session")
                    if (
                        revision is not None
                        and revision != [str(self.server.revision)]
                        or session is not None
                        and session != [self.server.session]
                    ):
                        self.json(
                            {"error": "The loaded file changed. Choose its table again."}, 409
                        )
                        return
                    history = self.server.history
                    if history is None:
                        self.json({"error": "Choose a history file first."}, 404)
                        return
                    table, own = history.table(int(match[1]))
                    if match[2] is None:
                        self.json(
                            {
                                "hands": [h["hand"] for h in table["hands"]],
                                "payouts": table["payouts"],
                                "seat": own["seat"],
                                "variant": table.get("variant", "recorded game"),
                            }
                        )
                    else:
                        index = int(match[2])
                        if not 0 <= index < len(table["hands"]):
                            raise IndexError("No such hand.")
                        self.json(
                            {
                                "round_id": history.header["tournament_id"],
                                "round_weight": None,
                                "simulated": False,
                                "reveal_hidden": False,
                                "hands": [replay_hand(history.header, table, own, index)],
                            }
                        )
                elif self.path in {"/", "/app.js", "/app.css"}:
                    name = "index.html" if self.path == "/" else self.path[1:]
                    content_type = {
                        "index.html": "text/html; charset=utf-8",
                        "app.js": "text/javascript",
                        "app.css": "text/css",
                    }[name]
                    self.send_data((ASSETS / name).read_bytes(), content_type)
                else:
                    self.json({"error": "Not found."}, 404)
            except IndexError:
                self.json({"error": "No such table or hand."}, 404)
            except (ValueError, KeyError, TypeError) as error:
                self.json({"error": f"Cannot replay this hand: {error}"}, 422)
            except (BrokenPipeError, ConnectionResetError):
                pass  # The browser changed hands before the response arrived.

    return ReplayServer(history, port, Handler)


def serve(path: Path | None = None, *, port: int = 0, open_browser: bool = True) -> None:
    if not 0 <= port <= 65535:
        raise ValueError("Port must be between 0 and 65535.")
    history = None
    try:
        if path is not None:
            print(f"Reading {path.name}…", flush=True)
            history = HistoryIndex(path)
        with make_server(history, port) as server:
            url = f"http://127.0.0.1:{server.server_port}"
            print(f"Hand Player: {url}\nPress Ctrl+C to close.", flush=True)
            if open_browser:
                webbrowser.open(url)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if history is not None:
            history.data.close()
