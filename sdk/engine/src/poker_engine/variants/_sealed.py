"""Hand events for the organizers' hand log only. Not a variant: modules whose names start with
an underscore are helpers, shipped only in the kits of the variants that import them."""

from typing import Any

from poker_engine.table import logged
from poker_engine.variant import Hand


class SealedLog:
    """A `TableExtension` logging each hand's `sealed()` events as its record's `sealed` list,
    never shown to a bot or in a team's hand history. `prize_` counters among them count as
    usual."""

    def __init__(self, players: int) -> None:
        pass

    def deal_options(self, seats: tuple[int, ...]) -> dict[str, Any]:
        return {}

    def hand_over(self, hand: Hand, seats: tuple[int, ...]) -> dict[str, Any]:
        events = hand.sealed()  # type: ignore[attr-defined]
        return {"sealed": [logged(event, seats) for event in events]}
