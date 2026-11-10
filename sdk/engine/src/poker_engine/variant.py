"""The interface a poker variant implements. The table handles everything between hands.

Players in a hand are numbered by position, with the button last. A variant only sees the
players who still have chips, and must take every random choice from the `rng` it is given.

Variants built into the engine are the modules of poker_engine.variants, named by ID, each
with its rules for teams beside it as <ID>.md. A variant may also live in a separate package
that registers itself, with its rules as a `rules` attribute:

    [project.entry-points."poker_engine.variants"]
    my_variant = "my_package:MyVariant"

A variant that needs more from the table than dealing hands defines `extension`: a callable
taking the table's player count and returning a `TableExtension`, made once per table.

An optional `board_count` describes the community-card layout for exported replays; it
defaults to one and does not change how a hand is played.
"""

import pkgutil
import random
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from importlib import import_module, resources
from importlib.metadata import entry_points
from typing import Any, Protocol

from poker_agent_protocol.game import Action, BlindLevel, LegalAction


@dataclass(frozen=True, slots=True)
class HandView:
    """What one player may see of the hand right now."""

    cards: tuple[str, ...]  # The player's own private cards.
    board: tuple[str, ...]
    pot: int  # All chips committed this hand, including current bets.
    to_call: int
    stacks: tuple[int, ...]  # Per position.
    bets: tuple[int, ...]  # Per position, current betting round.
    folded: tuple[bool, ...]
    shown: tuple[tuple[str, ...], ...]  # Face-up cards per position.
    variant: dict[str, Any] = field(default_factory=dict)
    phase: str = "betting"


@dataclass(frozen=True, slots=True)
class HandEvent:
    kind: str
    player: int | None = None
    amount: int = 0
    cards: tuple[str, ...] = ()


class Hand(Protocol):
    def actor(self) -> int | None:
        """Deal as needed and return the position to act, or None once the hand is over."""
        ...

    def legal_actions(self) -> tuple[LegalAction, ...]:
        """Allowed actions. Without check/fold, the first action is the failure fallback."""
        ...

    def apply(self, action: Action) -> None:
        """Apply the actor's action. Raise ValueError, without changing state, if it is illegal.
        Its event should name the actor and the action's kind; the hand log finds it that way
        to mark actions played for a failed bot."""
        ...

    def view(self, player: int) -> HandView: ...

    def events(self) -> tuple[HandEvent, ...]:
        """Every public event of this hand so far, oldest first and never rewritten. Never
        include hidden cards."""
        ...

    def dealt(self) -> tuple[tuple[str, ...], ...]:
        """Every private card dealt to each position so far, for the hand log. Never shown to
        a bot, and kept even after the cards are mucked."""
        ...

    def stacks(self) -> tuple[int, ...]: ...


class TableExtension(Protocol):
    """A variant's own work around its hands at one table. `seats` maps each hand position to
    its table seat."""

    def deal_options(self, seats: tuple[int, ...]) -> dict[str, Any]:
        """Keyword arguments added to this hand's `deal`."""
        ...

    def hand_over(self, hand: Hand, seats: tuple[int, ...]) -> dict[str, Any]:
        """Called once the hand is over. Returns fields added to its organizer log record."""
        ...


class Variant(Protocol):
    name: str  # The ID competitions and table rules refer to.
    title: (
        str  # Shown to organizers and teams: a regional variant's location, nothing of its rules.
    )
    max_players: int

    def deal(self, *, stacks: tuple[int, ...], blinds: BlindLevel, rng: random.Random) -> Hand: ...


# The public reference variant, and the table rules' default.
DEFAULT_VARIANT = "no_limit_holdem"


def installed_variants() -> list[str]:
    """Built-in variant IDs, then those registered by other packages."""
    from poker_engine import variants

    modules = pkgutil.iter_modules(variants.__path__)
    # Modules starting with an underscore are code shared by variants, not variants.
    built_in = sorted(module.name for module in modules if not module.name.startswith("_"))
    others = sorted(entry.name for entry in entry_points(group="poker_engine.variants"))
    return built_in + [name for name in others if name not in built_in]


def load_variant(name: str) -> Variant:
    if re.fullmatch(r"[a-z0-9][a-z0-9_]{0,63}", name):
        try:
            module = import_module(f"poker_engine.variants.{name}")
        except ModuleNotFoundError as error:
            if error.name != f"poker_engine.variants.{name}":
                raise
        else:
            if hasattr(module, "VARIANT"):
                built_in: Variant = module.VARIANT()
                return built_in
    for entry in entry_points(group="poker_engine.variants", name=name):
        variant: Variant = entry.load()()
        return variant
    raise KeyError(f"No installed poker variant is named {name!r}.")


def public_id(name: str) -> str:
    """The ID teams and organizers see for a variant: its `public_id` when it declares one,
    else its name. A regional variant names its location, so nothing the API returns, before
    or after its rules are announced, describes them."""
    return str(getattr(load_variant(name), "public_id", name))


def resolve_variant(value: str, names: Sequence[str]) -> str | None:
    """The installed variant `value` names, by its name or its public ID; None for neither."""
    for name in names:
        if value in (name, public_id(name)):
            return name
    return None


def variant_rules(name: str) -> str:
    """Markdown for teams describing the variant."""
    rules = resources.files("poker_engine.variants").joinpath(f"{name}.md")
    if re.fullmatch(r"[a-z0-9_]{1,64}", name) and rules.is_file():
        return str(rules.read_text())
    return str(getattr(load_variant(name), "rules", ""))
