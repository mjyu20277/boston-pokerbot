"""Points for one finished table: place payouts for busted bots, an ICM split at the hand cap."""

from collections.abc import Sequence
from typing import TYPE_CHECKING

from poker_agent_protocol.icm import icm

__all__ = ["bust_groups", "equity", "icm", "table_places", "table_points"]

if TYPE_CHECKING:  # The table logs equity each hand, so it imports this module.
    from poker_engine.table import TableResult


def bust_groups(bust_order: Sequence[int], ties: Sequence[int] = ()) -> list[list[int]]:
    """Indexes into `bust_order`, grouped so that seats that tied share one group."""
    groups: list[list[int]] = []
    for index in range(len(bust_order)):
        if index in ties and groups:
            groups[-1].append(index)
        else:
            groups.append([index])
    return groups


def equity(
    stacks: Sequence[int],
    bust_order: Sequence[int],
    payouts: Sequence[int],
    ties: Sequence[int] = (),
) -> list[float]:
    """Each seat's points if the table stopped now: busted seats keep their place's payout,
    seats that tied split their places' payouts evenly, and the seats with chips split the
    rest by ICM (which gives equal stacks equal points)."""
    seats = len(stacks)
    points = [0.0] * seats
    for group in bust_groups(bust_order, ties):
        # Zero-based places: the first bust takes last place.
        places = [seats - 1 - index for index in group]
        share = sum(float(payouts[place]) if place < len(payouts) else 0.0 for place in places)
        for index in group:
            points[bust_order[index]] = share / len(group)
    survivors = [seat for seat in range(seats) if stacks[seat] > 0]
    remaining = list(payouts[: len(survivors)])
    shares = icm([stacks[seat] for seat in survivors], remaining)
    for seat, value in zip(survivors, shares, strict=True):
        points[seat] = value
    return points


def table_points(result: "TableResult", payouts: Sequence[int]) -> list[float]:
    """Points per seat: the equity of the finished table."""
    return equity(result.final_stacks, result.bust_order, payouts, result.ties)


def table_places(result: "TableResult") -> list[int]:
    """Finishing place per seat, 1 = first. Busted seats place by bust order and survivors at
    the hand cap by chips; seats that tied (same hand and stack, or equal chips at the cap)
    share the better place, as in "T5"."""
    seats = len(result.final_stacks)
    places = [0] * seats
    for group in bust_groups(result.bust_order, result.ties):
        for index in group:
            places[result.bust_order[index]] = seats - group[-1]
    final = result.final_stacks
    for seat in range(seats):
        if final[seat] > 0:
            places[seat] = 1 + sum(other > final[seat] for other in final)
    return places
