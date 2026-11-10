"""Competitive baseline bot for the Boston poker variant.

The bot is intentionally split into small strategy components so auction logic, betting
logic, opponent tracking, and safety checks can evolve independently. Everything remembered
at a table is serialized through `memory`; no globals are used as table state.
"""

from __future__ import annotations

import json
from typing import Any

import poker_agent_sdk
from poker_agent_sdk import Action, GameState


class Config:
    """Central strategy parameters."""

    VALUE_BET_THRESHOLD = 0.72
    MONSTER_THRESHOLD = 0.86
    CALL_THRESHOLD = 0.55
    DRAW_CALL_THRESHOLD = 0.45
    SEMI_BLUFF_THRESHOLD = 0.48
    POT_ODDS_MARGIN = 0.08
    HIGH_FOLD_TO_RAISE = 0.58

    PREFLOP_OPEN_THRESHOLD = 0.50
    PREFLOP_CALL_THRESHOLD = 0.66
    PREFLOP_PREMIUM_THRESHOLD = 0.78
    PREFLOP_THREE_BET_THRESHOLD = 0.70
    PREFLOP_SHORT_STACK_THRESHOLD = 0.58
    SHORT_STACK_BIG_BLINDS = 10
    TOP3_PUSH_PLAYERS = 5
    TOP3_PUSH_HAND = 70
    TOP3_SHORT_STACK_BIG_BLINDS = 16
    TOP3_SHORT_STACK_THRESHOLD = 0.50
    TOP3_OPEN_DISCOUNT = 0.08
    TOP3_THREE_BET_THRESHOLD = 0.64
    TOP3_VALUE_BET_THRESHOLD = 0.66
    TOP3_SEMI_BLUFF_THRESHOLD = 0.44
    MAX_SPECULATIVE_CALL_PRESSURE = 0.10
    MAX_NORMAL_CALL_PRESSURE = 0.22
    LATE_STREET_VALUE_CALL_THRESHOLD = 0.68

    VALUE_BET_POT_FRACTION = 0.65
    MONSTER_BET_POT_FRACTION = 0.85
    SEMI_BLUFF_POT_FRACTION = 0.50

    AUCTION_BASE_BUDGET = 8
    AUCTION_STRONG_BUDGET = 18
    AUCTION_PREMIUM_BUDGET = 35
    AUCTION_EARLY_HAND_CAP = 35
    AUCTION_EARLY_BUDGET_FACTOR = 0.55
    AUCTION_AGGRO_DISCOUNT = 0.75
    AUCTION_LATE_CARD_NUDGE = 0.02
    AUCTION_PAIR_BID_FRACTION = 40
    BID_FLOP_EQUITY_BOOST = 0.50
    AUCTION_TARGET_SPEND_FRACTION = 0.80
    AUCTION_TARGET_HAND_HORIZON = 130
    AUCTION_CATCHUP_FRACTION = 0.70
    AUCTION_MAX_CATCHUP_BUDGET = 80
    TOP3_AUCTION_TARGET_SPEND_FRACTION = 0.95
    TOP3_AUCTION_MAX_CATCHUP_BUDGET = 140

    MEMORY_VERSION = 1
    MAX_MEMORY_BYTES = 12_000


RANKS = "--23456789TJQKA"
RANK_VALUE = {rank: value for value, rank in enumerate(RANKS) if value >= 2}


class Agent(poker_agent_sdk.Agent):
    def act(self, state: GameState, memory: bytes) -> tuple[Action, bytes]:
        decoded = MemoryCodec.decode(memory)
        action, updated = StrategyEngine().handle_turn(state, decoded)
        return action, MemoryCodec.encode(updated)


class StrategyEngine:
    """Routes each engine phase to the correct strategy module."""

    def __init__(self) -> None:
        self.evaluator = HandEvaluator()
        self.tracker = OpponentTracker()
        self.safety = ActionSafety()

    def handle_turn(self, state: GameState, memory: dict[str, Any]) -> tuple[Action, dict[str, Any]]:
        memory = self.tracker.update(state, memory)
        try:
            if state.phase == "auction":
                action = self._handle_auction(state, memory)
            elif state.phase == "betting":
                action = self._handle_betting(state, memory)
            else:
                action = self.safety.fallback(state)
        except Exception:
            action = self.safety.fallback(state)
        return self.safety.ensure_legal(state, action), memory

    def _handle_auction(self, state: GameState, memory: dict[str, Any]) -> Action:
        legal = state.can("bid")
        auction_cards = tuple(state.variant.get("auction_cards") or ())
        if legal is None or len(auction_cards) != 5 or legal.max_amount <= 0:
            return Action("bid", cards=(0, 0, 0, 0, 0))

        equity = self.evaluator.preflop_equity(state.cards)
        card_scores = [
            self.evaluator.auction_card_value(card, state.cards, equity)
            for card in auction_cards
        ]

        budget = self._auction_budget(state, memory, equity, max(card_scores))
        budget = max(0, min(legal.max_amount, budget))
        if budget <= 0:
            return Action("bid", cards=(0, 0, 0, 0, 0))

        # Earlier cards win ties, so later valuable cards need only a tiny extra help.
        indexed = sorted(
            range(5),
            key=lambda i: (
                card_scores[i] + (Config.AUCTION_LATE_CARD_NUDGE if i >= 3 else 0.0),
                -i,
            ),
            reverse=True,
        )
        targets = indexed[:3]
        weights = [max(1, int(card_scores[i] * 100)) for i in targets]
        total_weight = sum(weights)

        bids = [0, 0, 0, 0, 0]
        spent = 0
        for index, weight in zip(targets, weights):
            amount = max(1, budget * weight // total_weight)
            bids[index] = amount
            spent += amount

        bids[targets[0]] += budget - spent
        self._apply_pair_bid_floor(state, auction_cards, bids, legal.max_amount)
        while sum(bids) > legal.max_amount:
            weakest = min((i for i in range(5) if bids[i] > 0), key=lambda i: card_scores[i])
            bids[weakest] -= 1

        action = Action("bid", cards=tuple(int(bid) for bid in bids))
        memory["last_auction"] = {
            "hand": state.hand_number,
            "cards": list(auction_cards),
            "bids": list(action.cards),
        }
        return action

    def _handle_betting(self, state: GameState, memory: dict[str, Any]) -> Action:
        if not state.board:
            return self._handle_preflop(state)

        top3_push = self._top3_push_mode(state)
        value_threshold = (
            Config.TOP3_VALUE_BET_THRESHOLD if top3_push else Config.VALUE_BET_THRESHOLD
        )
        semi_bluff_threshold = (
            Config.TOP3_SEMI_BLUFF_THRESHOLD if top3_push else Config.SEMI_BLUFF_THRESHOLD
        )
        equity = self.evaluator.equity_estimate(state)
        equity = self._boost_for_won_auction_cards(state, memory, equity)
        pot_odds = PotOddsMath.pot_odds(state)
        to_call = max(0, state.to_call)

        if raise_ := state.can("raise"):
            fold_to_raise = self.tracker.table_fold_to_raise(memory)
            semi_bluff = (
                equity >= semi_bluff_threshold
                and fold_to_raise >= Config.HIGH_FOLD_TO_RAISE
                and self.evaluator.has_draw(state.cards, state.board)
            )
            if equity >= value_threshold or semi_bluff:
                fraction = self._bet_fraction(equity, semi_bluff)
                target = PotOddsMath.raise_amount(state, raise_, fraction)
                if target is not None:
                    return Action.raise_to(target)

        if state.can("check"):
            return Action.check()

        if state.can("call") and self._should_call(state, equity, pot_odds, to_call):
            return Action.call()

        return Action.fold() if state.can("fold") else self.safety.fallback(state)

    def _handle_preflop(self, state: GameState) -> Action:
        top3_push = self._top3_push_mode(state)
        equity = self.evaluator.preflop_equity(state.cards)
        open_threshold = self._position_adjusted_open_threshold(state, top3_push)
        three_bet_threshold = (
            Config.TOP3_THREE_BET_THRESHOLD if top3_push else Config.PREFLOP_THREE_BET_THRESHOLD
        )
        short_stack_bb = (
            Config.TOP3_SHORT_STACK_BIG_BLINDS if top3_push else Config.SHORT_STACK_BIG_BLINDS
        )
        short_stack_threshold = (
            Config.TOP3_SHORT_STACK_THRESHOLD
            if top3_push
            else Config.PREFLOP_SHORT_STACK_THRESHOLD
        )
        big_blind = max(1, state.blinds.big_blind)
        stack = PotOddsMath.own_stack(state)
        stack_bb = stack / big_blind
        to_call = max(0, state.to_call)
        pressure = to_call / max(1, stack + to_call)

        if stack_bb <= short_stack_bb:
            if raise_ := state.can("raise"):
                if equity >= short_stack_threshold:
                    return Action.raise_to(raise_.max_amount)
            if state.can("check"):
                return Action.check()
            if state.can("call") and equity >= Config.PREFLOP_PREMIUM_THRESHOLD:
                return Action.call()
            return Action.fold() if state.can("fold") else self.safety.fallback(state)

        if raise_ := state.can("raise"):
            if to_call <= 0 and equity >= open_threshold:
                target = PotOddsMath.preflop_open_amount(state, raise_)
                return Action.raise_to(target)
            if to_call > 0 and equity >= three_bet_threshold:
                target = PotOddsMath.preflop_reraise_amount(state, raise_)
                return Action.raise_to(target)

        if state.can("check"):
            return Action.check()

        if state.can("call") and self._should_call_preflop(equity, pressure):
            return Action.call()

        return Action.fold() if state.can("fold") else self.safety.fallback(state)

    def _position_adjusted_open_threshold(self, state: GameState, top3_push: bool) -> float:
        threshold = Config.PREFLOP_OPEN_THRESHOLD
        players = max(2, state.config.players)
        relative_to_button = (state.seat - state.button) % players
        if relative_to_button == 0:
            threshold -= 0.05
        elif relative_to_button in (players - 1, players - 2):
            threshold -= 0.03
        elif relative_to_button in (1, 2):
            threshold += 0.03
        if top3_push:
            threshold -= Config.TOP3_OPEN_DISCOUNT
        return clamp(threshold, 0.46, 0.64)

    def _auction_budget(
        self,
        state: GameState,
        memory: dict[str, Any],
        equity: float,
        best_card_score: float,
    ) -> int:
        catchup = self._auction_catchup_budget(state)
        if equity >= Config.PREFLOP_PREMIUM_THRESHOLD:
            budget = Config.AUCTION_PREMIUM_BUDGET
        elif equity >= Config.PREFLOP_OPEN_THRESHOLD or best_card_score >= 0.38:
            budget = Config.AUCTION_STRONG_BUDGET
        elif equity >= Config.PREFLOP_CALL_THRESHOLD or best_card_score >= 0.25:
            budget = Config.AUCTION_BASE_BUDGET
        else:
            return catchup if best_card_score >= 0.08 else 0

        budget += int(max(0, state.pot) / max(1, state.blinds.big_blind))
        if state.hand_number <= Config.AUCTION_EARLY_HAND_CAP:
            budget = int(budget * Config.AUCTION_EARLY_BUDGET_FACTOR)
        if self.tracker.table_auction_aggression(memory) > 0.35:
            budget = int(budget * Config.AUCTION_AGGRO_DISCOUNT)
        budget += catchup
        return max(0, budget)

    def _auction_catchup_budget(self, state: GameState) -> int:
        legal = state.can("bid")
        if legal is None or legal.max_amount <= 0:
            return 0
        starting_balance = 1000
        current_balance = legal.max_amount
        spent = max(0, starting_balance - current_balance)
        horizon = min(state.config.hand_cap, Config.AUCTION_TARGET_HAND_HORIZON)
        progress = min(1.0, max(0.0, state.hand_number / max(1, horizon)))
        target_fraction = Config.AUCTION_TARGET_SPEND_FRACTION
        max_catchup = Config.AUCTION_MAX_CATCHUP_BUDGET
        if self._top3_push_mode(state):
            target_fraction = Config.TOP3_AUCTION_TARGET_SPEND_FRACTION
            max_catchup = Config.TOP3_AUCTION_MAX_CATCHUP_BUDGET
        target_spent = int(starting_balance * target_fraction * progress)
        deficit = max(0, target_spent - spent)
        catchup = int(deficit * Config.AUCTION_CATCHUP_FRACTION)
        return min(max_catchup, catchup, current_balance)

    def _top3_push_mode(self, state: GameState) -> bool:
        if state.hand_number >= Config.TOP3_PUSH_HAND:
            return True
        live_players = sum(1 for seat in state.seats if seat.status in ("active", "all_in"))
        return 0 < live_players <= Config.TOP3_PUSH_PLAYERS

    def _bet_fraction(self, equity: float, semi_bluff: bool) -> float:
        if semi_bluff:
            return Config.SEMI_BLUFF_POT_FRACTION
        if equity >= Config.MONSTER_THRESHOLD:
            return Config.MONSTER_BET_POT_FRACTION
        return Config.VALUE_BET_POT_FRACTION

    def _should_call(self, state: GameState, equity: float, pot_odds: float, to_call: int) -> bool:
        if to_call <= 0:
            return False
        stack_pressure = to_call / max(1, PotOddsMath.own_stack(state) + to_call)
        if len(state.board) >= 4 and equity < Config.LATE_STREET_VALUE_CALL_THRESHOLD:
            return pot_odds <= 0.08 and stack_pressure <= Config.MAX_SPECULATIVE_CALL_PRESSURE
        if stack_pressure > Config.MAX_NORMAL_CALL_PRESSURE:
            return equity >= Config.VALUE_BET_THRESHOLD and equity + Config.POT_ODDS_MARGIN >= pot_odds
        if equity >= Config.CALL_THRESHOLD and equity + Config.POT_ODDS_MARGIN >= pot_odds:
            return stack_pressure <= Config.MAX_NORMAL_CALL_PRESSURE or equity >= Config.VALUE_BET_THRESHOLD
        if self.evaluator.has_draw(state.cards, state.board):
            return (
                equity >= Config.DRAW_CALL_THRESHOLD
                and equity + 0.14 >= pot_odds
                and stack_pressure <= Config.MAX_SPECULATIVE_CALL_PRESSURE
            )
        return False

    def _should_call_preflop(self, equity: float, pressure: float) -> bool:
        if equity >= Config.PREFLOP_PREMIUM_THRESHOLD:
            return pressure <= Config.MAX_NORMAL_CALL_PRESSURE
        if equity >= Config.PREFLOP_CALL_THRESHOLD:
            return pressure <= Config.MAX_SPECULATIVE_CALL_PRESSURE * 0.75
        return False

    def _apply_pair_bid_floor(
        self,
        state: GameState,
        auction_cards: tuple[str, ...],
        bids: list[int],
        max_total: int,
    ) -> None:
        pair_floor = max(1, max_total // Config.AUCTION_PAIR_BID_FRACTION)
        hole_ranks = {rank(card) for card in state.cards}
        for index, card in enumerate(auction_cards):
            if rank(card) in hole_ranks:
                bids[index] = max(bids[index], pair_floor)

    def _boost_for_won_auction_cards(
        self,
        state: GameState,
        memory: dict[str, Any],
        equity: float,
    ) -> float:
        if len(state.board) < 3:
            return equity
        last = memory.get("last_auction")
        if not isinstance(last, dict) or last.get("hand") != state.hand_number:
            return equity

        cards = last.get("cards")
        bids = last.get("bids")
        if not isinstance(cards, list) or not isinstance(bids, list):
            return equity

        bid_on = {card for card, bid in zip(cards, bids) if int(bid) > 0}
        if not bid_on:
            return equity
        hits = sum(1 for card in state.board[:3] if card in bid_on)
        if hits <= 0:
            return equity
        boost = 1.0 + Config.BID_FLOP_EQUITY_BOOST * min(1.0, hits / 3)
        return clamp(equity * boost, 0.0, 0.98)


class HandEvaluator:
    """Fast heuristic evaluator. Scores are equity-like values in [0.0, 1.0]."""

    def equity_estimate(self, state: GameState) -> float:
        if not state.board:
            return self.preflop_equity(state.cards)
        made = self.made_hand_score(state.cards, state.board)
        draw = self.draw_score(state.cards, state.board)
        players_penalty = max(0, self._live_players(state) - 2) * 0.025
        return clamp(made + draw - players_penalty, 0.0, 0.98)

    def preflop_equity(self, cards: tuple[str, ...]) -> float:
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

    def made_hand_score(self, hole: tuple[str, ...], board: tuple[str, ...]) -> float:
        cards = hole + board
        ranks = [rank(card) for card in cards]
        suits = [suit(card) for card in cards]
        counts = sorted((ranks.count(value), value) for value in set(ranks))
        count_values = sorted((count for count, _ in counts), reverse=True)
        hole_ranks = [rank(card) for card in hole]
        board_ranks = [rank(card) for card in board]

        flush = max((suits.count(s) for s in set(suits)), default=0) >= 5
        straight = has_straight(ranks)
        if flush and straight:
            return 0.97
        if count_values[0] == 4:
            quad_rank = max(value for count, value in counts if count == 4)
            return 0.95 if quad_rank in hole_ranks else 0.76
        if count_values[0] == 3 and len(count_values) > 1 and count_values[1] >= 2:
            trip_rank = max(value for count, value in counts if count == 3)
            return 0.91 if trip_rank in hole_ranks else 0.74
        if flush:
            return 0.84
        if straight:
            return 0.80
        if count_values[0] == 3:
            trip_rank = max(value for count, value in counts if count == 3)
            if trip_rank in hole_ranks:
                if hole_ranks.count(trip_rank) == 2:
                    return 0.84
                return 0.74
            return clamp(0.36 + max(hole_ranks, default=0) * 0.012, 0.36, 0.55)
        if count_values[0] == 2 and len(count_values) > 1 and count_values[1] == 2:
            paired_ranks = [value for count, value in counts if count == 2]
            own_pairs = sum(1 for value in paired_ranks if value in hole_ranks)
            if own_pairs >= 2:
                return 0.70
            if own_pairs == 1:
                return 0.62
            return clamp(0.36 + max(hole_ranks, default=0) * 0.010, 0.36, 0.52)
        if count_values[0] == 2:
            pair_rank = max(value for count, value in counts if count == 2)
            top_board = max((rank(card) for card in board), default=0)
            kicker = max((rank(card) for card in hole), default=0)
            if pair_rank not in hole_ranks:
                return clamp(0.25 + kicker * 0.014, 0.28, 0.48)
            base = 0.39 + pair_rank * 0.018 + kicker * 0.006
            if pair_rank >= top_board:
                base += 0.08
            return clamp(base, 0.35, 0.68)
        if board_ranks and max(hole_ranks, default=0) < max(board_ranks):
            return clamp(max(hole_ranks, default=0) * 0.018, 0.08, 0.30)
        return clamp(max(ranks) * 0.025, 0.08, 0.40)

    def draw_score(self, hole: tuple[str, ...], board: tuple[str, ...]) -> float:
        cards = hole + board
        ranks = [rank(card) for card in cards]
        suits = [suit(card) for card in cards]
        score = 0.0
        if max((suits.count(s) for s in set(suits)), default=0) == 4:
            score += 0.13
        score += straight_draw_score(ranks)
        return score

    def has_draw(self, hole: tuple[str, ...], board: tuple[str, ...]) -> bool:
        return bool(board) and self.draw_score(hole, board) >= 0.08

    def auction_card_value(self, card: str, hole: tuple[str, ...], preflop_equity: float) -> float:
        card_rank = rank(card)
        hole_ranks = [rank(hole_card) for hole_card in hole]
        hole_suits = [suit(hole_card) for hole_card in hole]
        value = max(0.0, (card_rank - 8) * 0.025)

        if card_rank in hole_ranks:
            value += 0.30
        if suit(card) in hole_suits:
            value += 0.10
        for hole_rank in hole_ranks:
            gap = abs(card_rank - hole_rank)
            if gap == 1:
                value += 0.08
            elif gap == 2:
                value += 0.04
        if preflop_equity >= Config.PREFLOP_PREMIUM_THRESHOLD and card_rank <= 9:
            value += 0.06
        return clamp(value, 0.0, 0.75)

    def _live_players(self, state: GameState) -> int:
        return sum(1 for seat in state.seats if seat.status in ("active", "all_in"))


class OpponentTracker:
    """Updates lightweight per-table opponent stats in memory."""

    def update(self, state: GameState, memory: dict[str, Any]) -> dict[str, Any]:
        memory.setdefault("version", Config.MEMORY_VERSION)
        memory.setdefault("opponents", {})
        memory.setdefault("last_flop_chips", {})

        opponents = memory["opponents"]
        for event in state.events:
            if event.seat is None or event.seat == state.seat:
                continue
            seat_key = str(event.seat)
            stats = opponents.setdefault(seat_key, self._new_stats())
            if event.kind == "call":
                stats["calls"] += 1
                stats["vpip"] += 1
            elif event.kind == "raise":
                stats["raises"] += 1
                stats["vpip"] += 1
                if not state.board:
                    stats["pfr"] += 1
            elif event.kind == "fold":
                stats["folds"] += 1

        self._update_auction_spend(state, memory)
        memory["hand_number"] = state.hand_number
        return memory

    def table_fold_to_raise(self, memory: dict[str, Any]) -> float:
        folds = 0
        actions = 0
        for stats in memory.get("opponents", {}).values():
            folds += stats.get("folds", 0)
            actions += stats.get("folds", 0) + stats.get("calls", 0) + stats.get("raises", 0)
        return folds / actions if actions else 0.0

    def table_auction_aggression(self, memory: dict[str, Any]) -> float:
        spend = 0
        observations = 0
        for stats in memory.get("opponents", {}).values():
            spend += stats.get("auction_spend", 0)
            observations += stats.get("auction_observations", 0)
        return spend / max(1, observations * 1000)

    def _update_auction_spend(self, state: GameState, memory: dict[str, Any]) -> None:
        balances = state.variant.get("flop_chips_by_seat")
        if not isinstance(balances, list):
            return

        previous = memory.get("last_flop_chips", {})
        opponents = memory.setdefault("opponents", {})
        current = {}
        for index, balance in enumerate(balances):
            current[str(index)] = int(balance)
            if index == state.seat:
                continue
            old = previous.get(str(index))
            if old is not None and old > balance:
                stats = opponents.setdefault(str(index), self._new_stats())
                stats["auction_spend"] += int(old - balance)
                stats["auction_observations"] += 1
        memory["last_flop_chips"] = current

    def _new_stats(self) -> dict[str, int]:
        return {
            "vpip": 0,
            "pfr": 0,
            "calls": 0,
            "raises": 0,
            "folds": 0,
            "auction_spend": 0,
            "auction_observations": 0,
        }


class PotOddsMath:
    @staticmethod
    def pot_odds(state: GameState) -> float:
        cost = max(0, state.to_call)
        return cost / max(1, state.pot + cost)

    @staticmethod
    def raise_amount(state: GameState, legal: Any, pot_fraction: float) -> int | None:
        own_bet = PotOddsMath.own_bet(state)
        call_total = own_bet + max(0, state.to_call)
        desired = call_total + int(max(1, state.pot) * pot_fraction)
        amount = max(legal.min_amount, min(legal.max_amount, desired))
        if amount < legal.min_amount or amount > legal.max_amount:
            return None
        return int(amount)

    @staticmethod
    def preflop_open_amount(state: GameState, legal: Any) -> int:
        big_blind = max(1, state.blinds.big_blind)
        desired = max(legal.min_amount, 3 * big_blind)
        return int(max(legal.min_amount, min(legal.max_amount, desired)))

    @staticmethod
    def preflop_reraise_amount(state: GameState, legal: Any) -> int:
        big_blind = max(1, state.blinds.big_blind)
        own_bet = PotOddsMath.own_bet(state)
        max_bet = max((seat.bet for seat in state.seats), default=0)
        call_total = own_bet + max(0, state.to_call)
        desired = max(legal.min_amount, 3 * max_bet, call_total + 4 * big_blind)
        return int(max(legal.min_amount, min(legal.max_amount, desired)))

    @staticmethod
    def own_stack(state: GameState) -> int:
        for seat in state.seats:
            if seat.seat == state.seat:
                return int(seat.stack)
        return 0

    @staticmethod
    def own_bet(state: GameState) -> int:
        for seat in state.seats:
            if seat.seat == state.seat:
                return int(seat.bet)
        return 0


class ActionSafety:
    """Final legality gate for every emitted action."""

    def ensure_legal(self, state: GameState, action: Action) -> Action:
        if action.kind == "raise":
            legal = state.can("raise")
            if legal is None:
                return self.fallback(state)
            return Action.raise_to(max(legal.min_amount, min(legal.max_amount, action.amount)))

        if action.kind == "bid":
            legal = state.can("bid")
            if legal is None:
                return self.fallback(state)
            bids = tuple(max(0, int(bid)) for bid in action.cards[:5])
            bids = bids + (0,) * (5 - len(bids))
            if sum(bids) > legal.max_amount:
                bids = self._scale_bids(bids, legal.max_amount)
            return Action("bid", cards=bids)

        if action.kind == "call" and state.can("check"):
            return Action.check()
        if state.can(action.kind):
            return action
        return self.fallback(state)

    def fallback(self, state: GameState) -> Action:
        if state.can("check"):
            return Action.check()
        if state.can("fold"):
            return Action.fold()
        first = state.legal_actions[0]
        return Action(first.kind, first.min_amount)

    def _scale_bids(self, bids: tuple[int, ...], max_total: int) -> tuple[int, ...]:
        total = sum(bids)
        if total <= max_total:
            return bids
        if max_total <= 0 or total <= 0:
            return (0, 0, 0, 0, 0)
        scaled = [bid * max_total // total for bid in bids]
        while sum(scaled) > max_total:
            index = max(range(5), key=lambda i: scaled[i])
            scaled[index] -= 1
        while sum(scaled) < max_total:
            index = max(range(5), key=lambda i: bids[i] - scaled[i])
            scaled[index] += 1
        return tuple(scaled)


class MemoryCodec:
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
            encoded = json.dumps(memory, separators=(",", ":")).encode()
        except Exception:
            return b"{}"
        if len(encoded) <= Config.MAX_MEMORY_BYTES:
            return encoded
        compact = {
            "version": Config.MEMORY_VERSION,
            "hand_number": memory.get("hand_number", 0),
            "last_flop_chips": memory.get("last_flop_chips", {}),
        }
        return json.dumps(compact, separators=(",", ":")).encode()


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


def straight_draw_score(ranks: list[int]) -> float:
    values = set(ranks)
    if 14 in values:
        values.add(1)
    best = 0
    for start in range(1, 11):
        best = max(best, sum(1 for value in range(start, start + 5) if value in values))
    if best >= 4:
        return 0.10
    if best == 3:
        return 0.035
    return 0.0


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
