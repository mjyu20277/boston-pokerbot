"""Local exports use the same public/private records as participant tournament downloads."""

import gzip
import json
import threading
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO, Self

from poker_engine.history import private_view, public_view


def encoded(record: dict[str, Any]) -> bytes:
    return (json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n").encode()


class HistoryWriter:
    """One gzip member per table pair, just like the worker. Never overwrite a prior run."""

    def __init__(self, path: Path, *, labels: list[str], hero: int, seed: str) -> None:
        self.path = path
        self.hero = labels[hero]
        self.header = {
            "tournament_id": f"local-{seed}",
            "you": self.hero,
            "entries": [{"entry_id": label, "team_name": label} for label in labels],
        }
        self.lock = threading.Lock()
        self.output: BinaryIO | None = None

    def __enter__(self) -> Self:
        self.output = self.path.open("xb")
        try:
            self.output.write(gzip.compress(encoded(self.header), mtime=0))
        except BaseException:
            self.output.close()
            raise
        return self

    def table(self, raw: dict[str, Any]) -> None:
        seat = raw["seats"].index(self.hero)
        data = gzip.compress(
            encoded(public_view(raw)) + encoded(private_view(raw, seat)), compresslevel=6, mtime=0
        )
        with self.lock:
            assert self.output is not None
            self.output.write(data)

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        with self.lock:
            assert self.output is not None
            self.output.close()
