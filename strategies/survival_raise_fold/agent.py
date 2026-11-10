"""Survival-first raise/fold experiment for Boston.

This strategy is intentionally simple:
- preflop: raise good hands, fold bad hands, almost never call
- auction: buy direct-improvement cards and catch up spend after hand 50
- postflop: value bet strong hands, check medium hands, avoid turn/river hero calls
"""

import json
from typing import Any

import poker_agent_sdk
from poker_agent_sdk import Action, GameState


RANKS = "--23456789TJQKA"
RANK_VALUE = {rank: value for value, rank in enumerate(RANKS) if value >= 2}


class Config:
    PREFLOP_OPEN = 0.50
    PREFLOP_CALL = 0.66
    PREFLOP_THREE_BET = 0.70
    SHORT_STACK_BB = 10
    SHORT_STACK_SHOVE = 0.58

    VALUE_BET = 0.72
    MONSTER = 0.86
    POSTFLOP_CALL = 0.58
    DRAW_CALL = 0.46
    LATE_STREET_CALL = 0.70
    MAX_CALL_PRESSURE = 0.20
    MAX_DRAW_PRESSURE = 0.08
    POT_ODDS_MARGIN = 0.08

    AUCTION_TARGET = 0.80
    AUCTION_HORIZON = 130
    AUCTION_CATCHUP = 0.75
    AUCTION_CATCHUP_CAP = 90
    AUCTION_PAIR_FRACTION = 40
    AUCTION_RESERVE_BEFORE_80 = 175
    AUCTION_LATE_NUDGE = 0.02

    MEMORY_LIMIT = 8_000


class Agent(poker_agent_sdk.Agent):
    def act(self, state: GameState, memory: bytes) -> tuple[Action, bytes]:
        mem = decode_memory(memory)
        try:
            if state.phase == "auction":
                action = auction_action(state, mem)
            elif state.phase == "betting":
                action = betting_action(state)
            else:
                action = fallback(state)
        except Exception:
            action = fallback(state)
        return ensure_legal(state, action), encode_memory(mem)


def betting_action(state: GameState) -> Action:
    if not state.board:
        return preflop_action(state)

    equity = postflop_equity(state)
    pot_odds = state.to_call / max(1, state.pot + state.to_call)
    pressure = state.to_call / max(1, own_stack(state) + state.to_call)
    street = len(state.board)

    if raise_ := state.can("raise"):
        if equity >= Config.VALUE_BET:
            fraction = 0.85 if equity >= Config.MONSTER else 0.65
            return Action.raise_to(clamp_int(call_total(state) + int(state.pot * fraction), raise_))

    if state.can("check"):
        return Action.check()

    if state.can("call"):
        if street >= 4:
            if equity >= Config.LATE_STREET_CALL and pressure <= Config.MAX_CALL_PRESSURE:
                return Action.call()
            if pot_odds <= 0.08 and pressure <= Config.MAX_DRAW_PRESSURE:
                return Action.call()
        elif equity >= Config.POSTFLOP_CALL and equity + Config.POT_ODDS_MARGIN >= pot_odds:
            if pressure <= Config.MAX_CALL_PRESSURE:
                return Action.call()
        elif has_draw(state.cards, state.board):
            if equity >= Config.DRAW_CALL and equity + 0.12 >= pot_odds:
                if pressure <= Config.MAX_DRAW_PRESSURE:
                    return Action.call()

    return Action.fold() if state.can("fold") else fallback(state)


def preflop_action(state: GameState) -> Action:
    equity = preflop_equity(state.cards)
    stack_bb = own_stack(state) / max(1, state.blinds.big_blind)
    pressure = state.to_call / max(1, own_stack(state) + state.to_call)
    open_threshold = position_open_threshold(state)

    if stack_bb <= Config.SHORT_STACK_BB:
        if raise_ := state.can("raise"):
            if equity >= Config.SHORT_STACK_SHOVE:
                return Action.raise_to(raise_.max_amount)
        if state.can("check"):
            return Action.check()
        if state.can("call") and equity >= Config.PREFLOP_THREE_BET and pressure <= 0.15:
            return Action.call()
        return Action.fold() if state.can("fold") else fallback(state)

    if raise_ := state.can("raise"):
        if state.to_call <= 0 and equity >= open_threshold:
            return Action.raise_to(clamp_int(3 * state.blinds.big_blind, raise_))
        if state.to_call > 0 and equity >= Config.PREFLOP_THREE_BET:
            target = max(3 * max_bet(state), call_total(state) + 4 * state.blinds.big_blind)
            return Action.raise_to(clamp_int(target, raise_))

    if state.can("check"):
        return Action.check()
    if state.can("call") and equity >= Config.PREFLOP_CALL and pressure <= 0.08:
        return Action.call()
    return Action.fold() if state.can("fold") else fallback(state)


def auction_action(state: GameState, memory: dict[str, Any]) -> Action:
    legal = state.can("bid")
    cards = tuple(state.variant.get("auction_cards") or ())
    if legal is None or legal.max_amount <= 0 or len(cards) != 5:
        return Action("bid", cards=(0, 0, 0, 0, 0))

    scores = [auction_card_score(card, state.cards) for card in cards]
    budget = auction_budget(state, max(scores))
    if budget <= 0:
        memory["last_auction"] = {"hand": state.hand_number, "cards": list(cards), "bids": [0] * 5}
        return Action("bid", cards=(0, 0, 0, 0, 0))

    ranked = sorted(
        range(5),
        key=lambda i: (scores[i] + (Config.AUCTION_LATE_NUDGE if i >= 3 else 0.0), -i),
        reverse=True,
    )[:3]
    weights = [max(1, int(scores[i] * 100)) for i in ranked]
    total = sum(weights)
    bids = [0, 0, 0, 0, 0]
    spent = 0
    for index, weight in zip(ranked, weights):
        amount = max(1, budget * weight // total)
        bids[index] = amount
        spent += amount
    bids[ranked[0]] += budget - spent

    pair_floor = max(1, legal.max_amount // Config.AUCTION_PAIR_FRACTION)
    hole_ranks = {rank(card) for card in state.cards}
    for index, card in enumerate(cards):
        if rank(card) in hole_ranks:
            bids[index] = max(bids[index], pair_floor)

    while sum(bids) > legal.max_amount:
        weakest = min((i for i in range(5) if bids[i] > 0), key=lambda i: scores[i])
        bids[weakest] -= 1

    memory["last_auction"] = {"hand": state.hand_number, "cards": list(cards), "bids": list(bids)}
    return Action("bid", cards=tuple(bids))


def auction_budget(state: GameState, best_card_score: float) -> int:
    legal = state.can("bid")
    if legal is None or legal.max_amount <= 0:
        return 0
    balance = legal.max_amount
    equity = preflop_equity(state.cards)

    if equity >= 0.78:
        base = 35
    elif equity >= Config.PREFLOP_OPEN or best_card_score >= 0.38:
        base = 18
    elif equity >= 0.45 or best_card_score >= 0.18:
        base = 8
    else:
        base = 0

    if state.hand_number <= 35:
        base = int(base * 0.55)

    spent = max(0, 1000 - balance)
    progress = min(1.0, state.hand_number / Config.AUCTION_HORIZON)
    target_spent = int(1000 * Config.AUCTION_TARGET * progress)
    deficit = max(0, target_spent - spent)
    catchup = min(Config.AUCTION_CATCHUP_CAP, int(deficit * Config.AUCTION_CATCHUP))
    budget = base + catchup + int(state.pot / max(1, state.blinds.big_blind))

    if state.hand_number < 80 and balance - budget < Config.AUCTION_RESERVE_BEFORE_80:
        budget = max(0, balance - Config.AUCTION_RESERVE_BEFORE_80)
    return max(0, min(balance, budget))


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
    cards = state.cards + state.board
    ranks = [rank(card) for card in cards]
    suits = [suit(card) for card in cards]
    hole_ranks = [rank(card) for card in state.cards]
    counts = sorted((ranks.count(value), value) for value in set(ranks))
    count_values = sorted((count for count, _ in counts), reverse=True)

    flush = max((suits.count(s) for s in set(suits)), default=0) >= 5
    straight = has_straight(ranks)
    if flush and straight:
        made = 0.97
    elif count_values[0] == 4:
        made = 0.95 if max(value for count, value in counts if count == 4) in hole_ranks else 0.76
    elif count_values[0] == 3 and len(count_values) > 1 and count_values[1] >= 2:
        trip = max(value for count, value in counts if count == 3)
        made = 0.91 if trip in hole_ranks else 0.74
    elif flush:
        made = 0.84
    elif straight:
        made = 0.80
    elif count_values[0] == 3:
        trip = max(value for count, value in counts if count == 3)
        made = 0.70 if trip in hole_ranks else 0.45
    elif count_values[0] == 2 and len(count_values) > 1 and count_values[1] == 2:
        pairs = [value for count, value in counts if count == 2]
        own_pairs = sum(1 for value in pairs if value in hole_ranks)
        made = 0.70 if own_pairs >= 2 else 0.62 if own_pairs == 1 else 0.45
    elif count_values[0] == 2:
        pair_rank = max(value for count, value in counts if count == 2)
        if pair_rank not in hole_ranks:
            made = clamp(0.25 + max(hole_ranks, default=0) * 0.014, 0.28, 0.48)
        else:
            made = clamp(0.47 + pair_rank * 0.018, 0.35, 0.70)
    else:
        made = clamp(max(hole_ranks, default=0) * 0.025, 0.08, 0.38)

    draw = draw_score(state.cards, state.board)
    live_penalty = max(0, live_players(state) - 2) * 0.025
    return clamp(made + draw - live_penalty, 0.0, 0.98)


def auction_card_score(card: str, hole: tuple[str, ...]) -> float:
    card_rank = rank(card)
    hole_ranks = [rank(c) for c in hole]
    hole_suits = [suit(c) for c in hole]
    score = max(0.0, (card_rank - 8) * 0.025)
    if card_rank in hole_ranks:
        score += 0.32
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
    if max((suits.count(s) for s in set(suits)), default=0) == 4:
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


def position_open_threshold(state: GameState) -> float:
    players = max(2, state.config.players)
    rel = (state.seat - state.button) % players
    threshold = Config.PREFLOP_OPEN
    if rel == 0:
        threshold -= 0.05
    elif rel in (players - 1, players - 2):
        threshold -= 0.03
    elif rel in (1, 2):
        threshold += 0.03
    if state.hand_number <= 50:
        threshold += 0.03
    return clamp(threshold, 0.46, 0.66)


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
    return encoded if len(encoded) <= Config.MEMORY_LIMIT else b"{}"
