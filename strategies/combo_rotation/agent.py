"""Rotating strategy experiment for Boston.

This file is intentionally separate from ../../agent.py. It splits tables into stable
strategy buckets so a large practice run can compare several simple approaches at once.
The split uses state.config.table_id; with 20,000 tables, each bucket should get roughly
1,000 tables.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import poker_agent_sdk
from poker_agent_sdk import Action, GameState


RANKS = "--23456789TJQKA"
RANK_VALUE = {rank: value for value, rank in enumerate(RANKS) if value >= 2}


@dataclass(frozen=True)
class Profile:
    name: str
    open_threshold: float
    three_bet_threshold: float
    call_threshold: float
    short_stack_bb: int
    short_stack_shove: float
    value_bet: float
    semi_bluff: float
    late_call: float
    max_call_pressure: float
    auction_target: float
    auction_cap: int
    auction_horizon: int
    always_fold_v2: bool = False
    bubble_push: bool = False
    pair_hunter: bool = False
    no_late_calls: bool = False
    late_jam: bool = False
    big_stack_bully: bool = False
    committed_auction_boost: bool = False


PROFILES = {
    "always_fold_v2": Profile(
        "always_fold_v2",
        0.90,
        0.90,
        0.90,
        8,
        0.90,
        0.82,
        0.90,
        0.90,
        0.02,
        0.15,
        60,
        160,
        always_fold_v2=True,
    ),
    "nit_raise_fold": Profile(
        "nit_raise_fold",
        0.66,
        0.76,
        0.72,
        10,
        0.62,
        0.76,
        0.55,
        0.74,
        0.10,
        0.50,
        55,
        150,
    ),
    "tight_raise_fold": Profile(
        "tight_raise_fold",
        0.58,
        0.70,
        0.68,
        10,
        0.58,
        0.72,
        0.50,
        0.70,
        0.14,
        0.75,
        85,
        130,
    ),
    "field_mimic": Profile(
        "field_mimic",
        0.47,
        0.60,
        0.74,
        18,
        0.48,
        0.68,
        0.46,
        0.76,
        0.07,
        0.95,
        155,
        105,
        late_jam=True,
        committed_auction_boost=True,
    ),
    "bubble_converter": Profile(
        "bubble_converter",
        0.50,
        0.60,
        0.74,
        20,
        0.48,
        0.66,
        0.44,
        0.78,
        0.07,
        0.96,
        160,
        105,
        bubble_push=True,
        late_jam=True,
        committed_auction_boost=True,
    ),
    "no_late_calls": Profile(
        "no_late_calls",
        0.50,
        0.62,
        0.74,
        18,
        0.50,
        0.70,
        0.48,
        0.90,
        0.04,
        0.94,
        140,
        110,
        no_late_calls=True,
        late_jam=True,
    ),
    "auction_95": Profile(
        "auction_95",
        0.50,
        0.62,
        0.74,
        18,
        0.50,
        0.70,
        0.48,
        0.76,
        0.07,
        0.98,
        180,
        95,
        late_jam=True,
        committed_auction_boost=True,
    ),
    "pair_hunter": Profile(
        "pair_hunter",
        0.52,
        0.64,
        0.74,
        18,
        0.50,
        0.70,
        0.48,
        0.76,
        0.07,
        0.95,
        160,
        105,
        pair_hunter=True,
        late_jam=True,
        committed_auction_boost=True,
    ),
    "hybrid_best": Profile(
        "hybrid_best",
        0.48,
        0.60,
        0.74,
        20,
        0.48,
        0.66,
        0.44,
        0.78,
        0.06,
        0.96,
        165,
        100,
        bubble_push=True,
        no_late_calls=True,
        late_jam=True,
        committed_auction_boost=True,
    ),
    "late_jam_nyx": Profile(
        "late_jam_nyx",
        0.48,
        0.60,
        0.74,
        22,
        0.46,
        0.66,
        0.43,
        0.76,
        0.08,
        0.90,
        135,
        115,
        bubble_push=True,
        no_late_calls=True,
        late_jam=True,
        committed_auction_boost=True,
    ),
    "short_stack_converter": Profile(
        "short_stack_converter",
        0.54,
        0.64,
        0.74,
        24,
        0.48,
        0.68,
        0.45,
        0.78,
        0.08,
        0.88,
        120,
        120,
        bubble_push=True,
        no_late_calls=True,
        late_jam=True,
    ),
    "big_stack_bully": Profile(
        "big_stack_bully",
        0.48,
        0.62,
        0.74,
        16,
        0.50,
        0.66,
        0.44,
        0.76,
        0.08,
        0.90,
        125,
        115,
        bubble_push=True,
        no_late_calls=True,
        big_stack_bully=True,
    ),
    "allin_auction_boost": Profile(
        "allin_auction_boost",
        0.52,
        0.64,
        0.72,
        18,
        0.50,
        0.66,
        0.44,
        0.76,
        0.08,
        0.95,
        160,
        110,
        bubble_push=True,
        no_late_calls=True,
        late_jam=True,
        committed_auction_boost=True,
    ),
}


# 20 buckets ~= 1,000 tables each in a 20,000-table run.
BUCKET_PLAN = (
    "field_mimic",  # bucket 00: top-field low-call/high-raise mimic
    "field_mimic",  # bucket 01
    "bubble_converter",  # bucket 02: converts 4th/5th into top 3
    "bubble_converter",  # bucket 03
    "hybrid_best",  # bucket 04
    "hybrid_best",  # bucket 05
    "hybrid_best",  # bucket 06
    "late_jam_nyx",  # bucket 07: wide late preflop pressure, Nyx-style
    "late_jam_nyx",  # bucket 08
    "late_jam_nyx",  # bucket 09
    "short_stack_converter",  # bucket 10: <=24BB shove/fold pressure
    "short_stack_converter",  # bucket 11
    "big_stack_bully",  # bucket 12: big-stack pressure near bubble
    "big_stack_bully",  # bucket 13
    "allin_auction_boost",  # bucket 14: spend harder when committed/all-in
    "allin_auction_boost",  # bucket 15
    "auction_95",  # bucket 16: high flop-chip usage
    "pair_hunter",  # bucket 17: targeted auction pair/trips hunting
    "late_jam_nyx",  # bucket 18: extra weight on observed winner profile
    "field_mimic",  # bucket 19: extra weight on high-PFR/high-auction profile
)


class Agent(poker_agent_sdk.Agent):
    def act(self, state: GameState, memory: bytes) -> tuple[Action, bytes]:
        mem = decode_memory(memory)
        bucket = table_bucket(state)
        profile = PROFILES[BUCKET_PLAN[bucket]]
        mem["bucket"] = bucket
        mem["strategy"] = profile.name

        try:
            if state.phase == "auction":
                action = auction_action(state, mem, profile)
            elif state.phase == "betting":
                action = betting_action(state, mem, profile)
            else:
                action = fallback(state)
        except Exception:
            action = fallback(state)
        return ensure_legal(state, action), encode_memory(mem)


def betting_action(state: GameState, memory: dict[str, Any], profile: Profile) -> Action:
    if auction_target_landed(state, memory):
        memory["committed_hand"] = state.hand_number

    if profile.always_fold_v2:
        return always_fold_v2_action(state, memory)

    top3_mode = profile.bubble_push and (live_players(state) <= 5 or state.hand_number >= 70)
    active_profile = adjust_for_bubble(profile, top3_mode)

    if not state.board:
        return preflop_action(state, active_profile)

    equity = postflop_equity(state)
    pot_odds = max(0, state.to_call) / max(1, state.pot + max(0, state.to_call))
    pressure = max(0, state.to_call) / max(1, own_stack(state) + max(0, state.to_call))
    street = len(state.board)

    if memory.get("committed_hand") == state.hand_number:
        return never_fold_action(state, equity)

    if raise_ := state.can("raise"):
        semi_bluff = equity >= active_profile.semi_bluff and has_draw(state.cards, state.board)
        if equity >= active_profile.value_bet or semi_bluff:
            fraction = 0.85 if equity >= 0.86 else 0.55 if semi_bluff else 0.65
            return Action.raise_to(clamp_int(call_total(state) + int(state.pot * fraction), raise_))

    if state.can("check"):
        return Action.check()

    if state.can("call") and should_call(state, equity, pot_odds, pressure, street, active_profile):
        return Action.call()

    return Action.fold() if state.can("fold") else fallback(state)


def always_fold_v2_action(state: GameState, memory: dict[str, Any]) -> Action:
    if auction_target_landed(state, memory):
        memory["committed_hand"] = state.hand_number
    if memory.get("committed_hand") == state.hand_number:
        return never_fold_action(state, postflop_equity(state) if state.board else 0.0)
    if not state.board and premium_starting_hand(state.cards):
        memory["premium_hand"] = state.hand_number
        if raise_ := state.can("raise"):
            return Action.raise_to(clamp_int(3 * state.blinds.big_blind, raise_))
        if state.can("check"):
            return Action.check()
        if state.can("call"):
            return Action.call()
    if state.board and memory.get("premium_hand") == state.hand_number:
        strength = made_hand_strength(state.cards, state.board)
        if strength >= 4 and (raise_ := state.can("raise")):
            return Action.raise_to(clamp_int(call_total(state) + int(state.pot * 0.65), raise_))
        if state.can("check"):
            return Action.check()
        if strength >= 4 and state.can("call"):
            return Action.call()
    return passive_fold(state)


def preflop_action(state: GameState, profile: Profile) -> Action:
    equity = preflop_equity(state.cards)
    stack_bb = own_stack(state) / max(1, state.blinds.big_blind)
    pressure = max(0, state.to_call) / max(1, own_stack(state) + max(0, state.to_call))
    open_threshold = position_open_threshold(state, profile.open_threshold)
    late_pressure = state.hand_number >= 90 or live_players(state) <= 5
    big_stack_bully = profile.big_stack_bully and late_pressure and is_big_stack(state)

    if stack_bb <= profile.short_stack_bb:
        if raise_ := state.can("raise"):
            if equity >= profile.short_stack_shove or (
                profile.late_jam and late_pressure and late_jam_hand(state.cards, facing_raise=state.to_call > 0)
            ):
                return Action.raise_to(raise_.max_amount)
        if state.can("check"):
            return Action.check()
        if state.can("call") and equity >= profile.three_bet_threshold and pressure <= 0.10:
            return Action.call()
        return Action.fold() if state.can("fold") else fallback(state)

    if raise_ := state.can("raise"):
        if profile.late_jam and late_pressure:
            if state.to_call <= 0 and late_jam_hand(state.cards, facing_raise=False):
                return Action.raise_to(raise_.max_amount)
            if state.to_call > 0 and late_jam_hand(state.cards, facing_raise=True):
                return Action.raise_to(raise_.max_amount)
        if big_stack_bully and state.to_call <= 0 and bully_open_hand(state.cards):
            target = max(raise_.min_amount, 4 * state.blinds.big_blind)
            return Action.raise_to(clamp_int(target, raise_))
        if state.to_call <= 0 and equity >= open_threshold:
            return Action.raise_to(clamp_int(3 * state.blinds.big_blind, raise_))
        if state.to_call > 0 and equity >= profile.three_bet_threshold:
            target = max(3 * max_bet(state), call_total(state) + 4 * state.blinds.big_blind)
            return Action.raise_to(clamp_int(target, raise_))

    if state.can("check"):
        return Action.check()
    if state.can("call") and equity >= profile.call_threshold and pressure <= 0.06:
        return Action.call()
    return Action.fold() if state.can("fold") else fallback(state)


def auction_action(state: GameState, memory: dict[str, Any], profile: Profile) -> Action:
    legal = state.can("bid")
    cards = tuple(state.variant.get("auction_cards") or ())
    if legal is None or legal.max_amount <= 0 or len(cards) != 5:
        return Action("bid", cards=(0, 0, 0, 0, 0))

    if profile.always_fold_v2:
        targets = trips_targets(state.cards, cards)
        budget = min(legal.max_amount, always_fold_v2_auction_budget(state, targets))
        return targeted_bid(state, memory, cards, targets, budget)

    scores = [auction_card_score(card, state.cards, profile) for card in cards]
    budget = auction_budget(state, profile, max(scores))
    if budget <= 0:
        memory["last_auction"] = {"hand": state.hand_number, "cards": list(cards), "bids": [0] * 5}
        return Action("bid", cards=(0, 0, 0, 0, 0))

    ranked = sorted(range(5), key=lambda i: (scores[i] + (0.02 if i >= 3 else 0.0), -i), reverse=True)
    targets = {index: max(1, int(scores[index] * 100)) for index in ranked[:3] if scores[index] > 0}
    if profile.pair_hunter:
        for index, card in enumerate(cards):
            if rank(card) in {rank(hole) for hole in state.cards}:
                targets[index] = max(targets.get(index, 0), 80)
    return targeted_bid(state, memory, cards, targets, min(legal.max_amount, budget))


def targeted_bid(
    state: GameState,
    memory: dict[str, Any],
    auction_cards: tuple[str, ...],
    targets: dict[int, int],
    budget: int,
) -> Action:
    if budget <= 0 or not targets:
        memory["last_auction"] = {"hand": state.hand_number, "cards": list(auction_cards), "bids": [0] * 5}
        return Action("bid", cards=(0, 0, 0, 0, 0))

    bids = [0, 0, 0, 0, 0]
    total_weight = sum(targets.values())
    spent = 0
    for index, weight in targets.items():
        amount = max(1, budget * weight // total_weight)
        bids[index] = amount
        spent += amount
    best = max(targets, key=targets.get)
    bids[best] += budget - spent
    memory["last_auction"] = {"hand": state.hand_number, "cards": list(auction_cards), "bids": list(bids)}
    memory["auction_hand"] = state.hand_number
    memory["auction_targets"] = [auction_cards[index] for index in targets]
    return Action("bid", cards=tuple(bids))


def auction_budget(state: GameState, profile: Profile, best_card_score: float) -> int:
    legal = state.can("bid")
    if legal is None or legal.max_amount <= 0:
        return 0
    balance = legal.max_amount
    equity = preflop_equity(state.cards)
    if equity >= 0.78:
        base = 35
    elif equity >= profile.open_threshold or best_card_score >= 0.38:
        base = 18
    elif equity >= 0.45 or best_card_score >= 0.18:
        base = 8
    else:
        base = 0
    if state.hand_number <= 35:
        base = int(base * 0.55)

    spent = max(0, 1000 - balance)
    progress = min(1.0, state.hand_number / max(1, profile.auction_horizon))
    target_spent = int(1000 * profile.auction_target * progress)
    catchup = min(profile.auction_cap, int(max(0, target_spent - spent) * 0.75))
    budget = base + catchup + int(state.pot / max(1, state.blinds.big_blind))
    if profile.committed_auction_boost and is_pot_committed(state):
        budget = max(budget, 100 + int(80 * best_card_score))
    return max(0, min(balance, budget))


def always_fold_v2_auction_budget(state: GameState, targets: dict[int, int]) -> int:
    if not targets:
        return 0
    base = 50 + min(50, max(targets.values()) - 100)
    if state.hand_number >= 80:
        base += 25
    if state.hand_number >= 120:
        base += 25
    return base


def should_call(
    state: GameState,
    equity: float,
    pot_odds: float,
    pressure: float,
    street: int,
    profile: Profile,
) -> bool:
    if max(0, state.to_call) <= 0:
        return False
    if profile.no_late_calls and street >= 4:
        return equity >= 0.82 and pressure <= 0.08 and equity + 0.08 >= pot_odds
    if street >= 4:
        return equity >= profile.late_call and pressure <= profile.max_call_pressure and equity + 0.08 >= pot_odds
    if equity >= profile.call_threshold and pressure <= profile.max_call_pressure and equity + 0.08 >= pot_odds:
        return True
    if has_draw(state.cards, state.board):
        return equity >= 0.46 and pressure <= 0.08 and equity + 0.12 >= pot_odds
    return False


def adjust_for_bubble(profile: Profile, top3_mode: bool) -> Profile:
    if not top3_mode:
        return profile
    return Profile(
        profile.name,
        max(0.44, profile.open_threshold - 0.08),
        max(0.62, profile.three_bet_threshold - 0.04),
        profile.call_threshold,
        max(profile.short_stack_bb, 16),
        min(profile.short_stack_shove, 0.50),
        min(profile.value_bet, 0.66),
        min(profile.semi_bluff, 0.44),
        profile.late_call,
        profile.max_call_pressure,
        profile.auction_target,
        profile.auction_cap,
        profile.auction_horizon,
        profile.always_fold_v2,
        profile.bubble_push,
        profile.pair_hunter,
        profile.no_late_calls,
        profile.late_jam,
        profile.big_stack_bully,
        profile.committed_auction_boost,
    )


def premium_starting_hand(cards: tuple[str, ...]) -> bool:
    if len(cards) < 2:
        return False
    a, b = rank(cards[0]), rank(cards[1])
    hi, lo = max(a, b), min(a, b)
    if hi == lo:
        return hi >= 11
    return hi == 14 and lo >= 12


def late_jam_hand(cards: tuple[str, ...], facing_raise: bool) -> bool:
    if len(cards) < 2:
        return False
    a, b = rank(cards[0]), rank(cards[1])
    hi, lo = max(a, b), min(a, b)
    suited = suit(cards[0]) == suit(cards[1])
    gap = hi - lo
    if hi == lo:
        return hi >= (7 if facing_raise else 2)
    if hi == 14:
        return lo >= (10 if facing_raise else 2)
    if facing_raise:
        return (hi, lo) in ((13, 12), (13, 11), (12, 11)) or (suited and hi >= 13 and lo >= 10)
    if hi >= 13 and lo >= 9:
        return True
    if hi >= 12 and lo >= 10:
        return True
    return suited and hi >= 7 and gap <= 2


def bully_open_hand(cards: tuple[str, ...]) -> bool:
    if len(cards) < 2:
        return False
    a, b = rank(cards[0]), rank(cards[1])
    hi, lo = max(a, b), min(a, b)
    suited = suit(cards[0]) == suit(cards[1])
    gap = hi - lo
    if hi == lo or hi == 14:
        return True
    if hi >= 13 and lo >= 7:
        return True
    if hi >= 12 and lo >= 9:
        return True
    return suited and hi >= 8 and gap <= 2


def trips_targets(hole: tuple[str, ...], auction_cards: tuple[str, ...]) -> dict[int, int]:
    if len(hole) < 2:
        return {}
    hole_ranks = [rank(card) for card in hole]
    if hole_ranks[0] != hole_ranks[1] or hole_ranks[0] < 10:
        return {}
    return {
        index: 100 + 5 * (hole_ranks[0] - 10)
        for index, card in enumerate(auction_cards)
        if rank(card) == hole_ranks[0]
    }


def auction_target_landed(state: GameState, memory: dict[str, Any]) -> bool:
    if len(state.board) < 3 or memory.get("auction_hand") != state.hand_number:
        return False
    targets = memory.get("auction_targets")
    return isinstance(targets, list) and any(card in state.board[:3] for card in targets)


def never_fold_action(state: GameState, equity: float) -> Action:
    if raise_ := state.can("raise"):
        if equity >= 0.70 or made_hand_strength(state.cards, state.board) >= 4:
            return Action.raise_to(clamp_int(call_total(state) + int(state.pot * 0.65), raise_))
    if state.can("check"):
        return Action.check()
    if state.can("call"):
        return Action.call()
    first = state.legal_actions[0]
    return Action(first.kind, first.min_amount)


def preflop_equity(cards: tuple[str, ...]) -> float:
    if len(cards) < 2:
        return 0.0
    a, b = rank(cards[0]), rank(cards[1])
    hi, lo = max(a, b), min(a, b)
    suited = suit(cards[0]) == suit(cards[1])
    gap = hi - lo
    if hi == lo:
        return clamp(0.38 + hi * 0.035, 0.0, 0.90)
    score = 0.06 + hi * 0.030 + lo * 0.012
    if suited:
        score += 0.050
    if gap == 1:
        score += 0.035
    elif gap == 2:
        score += 0.018
    elif gap >= 5:
        score -= 0.070
    if lo <= 5 and gap >= 6:
        score -= 0.045
    if hi >= 14 and lo >= 10:
        score += 0.080
    elif hi >= 13 and lo >= 10:
        score += 0.045
    return clamp(score, 0.0, 0.88)


def postflop_equity(state: GameState) -> float:
    made = made_hand_equity(state.cards, state.board)
    draw = draw_score(state.cards, state.board)
    penalty = max(0, live_players(state) - 2) * 0.025
    return clamp(made + draw - penalty, 0.0, 0.98)


def made_hand_equity(hole: tuple[str, ...], board: tuple[str, ...]) -> float:
    strength = made_hand_strength(hole, board)
    if strength >= 8:
        return 0.97
    if strength == 7:
        return 0.95
    if strength == 6:
        return 0.91
    if strength == 5:
        return 0.82
    if strength == 4:
        return 0.78 if pocket_pair_trips(hole, board) else 0.70
    if strength == 3:
        return 0.62
    if strength == 2:
        return pair_equity(hole, board)
    return clamp(max((rank(card) for card in hole), default=0) * 0.025, 0.08, 0.38)


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
    if flush or straight:
        return 5
    if counts[0] == 3:
        return 4
    if counts[0] == 2 and len(counts) > 1 and counts[1] == 2:
        return 3
    if counts[0] == 2:
        return 2
    return 1


def pocket_pair_trips(hole: tuple[str, ...], board: tuple[str, ...]) -> bool:
    return len(hole) == 2 and rank(hole[0]) == rank(hole[1]) and any(rank(card) == rank(hole[0]) for card in board)


def pair_equity(hole: tuple[str, ...], board: tuple[str, ...]) -> float:
    ranks = [rank(card) for card in hole + board]
    hole_ranks = [rank(card) for card in hole]
    paired = [value for value in set(ranks) if ranks.count(value) >= 2]
    if not paired:
        return 0.35
    pair_rank = max(paired)
    if pair_rank not in hole_ranks:
        return clamp(0.25 + max(hole_ranks, default=0) * 0.014, 0.28, 0.48)
    return clamp(0.47 + pair_rank * 0.018, 0.35, 0.70)


def auction_card_score(card: str, hole: tuple[str, ...], profile: Profile) -> float:
    card_rank = rank(card)
    hole_ranks = [rank(hole_card) for hole_card in hole]
    hole_suits = [suit(hole_card) for hole_card in hole]
    score = max(0.0, (card_rank - 8) * 0.025)
    if card_rank in hole_ranks:
        score += 0.36 if profile.pair_hunter else 0.32
    if suit(card) in hole_suits:
        score += 0.10
    for hole_rank in hole_ranks:
        gap = abs(card_rank - hole_rank)
        if gap == 1:
            score += 0.08
        elif gap == 2:
            score += 0.04
    return clamp(score, 0.0, 0.75)


def has_draw(hole: tuple[str, ...], board: tuple[str, ...]) -> bool:
    return bool(board) and draw_score(hole, board) >= 0.08


def draw_score(hole: tuple[str, ...], board: tuple[str, ...]) -> float:
    cards = hole + board
    ranks = [rank(card) for card in cards]
    suits = [suit(card) for card in cards]
    score = 0.0
    if max((suits.count(value) for value in set(suits)), default=0) == 4:
        score += 0.13
    values = set(ranks)
    if 14 in values:
        values.add(1)
    best = 0
    for start in range(1, 11):
        best = max(best, sum(1 for value in range(start, start + 5) if value in values))
    if best >= 4:
        score += 0.10
    elif best == 3:
        score += 0.035
    return score


def position_open_threshold(state: GameState, base: float) -> float:
    players = max(2, state.config.players)
    rel = (state.seat - state.button) % players
    threshold = base
    if rel == 0:
        threshold -= 0.05
    elif rel in (players - 1, players - 2):
        threshold -= 0.03
    elif rel in (1, 2):
        threshold += 0.03
    if state.hand_number <= 50:
        threshold += 0.03
    return clamp(threshold, 0.44, 0.72)


def table_bucket(state: GameState) -> int:
    value = 2166136261
    text = state.config.table_id or f"{state.config.seat}:{state.config.players}"
    for char in text:
        value ^= ord(char)
        value = (value * 16777619) & 0xFFFFFFFF
    return value % len(BUCKET_PLAN)


def ensure_legal(state: GameState, action: Action) -> Action:
    if action.kind == "raise":
        legal = state.can("raise")
        return Action.raise_to(clamp_int(action.amount, legal)) if legal else fallback(state)
    if action.kind == "bid":
        legal = state.can("bid")
        if legal is None:
            return fallback(state)
        bids = tuple(max(0, int(bid)) for bid in action.cards[:5])
        bids = bids + (0,) * (5 - len(bids))
        while sum(bids) > legal.max_amount:
            index = max(range(5), key=lambda i: bids[i])
            bids = bids[:index] + (bids[index] - 1,) + bids[index + 1 :]
        return Action("bid", cards=bids)
    if action.kind == "call" and state.can("check"):
        return Action.check()
    return action if state.can(action.kind) else fallback(state)


def fallback(state: GameState) -> Action:
    if state.can("check"):
        return Action.check()
    if state.can("fold"):
        return Action.fold()
    first = state.legal_actions[0]
    return Action(first.kind, first.min_amount)


def passive_fold(state: GameState) -> Action:
    if state.can("fold"):
        return Action.fold()
    if state.can("check"):
        return Action.check()
    return fallback(state)


def call_total(state: GameState) -> int:
    return own_bet(state) + max(0, state.to_call)


def max_bet(state: GameState) -> int:
    return max((seat.bet for seat in state.seats), default=0)


def own_stack(state: GameState) -> int:
    for seat in state.seats:
        if seat.seat == state.seat:
            return int(seat.stack)
    return 0


def own_bet(state: GameState) -> int:
    for seat in state.seats:
        if seat.seat == state.seat:
            return int(seat.bet)
    return 0


def live_players(state: GameState) -> int:
    return sum(1 for seat in state.seats if seat.status in ("active", "all_in"))


def is_big_stack(state: GameState) -> bool:
    stack = own_stack(state)
    live_stacks = [seat.stack for seat in state.seats if seat.status in ("active", "all_in")]
    if not live_stacks:
        return False
    return stack >= 1.25 * (sum(live_stacks) / len(live_stacks))


def is_pot_committed(state: GameState) -> bool:
    stack = own_stack(state)
    invested = own_bet(state)
    if stack <= 0:
        return True
    return invested >= stack or max(0, state.to_call) >= stack * 0.50


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


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def clamp_int(value: int, legal: Any) -> int:
    return int(max(legal.min_amount, min(legal.max_amount, value)))


def decode_memory(memory: bytes) -> dict[str, Any]:
    if not memory:
        return {}
    try:
        decoded = json.loads(memory.decode())
        return decoded if isinstance(decoded, dict) else {}
    except Exception:
        return {}


def encode_memory(memory: dict[str, Any]) -> bytes:
    try:
        encoded = json.dumps(memory, separators=(",", ":")).encode()
    except Exception:
        return b"{}"
    return encoded if len(encoded) <= 12_000 else b"{}"
