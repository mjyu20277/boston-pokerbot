"""Stream a participant hand history, including concatenated gzip members. Standard library only.

From the kit root: python hand-history/read.py [path/to/hands.jsonl.gz]
"""

import argparse
import gzip
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any


def read_history(path: Path) -> Iterator[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
    """Yield (header, public table, your private data), keeping one table in memory."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as source:
        lines = iter(source)
        try:
            header = json.loads(next(lines))
        except StopIteration as error:
            raise ValueError("Missing hand-history header.") from error
        if (
            not isinstance(header, dict)
            or not isinstance(header.get("tournament_id"), str)
            or not isinstance(header.get("you"), str)
            or not isinstance(header.get("entries"), list)
        ):
            raise ValueError("Invalid hand-history header.")
        if any(
            not isinstance(entry, dict)
            or not isinstance(entry.get("entry_id"), str)
            or not isinstance(entry.get("team_name"), str)
            for entry in header["entries"]
        ):
            raise ValueError("Invalid entry in hand-history header.")
        entries = {entry["entry_id"] for entry in header["entries"]}
        if header["you"] not in entries:
            raise ValueError("Your entry is missing from the header.")
        for line in lines:
            table = json.loads(line)
            try:
                own = json.loads(next(lines))["you"]
            except StopIteration as error:
                raise ValueError("Missing private line after public table.") from error
            if (own["iteration"], own["table"]) != (table["iteration"], table["table"]):
                raise ValueError("Public and private lines describe different tables.")
            seat = own["seat"]
            if (
                type(seat) is not int
                or not 0 <= seat < len(table["seats"])
                or table["seats"][seat] != header["you"]
            ):
                raise ValueError("Private seat does not match your entry.")
            if (
                not isinstance(table["fillers"], list)
                or len(table["fillers"]) != len(table["seats"])
                or any(type(filler) is not bool for filler in table["fillers"])
                or any(entry not in entries for entry in table["seats"])
            ):
                raise ValueError("Invalid table seats or filler flags.")
            if (
                not isinstance(own["cards"], list)
                or len(own["cards"]) != len(table["hands"])
                or any(
                    not isinstance(cards, list) or any(not isinstance(c, str) for c in cards)
                    for cards in own["cards"]
                )
            ):
                raise ValueError("Own cards do not align with the table's hands.")
            hands = {hand["hand"]: hand for hand in table["hands"]}
            if len(hands) != len(table["hands"]):
                raise ValueError("Duplicate hand number in table.")
            if not isinstance(own["automatic"], list):
                raise ValueError("Invalid automatic action references.")
            for reference in own["automatic"]:
                if (
                    not isinstance(reference, list)
                    or len(reference) != 2
                    or any(type(index) is not int for index in reference)
                ):
                    raise ValueError("Invalid automatic action reference.")
                hand_number, index = reference
                hand = hands.get(hand_number)
                if (
                    hand is None
                    or not 0 <= index < len(hand["events"])
                    or hand["events"][index].get("seat") != seat
                ):
                    raise ValueError("Automatic action does not refer to your seat's event.")
            yield header, table, own


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path", nargs="?", type=Path, default=Path(__file__).with_name("sample.jsonl.gz")
    )
    arguments = parser.parse_args()
    for header, table, own in read_history(arguments.path):
        teams = {entry["entry_id"]: entry["team_name"] for entry in header["entries"]}
        print(
            json.dumps(
                {
                    "iteration": table["iteration"],
                    "table": table["table"],
                    "your_seat": own["seat"],
                    "your_seat_is_filler": table["fillers"][own["seat"]],
                    "teams_by_seat": [teams[entry] for entry in table["seats"]],
                    "hands": len(table["hands"]),
                    "your_automatic_actions": len(own["automatic"]),
                }
            )
        )


if __name__ == "__main__":
    main()
