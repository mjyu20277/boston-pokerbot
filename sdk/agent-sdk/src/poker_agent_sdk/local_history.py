"""Stream participant downloads into a temporary index; only one table is loaded at a time."""

import gzip
import json
import math
import re
import tempfile
import zlib
from pathlib import Path
from typing import Any, BinaryIO, Self

MAX_RECORD = 64 * 1024 * 1024
CARD = re.compile(r"[2-9TJQKA][cdhs]\Z")
HISTORY_HINT = (
    "Use the saved hand-history file from poker-agent play or your participant download; "
    "the summary JSON cannot be replayed."
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def integer(value: Any, *, minimum: int = 0) -> bool:
    return type(value) is int and minimum <= value <= 2**53 - 1


def cards(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= 52
        and all(isinstance(card, str) and CARD.fullmatch(card) for card in value)
    )


def read_record(
    source: BinaryIO | gzip.GzipFile, *, header: bool = False
) -> tuple[dict[str, Any], bytes] | None:
    line = source.readline(MAX_RECORD + 1)
    if not line:
        return None
    require(len(line) <= MAX_RECORD, "A history record exceeds the 64 MiB limit.")
    try:
        record = json.loads(line)
    except (ValueError, UnicodeError, RecursionError) as error:
        hint = f" {HISTORY_HINT}" if header else ""
        raise ValueError(f"Invalid JSON in hand history.{hint}") from error
    require(isinstance(record, dict), "Expected a JSON object in hand history.")
    return record, line


def validate_private_events(
    own: dict[str, Any], hands: list[dict[str, Any]], numbers: dict[int, dict[str, Any]]
) -> None:
    """Optional owner-only choices, attached to the public moment when the owner learned them."""
    private = own.get("private_events", [])
    require(isinstance(private, list), "Invalid private event list.")
    dealt = {hand["hand"]: own["cards"][i] for i, hand in enumerate(hands)}
    seen: set[tuple[int, int, str, tuple[str, ...]]] = set()
    choices: dict[tuple[int, int], dict[str, set[str]]] = {}
    card_kinds: set[str] = set()
    amount_kinds: set[str] = set()
    card_kinds.add("bid")
    amount_kinds.update(("bid", "flop_chips", "flop_chips_paid"))
    for event in private:
        require(isinstance(event, dict), "Invalid private event.")
        kind = event.get("kind")
        require(
            isinstance(kind, str) and kind in card_kinds | amount_kinds,
            "Unknown private event kind.",
        )
        required = {"hand", "event_index", "kind"}
        if kind in card_kinds:
            required.add("cards")
        if kind in amount_kinds:
            required.add("amount")
        require(set(event) == required, "Invalid private event fields.")
        number, index = event["hand"], event["event_index"]
        require(integer(number) and number in numbers, "Unknown private event hand.")
        events = numbers[number]["events"]
        require(integer(index, minimum=-1) and index < len(events), "Invalid private event index.")
        if "amount" in required:
            require(integer(event["amount"]), "Invalid private event amount.")
        if "cards" in required:
            require(cards(event["cards"]) and bool(event["cards"]), "Invalid private event cards.")
            require(len(set(event["cards"])) == len(event["cards"]), "Duplicate private card.")
        identity: tuple[int, int, str, tuple[str, ...]] = (number, index, kind, ())
        if kind == "bid":
            identity = (number, index, kind, tuple(event["cards"]))
        require(identity not in seen, "Duplicate private event.")
        seen.add(identity)
        public = events[index] if index >= 0 else None
        if kind == "bid":
            require(
                public is not None
                and public["kind"] == "bid"
                and public.get("seat") == own["seat"],
                "Private bid does not match your public action.",
            )
            offered = {
                card
                for e in events[:index]
                if e["kind"] == "auction"
                for card in e.get("cards", [])
            }
            require(
                len(event["cards"]) == 1 and event["cards"][0] in offered,
                "Private bid names an unavailable card.",
            )
        elif kind in {"flop_chips", "flop_chips_paid"}:
            initial = kind == "flop_chips" and index == -1
            last_bid = max((i for i, e in enumerate(events) if e["kind"] == "bid"), default=-1)
            flop = next(
                (i for i, e in enumerate(events) if i > last_bid and e["kind"] == "board"), -1
            )
            require(
                initial
                or index == flop
                and last_bid >= 0
                and index >= 0
                and any(
                    e["kind"] == "bid" and e.get("seat") == own["seat"] for e in events[: index + 1]
                ),
                "Private balance does not match your auction.",
            )


def validate_table(header: dict[str, Any], table: dict[str, Any], own: dict[str, Any]) -> None:
    board_limit = 1
    try:
        if "variant" in table:
            require(isinstance(table["variant"], str), "Invalid table variant.")
        seats = table["seats"]
        require(isinstance(seats, list) and 2 <= len(seats) <= 10, "Invalid table seats.")
        names = {entry["entry_id"] for entry in header["entries"]}
        require(all(isinstance(s, str) and s in names for s in seats), "Unknown table entry.")
        require(len(set(seats)) == len(seats), "Duplicate table entry.")
        count = len(seats)
        require(integer(table["iteration"]) and integer(table["table"]), "Invalid table identity.")
        require(
            (own["iteration"], own["table"]) == (table["iteration"], table["table"]),
            "Public and private records describe different tables.",
        )
        require(
            integer(own["seat"]) and own["seat"] < count and seats[own["seat"]] == header["you"],
            "Private seat does not match your entry.",
        )
        require(
            isinstance(table["fillers"], list)
            and len(table["fillers"]) == count
            and all(type(f) is bool for f in table["fillers"]),
            "Invalid filler flags.",
        )
        require(
            isinstance(table["payouts"], list) and all(integer(p) for p in table["payouts"]),
            "Invalid payouts.",
        )
        hands = table["hands"]
        require(isinstance(hands, list) and 0 < len(hands) <= 10000, "Invalid hand list.")
        require(
            isinstance(own["cards"], list)
            and len(own["cards"]) == len(hands)
            and all(cards(c) for c in own["cards"]),
            "Private cards do not align with the hands.",
        )
        numbers: dict[int, dict[str, Any]] = {}
        for hand in hands:
            require(isinstance(hand, dict), "Invalid hand.")
            require(integer(hand["hand"], minimum=1), "Invalid hand number.")
            require(hand["hand"] not in numbers, "Duplicate hand number.")
            numbers[hand["hand"]] = hand
            require(integer(hand["button"]) and hand["button"] < count, "Invalid button.")
            require(
                isinstance(hand["blinds"], list)
                and 2 <= len(hand["blinds"]) <= 3
                and all(integer(b) for b in hand["blinds"]),
                "Invalid blinds.",
            )
            for key in ("stacks_before", "stacks_after", "equity_before", "equity_after"):
                values = hand[key]
                require(isinstance(values, list) and len(values) == count, f"Invalid {key}.")
                require(
                    all(
                        integer(v)
                        if key.startswith("stacks")
                        else type(v) in (int, float) and math.isfinite(v) and v >= 0
                        for v in values
                    ),
                    f"Invalid {key}.",
                )
            require(
                sum(hand["stacks_before"]) == sum(hand["stacks_after"]),
                "Hand stacks do not conserve chips.",
            )
            for key in ("flop_chips_before", "flop_chips_after"):
                values = hand.get(key, [0] * count)  # Older downloads have no balances.
                require(
                    isinstance(values, list)
                    and len(values) == count
                    and all(integer(v) for v in values),
                    f"Invalid {key}.",
                )
            require(
                isinstance(hand["events"], list) and len(hand["events"]) <= 10000,
                "Invalid events.",
            )
            for event in hand["events"]:
                require(
                    isinstance(event, dict) and isinstance(event["kind"], str), "Invalid event."
                )
                seat = event.get("seat")
                require(seat is None or integer(seat) and seat < count, "Invalid event seat.")
                if event["kind"] in {
                    "ante",
                    "blind",
                    "call",
                    "raise",
                    "fold",
                    "check",
                    "muck",
                    "show",
                    "win",
                    "stack",
                }:
                    require(seat is not None, "Missing event seat.")
                require(integer(event.get("amount", 0)), "Invalid event amount.")
                require(cards(event.get("cards", [])), "Invalid event cards.")
                if event["kind"] == "board":
                    require(
                        event.get("amount", 0) <= table.get("board_count", board_limit),
                        "Board event contradicts the table's board count.",
                    )
        require(isinstance(own["automatic"], list), "Invalid automatic action list.")
        for reference in own["automatic"]:
            require(
                isinstance(reference, list)
                and len(reference) == 2
                and all(integer(n) for n in reference),
                "Invalid automatic action reference.",
            )
            number, index = reference
            require(
                number in numbers
                and index < len(numbers[number]["events"])
                and numbers[number]["events"][index].get("seat") == own["seat"],
                "Automatic action does not belong to your seat.",
            )
        validate_private_events(own, hands, numbers)
    except (KeyError, TypeError, IndexError, OverflowError) as error:
        raise ValueError("Malformed participant hand history.") from error


class HistoryIndex:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = tempfile.TemporaryFile()
        self.offsets: list[int] = []
        self.tables: list[dict[str, Any]] = []
        try:
            self._load(path)
        except EOFError as error:
            self.data.close()
            raise ValueError("The gzip history is truncated; download it again.") from error
        except (gzip.BadGzipFile, zlib.error) as error:
            self.data.close()
            raise ValueError("The gzip history is corrupt; download it again.") from error
        except BaseException:
            self.data.close()
            raise

    def _load(self, path: Path) -> None:
        with path.open("rb") as raw:
            compressed = raw.read(2) == b"\x1f\x8b"
            raw.seek(0)
            with gzip.GzipFile(fileobj=raw) if compressed else path.open("rb") as source:
                first = read_record(source, header=True)
                require(first is not None, "Missing hand-history header.")
                assert first is not None
                self.header = first[0]
                require(
                    isinstance(self.header.get("tournament_id"), str)
                    and isinstance(self.header.get("you"), str)
                    and isinstance(self.header.get("entries"), list),
                    HISTORY_HINT,
                )
                entries = self.header["entries"]
                require(
                    all(
                        isinstance(e, dict)
                        and isinstance(e.get("entry_id"), str)
                        and isinstance(e.get("team_name"), str)
                        for e in entries
                    ),
                    "Invalid entries in history header.",
                )
                names = {e["entry_id"]: e["team_name"] for e in entries}
                require(len(names) == len(entries), "Duplicate entry in history header.")
                require(self.header["you"] in names, "Your entry is missing from the header.")
                seen = set()
                while (public := read_record(source)) is not None:
                    private = read_record(source)
                    require(private is not None, "Missing private record after table.")
                    assert private is not None
                    own = private[0].get("you")
                    require(isinstance(own, dict), "Missing private seat record.")
                    assert isinstance(own, dict)
                    table = public[0]
                    validate_table(self.header, table, own)
                    key = (table["iteration"], table["table"])
                    require(key not in seen, "Duplicate table in history.")
                    seen.add(key)
                    self.offsets.append(self.data.tell())
                    self.data.write(public[1].rstrip(b"\r\n") + b"\n")
                    self.data.write(private[1].rstrip(b"\r\n") + b"\n")
                    self.tables.append(
                        {
                            "id": len(self.offsets) - 1,
                            "iteration": key[0],
                            "table": key[1],
                            "hands": len(table["hands"]),
                            "filler": table["fillers"][own["seat"]],
                        }
                    )
                self.tables.sort(key=lambda t: (t["iteration"], t["table"]))

    def table(self, index: int) -> tuple[dict[str, Any], dict[str, Any]]:
        if not 0 <= index < len(self.offsets):
            raise IndexError("No such table.")
        self.data.seek(self.offsets[index])
        return json.loads(self.data.readline()), json.loads(self.data.readline())["you"]

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.data.close()
