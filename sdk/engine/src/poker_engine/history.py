"""A table's hand log as its players saw it, for the hand histories teams download.

A table's log holds every seat's cards. Its public view keeps everything any player at the
table saw (every action, the board, and cards shown at showdown) and drops the rest: other
seats' unshown cards, and which actions the table played for a failing bot. Each seat then
gets a short private line with its own cards, automatic actions, and timed private choices.

The public view is the same for every seat, so it is encoded once and shared; only the
private lines differ.
"""

from collections.abc import Mapping
from typing import Any

# Public exports opt in to fields so future organizer metadata cannot leak by default.
PUBLIC_TABLE_FIELDS = {
    "variant",
    "board_count",
    "iteration",
    "table",
    "seats",
    "fillers",
    "payouts",
}
# `cards` and `events` are listed so they keep their place in the record (they are replaced
# below); a team's file therefore reads exactly as before the allowlist.
PUBLIC_HAND_FIELDS = {
    "hand",
    "button",
    "blinds",
    "stacks_before",
    "stacks_after",
    "cards",
    "events",
    "equity_before",
    "equity_after",
}
# Every seat's balance is public afterwards; a balance shows what a seat paid, never its bids.
PUBLIC_HAND_FIELDS.update(("flop_chips_before", "flop_chips_after"))
PUBLIC_EVENT_FIELDS = {"kind", "seat", "amount", "cards"}
SHOWN_KINDS = {"show"}


def public_view(table: Mapping[str, Any]) -> dict[str, Any]:
    """The table line (`iteration`, `table`, `seats`, ..., `hands`) without private detail."""
    view = {key: value for key, value in table.items() if key in PUBLIC_TABLE_FIELDS}
    # Only multi-board games name a board count; a single board is the default.
    if view.get("board_count") == 1:
        del view["board_count"]
    view["hands"] = []
    for record in table["hands"]:
        shown: dict[int, list[str]] = {
            event["seat"]: event["cards"]
            for event in record["events"]
            if event["kind"] in SHOWN_KINDS
        }
        # Private metadata contains every player's private choices. The public line must
        # omit it; private_view separately allowlists only each owner's known choices.
        hand = {key: value for key, value in record.items() if key in PUBLIC_HAND_FIELDS}
        hand["cards"] = [shown.get(seat, []) for seat in range(len(record["cards"]))]
        hand["events"] = [
            {key: value for key, value in event.items() if key in PUBLIC_EVENT_FIELDS}
            for event in record["events"]
        ]
        view["hands"].append(hand)
    return view


def private_view(table: Mapping[str, Any], seat: int) -> dict[str, Any]:
    """This seat's cards, fallbacks and optional timed choices, following the public line."""
    automatic = [
        [record["hand"], index]
        for record in table["hands"]
        for index, event in enumerate(record["events"])
        if event.get("automatic") and event.get("seat") == seat
    ]
    private_events = [event for record in table["hands"] for event in _private_events(record, seat)]
    return {
        "you": {
            "iteration": table["iteration"],
            "table": table["table"],
            "seat": seat,
            "cards": [record["cards"][seat] for record in table["hands"]],
            "automatic": automatic,
            **({"private_events": private_events} if private_events else {}),
        }
    }


def _private_events(record: Mapping[str, Any], seat: int) -> list[dict[str, Any]]:
    """Export only the owner's known choices, at the public event where they take effect.

    This is deliberately constructed field by field. Other seats' private information, prize
    counters and future organizer-only fields never pass through the participant export.
    Missing private data is an older log, not an invitation to infer private cards from a show.
    """
    events = record["events"]
    private: list[Any] = []
    private = [event for event in record.get("sealed", []) if event.get("seat") == seat]
    kinds: list[str] = []
    kinds.append("bid")
    own = {
        kind: next(
            (i for i, e in enumerate(events) if e["kind"] == kind and e.get("seat") == seat), None
        )
        for kind in kinds
    }
    result: list[dict[str, Any]] = []

    def add(kind: str, index: int, **fields: Any) -> None:
        result.append({"hand": record["hand"], "event_index": index, "kind": kind, **fields})

    balance = next(
        (event.get("amount", 0) for event in private if event["kind"] == "flop_chips"), None
    )
    paid = sum(event.get("amount", 0) for event in private if event["kind"] == "flop_chips_paid")
    if balance is not None:
        add("flop_chips", -1, amount=balance + paid)
    if own["bid"] is not None:
        for event in private:
            if event["kind"] == "bid":
                add("bid", own["bid"], cards=list(event["cards"]), amount=event.get("amount", 0))
        if balance is not None:
            last_bid = max(i for i, event in enumerate(events) if event["kind"] == "bid")
            flop = next(
                (i for i, event in enumerate(events) if i > last_bid and event["kind"] == "board"),
                None,
            )
            # A payment identifies successful bids, so reveal it with the chosen flop.
            if flop is not None:
                add("flop_chips_paid", flop, amount=paid)
                add("flop_chips", flop, amount=balance)
    return sorted(result, key=lambda event: event["event_index"])
