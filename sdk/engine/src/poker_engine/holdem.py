"""Reference variant: no-limit Texas hold'em through PokerKit."""

import random
from collections import deque
from collections.abc import Iterable, Iterator, Sequence
from functools import cached_property
from itertools import combinations
from math import prod

from poker_agent_protocol.game import Action, BlindLevel, LegalAction
from pokerkit import (
    AntePosting,
    Automation,
    BlindOrStraddlePosting,
    BoardDealing,
    Card,
    CardsLike,
    CheckingOrCalling,
    ChipsPulling,
    CompletionBettingOrRaisingTo,
    Deck,
    Entry,
    Folding,
    Hand,
    HoleCardsShowingOrMucking,
    HoleDealing,
    NoLimitTexasHoldem,
    StandardHighHand,
)

from poker_engine.variant import HandEvent, HandView

AUTOMATIONS = (
    Automation.ANTE_POSTING,
    Automation.BET_COLLECTION,
    Automation.BLIND_OR_STRADDLE_POSTING,
    Automation.HAND_KILLING,
    Automation.CHIPS_PUSHING,
    Automation.CHIPS_PULLING,
)
DECK = tuple(sorted(Deck.STANDARD, key=repr))


class _CachedHighHand(StandardHighHand):
    # PokerKit compares each immutable hand repeatedly while finding and paying winners.
    # Keep its evaluator and ranking; avoid looking up the same hand's entry each time.
    @cached_property
    def entry(self) -> Entry:
        return super().entry

    @classmethod
    def from_game(cls, hole_cards: CardsLike, board_cards: CardsLike = ()) -> Hand:
        """PokerKit's own answer, found with `strength`: of the five-card combinations in
        PokerKit's order, the first that makes the best hand."""
        hole, board = Card.clean(hole_cards), Card.clean(board_cards)  # Once: may be iterators.
        cards = (*hole, *board)
        known = set(cards)
        if not 5 <= len(cards) <= 9 or len(known) < len(cards) or not known <= CARD_VALUES.keys():
            return super().from_game(hole, board)  # Unknown or repeated cards: PokerKit's way.
        best = strength(cards)
        return cls(next(five for five in combinations(cards, 5) if strength(five) == best))


# Each card as (rank, suit): 2-14, aces high, and its suit letter.
CARD_VALUES = {card: ("23456789TJQKA".index(repr(card)[0]) + 2, repr(card)[1]) for card in DECK}


# Each card's rank as a prime, so a product names a multiset of ranks, and its suit as one
# in a four-bit count per suit: a sum of up to nine never carries into the next suit.
_PRIMES = (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41)
_RANK_PRIME = {card: _PRIMES[rank - 2] for card, (rank, _) in CARD_VALUES.items()}
_SUIT_COUNT = {card: 1 << 4 * "cdhs".index(suit) for card, (_, suit) in CARD_VALUES.items()}
# A suit count of five or more: adding three sets that suit's top bit.
_FLUSH_POSSIBLE, _FLUSH_BITS = 0x3333, 0x8888
# Ranks of hands that cannot be flushes, by their rank product, filled as they are first seen:
# at most 74,000 multisets of five to seven cards.
_BY_RANKS: dict[int, tuple[int, ...]] = {}


def runout_strengths(
    hands: Sequence[Sequence[Card]], runouts: Iterable[Sequence[Card]]
) -> Iterator[list[tuple[int, ...]]]:
    """`strength` of each hand plus each runout, runout by runout: the same answers, with each
    hand's part of the lookup worked out once rather than once per runout."""
    fixed = [
        (prod(map(_RANK_PRIME.__getitem__, hand)), sum(map(_SUIT_COUNT.__getitem__, hand)), hand)
        for hand in hands
    ]
    for runout in runouts:
        primes = prod(map(_RANK_PRIME.__getitem__, runout))
        suits = sum(map(_SUIT_COUNT.__getitem__, runout))
        ranks = []
        for hand_primes, hand_suits, hand in fixed:
            if (hand_suits + suits + _FLUSH_POSSIBLE) & _FLUSH_BITS or len(hand) + len(runout) > 7:
                ranks.append(ranked((*hand, *runout)))
                continue
            key = hand_primes * primes
            found = _BY_RANKS.get(key)
            if found is None:
                found = _BY_RANKS[key] = ranked((*hand, *runout))
            ranks.append(found)
        yield ranks


def strength(cards: Iterable[Card]) -> tuple[int, ...]:
    """The best standard high hand of five of these cards, as a tuple that compares exactly as
    PokerKit's `StandardHighHand.from_game` does: a category (0 high card ... 8 straight
    flush), then the ranks that break ties within it. About ten times faster. Takes five to
    nine distinct cards, so at most one suit can make a flush.

    Without five of a suit only the ranks matter, so five to seven such cards are looked up
    by their ranks after the first time; other hands are worked out by `ranked`."""
    if not isinstance(cards, tuple | list):
        cards = tuple(cards)
    if (
        5 <= len(cards) <= 7
        and not (sum(map(_SUIT_COUNT.__getitem__, cards)) + _FLUSH_POSSIBLE) & _FLUSH_BITS
    ):
        key = prod(map(_RANK_PRIME.__getitem__, cards))
        found = _BY_RANKS.get(key)
        if found is None:
            found = _BY_RANKS[key] = ranked(cards)
        return found
    return ranked(cards)


def ranked(cards: Iterable[Card]) -> tuple[int, ...]:
    """`strength`, worked out from the cards every time."""
    values = [CARD_VALUES[card] for card in cards]
    if not 5 <= len(values) <= 9:
        raise ValueError(f"A hand is the best five of five to nine cards, not {len(values)}.")
    suits: dict[str, list[int]] = {}
    counts = [0] * 15
    for rank, suit in values:
        suits.setdefault(suit, []).append(rank)
        counts[rank] += 1
    flush = next((sorted(ranks, reverse=True) for ranks in suits.values() if len(ranks) >= 5), None)
    if flush and (high := _straight(flush)):
        return (8, high)
    singles: list[int] = []
    pairs: list[int] = []
    trips: list[int] = []
    quads: list[int] = []
    by_count = (singles, pairs, trips, quads)
    for rank in range(14, 1, -1):  # High to low, so each list is too.
        if counts[rank]:
            by_count[counts[rank] - 1].append(rank)
    if quads:
        return (7, quads[0], max(trips + pairs + singles + quads[1:]))
    if trips and len(trips) + len(pairs) > 1:
        return (6, trips[0], max(trips[1:] + pairs))
    if flush:
        return (5, *flush[:5])
    if high := _straight([rank for rank in range(14, 1, -1) if counts[rank]]):
        return (4, high)
    if trips:
        return (3, trips[0], *sorted(trips[1:] + pairs + singles, reverse=True)[:2])
    if len(pairs) > 1:
        return (2, pairs[0], pairs[1], max(pairs[2:] + singles))
    if pairs:
        return (1, pairs[0], *singles[:3])
    return (0, *singles[:5])


def _straight(ranks: list[int]) -> int:
    """The highest straight's top card among these distinct ranks, high to low; 0 if none.
    The ace also plays low, below the 2."""
    if 14 in ranks:
        ranks = [*ranks, 1]
    for index in range(len(ranks) - 4):
        if ranks[index] - ranks[index + 4] == 4:
            return ranks[index]
    return 0


class _CachedHoldem(NoLimitTexasHoldem):
    hand_types = (_CachedHighHand,)


class NoLimitHoldem:
    name = "no_limit_holdem"
    title = "No-limit hold'em"
    max_players = 10

    def deal(
        self, *, stacks: tuple[int, ...], blinds: BlindLevel, rng: random.Random
    ) -> "HoldemHand":
        return HoldemHand(stacks=stacks, blinds=blinds, rng=rng)


class HoldemHand:
    game: type[NoLimitTexasHoldem] = _CachedHoldem  # The PokerKit game this hand plays.

    def __init__(self, *, stacks: tuple[int, ...], blinds: BlindLevel, rng: random.Random) -> None:
        self._total = sum(stacks)
        self._state = self.game.create_state(
            AUTOMATIONS,
            True,
            blinds.ante,
            (blinds.small_blind, blinds.big_blind),
            blinds.big_blind,
            stacks,
            len(stacks),
        )
        # PokerKit shuffles with the global RNG; replace its deck before anything is dealt.
        deck = list(DECK)
        rng.shuffle(deck)
        self._state.deck_cards = deque(deck)
        self._events: list[HandEvent] = []
        self._shown: list[tuple[str, ...]] = [()] * len(stacks)
        self._dealt: list[tuple[str, ...]] = [()] * len(stacks)
        self._recorded = 0
        self._settled = False

    def actor(self) -> int | None:
        state = self._state
        while True:
            # PokerKit deals, burns and shows only between betting rounds, and checking each
            # costs a pass over the deck: ask for the actor first.
            if state.actor_index is not None:
                return state.actor_index
            if state.can_deal_hole():
                state.deal_hole()
            elif state.can_burn_card():
                state.burn_card()
            elif state.can_deal_board():
                state.deal_board()
            elif state.showdown_index is not None:
                # Every contender shows, including losing hands. No bot decision can hide
                # a hand or surrender its pot eligibility after betting has finished.
                state.show_or_muck_hole_cards(True)
            else:
                return state.actor_index

    def legal_actions(self) -> tuple[LegalAction, ...]:
        state = self._state
        if state.showdown_index is not None:
            # actor() normally resolves this automatically. Direct hand callers cannot muck.
            return (LegalAction("show"),)
        actor = state.actor_index
        assert actor is not None
        legal = []
        if state.can_fold():
            legal.append(LegalAction("fold"))
        if state.can_check_or_call():
            legal.append(LegalAction("check" if max(state.bets) == state.bets[actor] else "call"))
        # As `can_complete_bet_or_raise_to()`, which works both amounts out several times.
        low = state.min_completion_betting_or_raising_to_amount
        high = state.max_completion_betting_or_raising_to_amount
        if low is not None and high is not None and low <= high:
            legal.append(LegalAction("raise", low, high))
        return tuple(legal)

    def apply(self, action: Action) -> None:
        state = self._state
        if action.kind in ("show", "muck"):
            if (
                action.amount
                or action.cards
                or action.kind not in {legal.kind for legal in self.legal_actions()}
            ):
                raise ValueError("That showdown action is not legal.")
            state.show_or_muck_hole_cards(action.kind == "show")
        elif action.kind == "fold":
            state.fold()
        elif action.kind in ("check", "call"):
            state.check_or_call()
        elif action.kind == "raise":
            state.complete_bet_or_raise_to(action.amount)
        else:
            raise ValueError(f"No-limit hold'em has no {action.kind} action.")

    def view(self, player: int) -> HandView:
        state = self._state
        self.events()
        return HandView(
            cards=tuple(repr(card) for card in state.hole_cards[player]),
            board=tuple(repr(dealt[0]) for dealt in state.board_cards),
            pot=self._total - sum(state.stacks),
            to_call=max(state.bets) - state.bets[player],
            stacks=tuple(state.stacks),
            bets=tuple(state.bets),
            folded=tuple(not status for status in state.statuses),
            shown=tuple(self._shown),
            phase="showdown" if state.showdown_index is not None else "betting",
        )

    def events(self) -> tuple[HandEvent, ...]:
        """Convert new public PokerKit operations. Deals and burns stay private."""
        operations = self._state.operations
        for operation in operations[self._recorded :]:
            match operation:
                case HoleDealing(player_index=player, cards=cards):
                    self._dealt[player] += tuple(map(repr, cards))
                case AntePosting(player_index=player, amount=amount):
                    self._events.append(HandEvent("ante", player, amount))
                case BlindOrStraddlePosting(player_index=player, amount=amount):
                    self._events.append(HandEvent("blind", player, amount))
                case BoardDealing(cards=cards):
                    self._events.append(HandEvent("board", cards=tuple(map(repr, cards))))
                case Folding(player_index=player):
                    self._events.append(HandEvent("fold", player))
                case CheckingOrCalling(player_index=player, amount=amount):
                    self._events.append(HandEvent("call" if amount else "check", player, amount))
                case CompletionBettingOrRaisingTo(player_index=player, amount=amount):
                    self._events.append(HandEvent("raise", player, amount))
                case HoleCardsShowingOrMucking(player_index=player, hole_cards=cards) if cards:
                    self._shown[player] = tuple(map(repr, cards))
                    self._events.append(HandEvent("show", player, cards=self._shown[player]))
                case HoleCardsShowingOrMucking(player_index=player):
                    self._events.append(HandEvent("muck", player))
                case ChipsPulling(player_index=player, amount=amount) if amount:
                    self._events.append(HandEvent("win", player, amount))
        self._recorded = len(operations)
        if not self._state.status and not self._settled:
            # Uncalled bets are refunded without an operation, so state final stacks outright.
            self._settled = True
            for player, stack in enumerate(self._state.stacks):
                self._events.append(HandEvent("stack", player, stack))
        return tuple(self._events)

    def dealt(self) -> tuple[tuple[str, ...], ...]:
        self.events()
        return tuple(self._dealt)

    def stacks(self) -> tuple[int, ...]:
        return tuple(self._state.stacks)
