"""Conservative always-fold experiment with rare premium exceptions.

This is kept separate from the main strategy in ../../agent.py. The default behavior is still
fold/check, but the bot plays a narrow preflop range and may buy a flop card that makes trips
or better. If that auction target lands, it refuses to fold for the rest of that hand.
"""

from __future__ import annotations

import json
from typing import Any

import poker_agent_sdk
from poker_agent_sdk import Action, GameState


RANKS = "--23456789TJQKA"
RANK_VALUE = {rank: value for value, rank in enumerate(RANKS) if value >= 2}


class Agent(poker_agent_sdk.Agent):
    def act(self, state: GameState, memory: bytes) -> tuple[Action, bytes]:
        remembered = Memory.decode(memory)
        remembered = reset_for_new_hand(state, remembered)

        if state.phase == "auction":
            action = auction_action(state, remembered)
            return action, Memory.encode(remembered)

        action = betting_action(state, remembered)
        return ensure_legal(state, action), Memory.encode(remembered)


def betting_action(state: GameState, memory: dict[str, Any]) -> Action:
    if auction_target_landed(state, memory):
        memory["committed_hand"] = state.hand_number

    if not state.board:
        if premium_starting_hand(state.cards):
            memory["premium_hand"] = state.hand_number
            if raise_ := state.can("raise"):
                return Action.raise_to(preflop_raise_amount(state, raise_))
            if state.can("check"):
                return Action.check()
            if state.can("call"):
                return Action.call()
        return passive_fold(state)

    if memory.get("committed_hand") == state.hand_number:
        return never_fold_action(state)

    # Premium preflop hands stay conservative after the flop: value bet strong made hands,
    # otherwise check for free and fold to pressure.
    if memory.get("premium_hand") == state.hand_number:
        strength = made_hand_strength(state.cards, state.board)
        if strength >= 4 and (raise_ := state.can("raise")):
            return Action.raise_to(value_raise_amount(state, raise_))
        if state.can("check"):
            return Action.check()
        if strength >= 4 and state.can("call"):
            return Action.call()

    return passive_fold(state)


def auction_action(state: GameState, memory: dict[str, Any]) -> Action:
    legal = state.can("bid")
    auction_cards = tuple(state.variant.get("auction_cards") or ())
    if legal is None or len(auction_cards) != 5 or legal.max_amount <= 0:
        return Action("bid", cards=(0, 0, 0, 0, 0))

    targets = auction_targets(state.cards, auction_cards)
    if not targets:
        return Action("bid", cards=(0, 0, 0, 0, 0))

    budget = auction_budget(state, legal.max_amount, targets)
    if budget <= 0:
        return Action("bid", cards=(0, 0, 0, 0, 0))

    bids = [0, 0, 0, 0, 0]
    weights = [targets[index] for index in targets]
    total_weight = sum(weights)
    spent = 0
    for index, weight in targets.items():
        amount = max(1, budget * weight // total_weight)
        bids[index] = amount
        spent += amount
    best = max(targets, key=targets.get)
    bids[best] += budget - spent

    memory["auction_hand"] = state.hand_number
    memory["auction_targets"] = [auction_cards[index] for index in targets]
    return Action("bid", cards=tuple(bids))


def auction_targets(hole: tuple[str, ...], auction_cards: tuple[str, ...]) -> dict[int, int]:
    if len(hole) < 2:
        return {}

    hole_ranks = [rank(card) for card in hole]
    targets: dict[int, int] = {}

    # The main intended exception: a pocket pair can buy the matching rank to make trips.
    if hole_ranks[0] == hole_ranks[1] and hole_ranks[0] >= 10:
        for index, card in enumerate(auction_cards):
            if rank(card) == hole_ranks[0]:
                targets[index] = 100 + 5 * (hole_ranks[0] - 10)

    return targets


def auction_budget(state: GameState, balance: int, targets: dict[int, int]) -> int:
    if balance <= 0:
        return 0
    best_weight = max(targets.values(), default=0)
    base = 50 + min(50, max(0, best_weight - 100))

    # Spend a little more later because unused flop-chips score nothing.
    if state.hand_number >= 80:
        base += 25
    if state.hand_number >= 120:
        base += 25
    return min(balance, base)


def auction_target_landed(state: GameState, memory: dict[str, Any]) -> bool:
    if len(state.board) < 3 or memory.get("auction_hand") != state.hand_number:
        return False
    targets = memory.get("auction_targets")
    if not isinstance(targets, list):
        return False
    return any(card in state.board[:3] for card in targets)


def premium_starting_hand(cards: tuple[str, ...]) -> bool:
    if len(cards) < 2:
        return False
    a, b = rank(cards[0]), rank(cards[1])
    hi, lo = max(a, b), min(a, b)
    if hi == lo:
        return hi >= 11
    return hi == 14 and lo >= 12


def made_hand_strength(hole: tuple[str, ...], board: tuple[str, ...]) -> int:
    cards = hole + board
    if len(cards) < 5:
        return 0
    ranks = [rank(card) for card in cards]
    suits = [suit(card) for card in cards]
    counts = sorted((ranks.count(value) for value in set(ranks)), reverse=True)
    flush = max((suits.count(value) for value in set(suits)), default=0) >= 5
    straight = has_straight(ranks)
    if flush and straight:
        return 8
    if counts[0] == 4:
        return 7
    if counts[0] == 3 and len(counts) > 1 and counts[1] >= 2:
        return 6
    if flush:
        return 5
    if straight:
        return 5
    if counts[0] == 3:
        return 4
    if counts[0] == 2 and len(counts) > 1 and counts[1] == 2:
        return 3
    if counts[0] == 2:
        return 2
    return 1


def preflop_raise_amount(state: GameState, legal: Any) -> int:
    big_blind = max(1, state.blinds.big_blind)
    desired = max(legal.min_amount, 3 * big_blind)
    return int(max(legal.min_amount, min(legal.max_amount, desired)))


def value_raise_amount(state: GameState, legal: Any) -> int:
    own_bet = own_current_bet(state)
    call_total = own_bet + max(0, state.to_call)
    desired = call_total + int(max(1, state.pot) * 0.65)
    return int(max(legal.min_amount, min(legal.max_amount, desired)))


def never_fold_action(state: GameState) -> Action:
    if raise_ := state.can("raise"):
        strength = made_hand_strength(state.cards, state.board)
        if strength >= 4:
            return Action.raise_to(value_raise_amount(state, raise_))
    if state.can("check"):
        return Action.check()
    if state.can("call"):
        return Action.call()
    first = state.legal_actions[0]
    return Action(first.kind, first.min_amount)


def passive_fold(state: GameState) -> Action:
    if state.can("fold"):
        return Action.fold()
    if state.can("check"):
        return Action.check()
    first = state.legal_actions[0]
    return Action(first.kind, first.min_amount)


def ensure_legal(state: GameState, action: Action) -> Action:
    if action.kind == "raise":
        legal = state.can("raise")
        if legal is None:
            return passive_fold(state)
        return Action.raise_to(max(legal.min_amount, min(legal.max_amount, action.amount)))
    if action.kind == "call" and state.can("check"):
        return Action.check()
    if state.can(action.kind):
        return action
    return passive_fold(state)


def reset_for_new_hand(state: GameState, memory: dict[str, Any]) -> dict[str, Any]:
    if memory.get("seen_hand") == state.hand_number:
        return memory
    return {"seen_hand": state.hand_number}


def own_current_bet(state: GameState) -> int:
    for seat in state.seats:
        if seat.seat == state.seat:
            return int(seat.bet)
    return 0


def rank(card: str) -> int:
    return RANK_VALUE.get(card[0], 0) if card else 0


def suit(card: str) -> str:
    return card[1] if len(card) > 1 else ""


def has_straight(ranks: list[int]) -> bool:
    values = set(ranks)
    if 14 in values:
        values.add(1)
    run = 0
    previous = None
    for value in sorted(values):
        run = run + 1 if previous is not None and value == previous + 1 else 1
        if run >= 5:
            return True
        previous = value
    return False


class Memory:
    @staticmethod
    def decode(memory: bytes) -> dict[str, Any]:
        if not memory:
            return {}
        try:
            decoded = json.loads(memory.decode())
            return decoded if isinstance(decoded, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def encode(memory: dict[str, Any]) -> bytes:
        try:
            return json.dumps(memory, separators=(",", ":")).encode()
        except Exception:
            return b"{}"
