"""Participant-visible frames for the shared RedZone picture, without organizer-only data."""

from copy import deepcopy
from typing import Any

from poker_agent_sdk.narration import narrate

SIMULTANEOUS = {"show": "showdown", "muck": "showdown"}
GROUPED = {
    "showdown": ("Showdown", 2200),
}


def replay_hand(
    header: dict[str, Any], table: dict[str, Any], own: dict[str, Any], index: int
) -> dict[str, Any]:
    hand = table["hands"][index]
    count, hero = len(table["seats"]), own["seat"]
    # Original cards only. The final public `cards` field would reveal future showdowns.
    dealt = own["cards"][index]
    holes = len(dealt) or max(map(len, own["cards"]), default=2) or 2
    stacks = list(hand["stacks_before"])
    cards = [
        list(dealt) if s == hero else ["??"] * holes if stacks[s] else [] for s in range(count)
    ]
    exposed: list[list[str]] = [[] for _ in stacks]
    statuses = ["playing" if chips else "out" for chips in stacks]
    events = hand["events"]
    # Layout is a table rule, never a signal that this particular hand reaches the flop.
    # Older downloads have no metadata; use one consistent layout for the entire table.
    board_count = 1
    boards: list[list[str]] = [[] for _ in range(board_count)]
    bets, antes, pot = [0] * count, [0] * count, 0
    frames: list[dict[str, Any]] = []
    warnings: list[str] = []
    automatic = {tuple(ref) for ref in own["automatic"]}
    pending: list[dict[str, Any]] = []
    spread: dict[str, Any] | None = None
    private: dict[int, list[dict[str, Any]]] = {}
    for event in own.get("private_events", []):
        if event["hand"] == hand["hand"]:
            private.setdefault(event["event_index"], []).append(event)
    balance: int | None = next(
        (e["amount"] for e in private.get(-1, []) if e["kind"] == "flop_chips"), None
    )

    def collect(contributions: list[int]) -> tuple[int, int]:
        """PokerKit trims an unmatched ante separately from unmatched street wagers."""
        nonlocal pot
        leader = max(range(count), key=contributions.__getitem__)
        refund = contributions[leader] - sorted(contributions)[-2]
        if refund:
            stacks[leader] += refund
            pot -= refund
            if statuses[leader] == "all-in":
                statuses[leader] = "playing"
        contributions[:] = [0] * count
        return leader, refund

    def snapshot(event_index: int, label: str, actor: int | None, duration: int = 1100) -> None:
        frames.append(
            deepcopy(
                {
                    "event_index": event_index,
                    "label": label,
                    "say": narrate(label, blinds=(hand["blinds"][0], hand["blinds"][1])),
                    "pending": pending,
                    "spread": spread,
                    "actor": actor,
                    "boards": boards,
                    "cards": cards,
                    "exposed": exposed,
                    "stacks": stacks,
                    "bets": bets,
                    "statuses": statuses,
                    "pot": pot,
                    "equities": None,
                    "odds_note": "Only your cards and public information are available.",
                    "duration_ms": duration,
                }
            )
        )

    snapshot(-1, "Cards dealt", None, 1700)
    if balance is not None:
        snapshot(-1, f"Starts with {balance:,} flop chips", hero)
    visible = [
        (i, event) for i, event in enumerate(events) if not event["kind"].startswith("prize_")
    ]
    collect_kinds = {"board", "show", "muck", "win"}
    collect_kinds.update(("auction", "bid"))
    grouped, fallback = 0, False
    for position, (i, event) in enumerate(visible):
        kind, seat, amount = event["kind"], event.get("seat"), event.get("amount", 0)
        if kind == "stack":
            continue
        if kind != "ante" and any(antes):
            leader, refund = collect(antes)
            if refund:
                snapshot(i - 1, f"Has {refund:,} unmatched ante chips returned", leader)
        if kind in collect_kinds:
            leader, refund = collect(bets)
            if refund:
                snapshot(i - 1, f"Has {refund:,} uncalled chips returned", leader)
            if kind == "win":
                continue
        label, duration = kind.replace("_", " ").capitalize(), 1100
        if kind in {"ante", "blind", "call", "raise"}:
            assert seat is not None
            added = amount - bets[seat] if kind == "raise" else amount
            if not 0 <= added <= stacks[seat]:
                raise ValueError("Logged contribution is inconsistent with stacks.")
            stacks[seat] -= added
            pot += added
            (antes if kind == "ante" else bets)[seat] += added
            label = f"{kind.capitalize()} {amount:,}"
            if not stacks[seat]:
                statuses[seat] = "all-in"
                label, duration = f"All-in · {kind} {amount:,}", 2300
        elif kind == "board":
            board = max(0, amount - 1)
            boards[board].extend(event.get("cards", []))
            street = {3: "Flop", 4: "Turn", 5: "River"}.get(len(boards[board]), "Board")
            label = street + (f" · board {board + 1}" if len(boards) > 1 else "")
            spread = None
            duration = 2500
        elif kind in {"fold", "muck"}:
            statuses[seat] = "folded" if kind == "fold" else "mucked"
            duration = 650
        elif kind == "show":
            cards[seat] = list(event.get("cards", []))
            exposed[seat] = list(cards[seat])
            label, duration = "Shows", 1500
        elif kind == "auction":
            label = "Auction options: " + " ".join(event.get("cards", []))
            spread = {
                "caption": "Auction cards · bids choose 3",
                "cards": list(event.get("cards", [])),
            }
        elif kind == "bid":
            label = "Sealed bid submitted"
            if seat == hero and balance is not None:
                bids = [e for e in private.get(i, []) if e["kind"] == "bid"]
                label = (
                    "Bids "
                    + (" · ".join(f"{e['amount']:,} on {e['cards'][0]}" for e in bids) or "0")
                    + " flop chips"
                )
        paid: int | None = None
        for change in private.get(i, []):
            pass  # A kit without private changes has no handlers below.
            if change["kind"] == "flop_chips":
                balance = change["amount"]
            elif change["kind"] == "flop_chips_paid":
                paid = change["amount"]
        following = visible[position + 1][1]["kind"] if position + 1 < len(visible) else None
        # Apply public and private changes first, then present each joint reveal in one frame.
        fallback = fallback or (hand["hand"], i) in automatic
        group = SIMULTANEOUS.get(kind)
        if group is not None and SIMULTANEOUS.get(following or "") == group:
            grouped += 1
            continue
        if group is not None and grouped:
            (label, duration), seat = GROUPED[group], None
        if fallback:
            label += " · automatic fallback"
        grouped, fallback = 0, False
        snapshot(i, label, seat, duration)
        if paid is not None:
            snapshot(i, f"Pays {paid:,} flop chips · {balance:,} remaining", hero)
    stacks, bets, pot = list(hand["stacks_after"]), [0] * count, 0
    for event in events:
        if event["kind"] == "win":
            statuses[event["seat"]] = "winner"
    # Engine settlement includes refunds, side pots, ties and odd chips. Never reconstruct it.
    snapshot(len(events), "Pot awarded", None, 3500)
    names = {entry["entry_id"]: entry["team_name"] for entry in header["entries"]}
    return {
        "id": f"{table['iteration']}:{table['table']}:{hand['hand']}",
        "hand_number": hand["hand"],
        "iteration": table["iteration"],
        "table": table["table"],
        "variant": table.get("variant", "recorded game"),
        "button": hand["button"],
        "blinds": hand["blinds"],
        "seats": [
            {
                "entry_id": entry,
                "name": names[entry] + (" (you)" if seat == hero else ""),
                "filler": table["fillers"][seat],
            }
            for seat, entry in enumerate(table["seats"])
        ],
        "frames": frames,
        "warnings": warnings,
    }
