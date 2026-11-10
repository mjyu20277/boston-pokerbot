"""No-limit hold'em whose flop is bought at a sealed auction, with flop-chips kept per table.

After preflop betting, if two or more players are still in, five auction cards are dealt face up.
Every one of them bids flop-chips on each card. The three best-scoring cards become the flop and
the other two are dead. Everyone who bid on a selected card pays that bid; paid flop-chips are
destroyed. Bids and scores are never shown to bots: the organizers' hand log keeps them as
sealed events. Every seat's balance is public, during play and in each hand's record. Payments
happen only once every bid is in, so a balance never shows a bid of the auction under way.
"""

import random
from collections import deque
from dataclasses import replace
from itertools import combinations
from typing import Any

from poker_agent_protocol.game import Action, BlindLevel, LegalAction
from pokerkit import Card, Folding, StandardHighHand

from poker_engine.holdem import DECK, HoldemHand, runout_strengths
from poker_engine.variant import Hand, HandEvent, HandView
from poker_engine.variants._sealed import SealedLog

FLOP_CHIPS = 1000  # Each seat's balance at the start of every table.
AUCTION_CARDS = 5
FLOP_CARDS = 3
SHARE = 2520  # A runout's pot, divisible among any one to ten winners.
MAX_HANDS = 1000  # The most hands a table can play (the API's largest hand cap).


class FlopChipsLog(SealedLog):
    """Each seat's flop-chips, kept across the hands of a table, and the sealed hand log."""

    def __init__(self, players: int) -> None:
        self.balances = [FLOP_CHIPS] * players
        self.hands = 0

    def deal_options(self, seats: tuple[int, ...]) -> dict[str, Any]:
        return {
            "carry": tuple(self.balances[seat] for seat in seats),
            "seats": seats,
            "table_balances": tuple(self.balances),
        }

    def hand_over(self, hand: Hand, seats: tuple[int, ...]) -> dict[str, Any]:
        assert isinstance(hand, FlopAuctionHand)
        self.hands += 1
        before = list(self.balances)
        emptied = []
        for position, balance in enumerate(hand.carry()):
            if balance == 0 < self.balances[seats[position]]:
                emptied.append(seats[position])
            self.balances[seats[position]] = balance
        logged = super().hand_over(hand, seats)
        # A knockout split K ways is 1/K to each winner, which no whole-chip event can carry.
        # Equal Killer totals are ordered by later rounds first (`_countback`).
        logged["sealed"] += [
            {"kind": "prize_killer_countback", "seat": seats[player], "amount": share}
            for player, share in hand.knockouts()
        ]
        # High Roller's second tiebreak, the earliest hand a seat's flop-chips ran out: as the
        # largest of hands left in a table of `MAX_HANDS`, so the earliest is the largest.
        logged["sealed"] += [
            {
                "kind": "prize_high_roller_tiebreak2_max",
                "seat": seat,
                "amount": MAX_HANDS + 1 - self.hands,
            }
            for seat in emptied
        ]
        return {
            "flop_chips_before": before,
            "flop_chips_after": list(self.balances),
            **logged,
        }


class FlopAuctionHoldem:
    name = "flop_auction_holdem"
    title = "Boston"
    public_id = "boston"
    max_players = 10
    extension = FlopChipsLog
    # Speed Demon: the lowest median answer time over at least 100 bids and actions. The
    # worker counts each seat's measured answers under this prefix, in microseconds. Timeouts
    # are not answers, so they are not counted.
    response_prize = "prize_speed_demon_low100"

    def deal(
        self,
        *,
        stacks: tuple[int, ...],
        blinds: BlindLevel,
        rng: random.Random,
        carry: tuple[int, ...] | None = None,
        seats: tuple[int, ...] | None = None,
        table_balances: tuple[int, ...] | None = None,
    ) -> "FlopAuctionHand":
        balances = carry if carry is not None else (FLOP_CHIPS,) * len(stacks)
        # Without a table, positions are seats and every seat is in the hand.
        return FlopAuctionHand(
            stacks=stacks,
            blinds=blinds,
            rng=rng,
            flop_chips=balances,
            seats=seats if seats is not None else tuple(range(len(stacks))),
            table_balances=table_balances if table_balances is not None else balances,
        )


def flop_order(bids: dict[int, tuple[int, ...]]) -> list[int]:
    """Auction card indexes, best first. Higher score wins; equal scores go to the card dealt
    first."""
    scores = [sum(bid[card] for bid in bids.values()) for card in range(AUCTION_CARDS)]
    return sorted(range(AUCTION_CARDS), key=lambda card: (-scores[card], card))


class FlopAuctionHand(HoldemHand):
    def __init__(
        self,
        *,
        stacks: tuple[int, ...],
        blinds: BlindLevel,
        rng: random.Random,
        flop_chips: tuple[int, ...],
        seats: tuple[int, ...],
        table_balances: tuple[int, ...],
    ) -> None:
        super().__init__(stacks=stacks, blinds=blinds, rng=rng)
        self._rng = rng
        self._start = stacks
        self._balances = list(flop_chips)
        self._seats = seats
        self._table_balances = table_balances  # By seat, for seats not in this hand.
        self._auction: list[Card] = []
        self._bidding: deque[int] = deque()
        self._bids: dict[int, tuple[int, ...]] = {}
        self._paid: list[int | None] = [None] * len(stacks)
        self._sealed: list[HandEvent] = []
        self._knockouts: list[tuple[int, float]] = []
        self._sealed_settled = False

    def actor(self) -> int | None:
        state = self._state
        while True:
            if self._bidding:
                return self._bidding[0]
            if (
                not self._auction
                and not state.can_deal_hole()
                and not state.board_cards
                and state.actor_index is None
                and sum(state.statuses) > 1
                and (state.can_burn_card() or state.showdown_index is not None)
            ):
                self._open_auction()
            elif state.can_deal_hole():
                state.deal_hole()
            elif state.can_burn_card():
                state.burn_card()
            elif state.can_deal_board():
                state.deal_board()
            elif state.showdown_index is not None:
                state.show_or_muck_hole_cards(True)
            else:
                return state.actor_index

    def legal_actions(self) -> tuple[LegalAction, ...]:
        if self._bidding:
            # The range is the total a bid may spend across the five cards.
            return (LegalAction("bid", 0, self._balances[self._bidding[0]]),)
        return super().legal_actions()

    def apply(self, action: Action) -> None:
        if not self._bidding:
            super().apply(action)
            return
        player = self._bidding[0]
        bid = action.cards or (0,) * AUCTION_CARDS
        if (
            action.kind != "bid"
            or action.amount
            or len(bid) != AUCTION_CARDS
            or any(amount < 0 for amount in bid)
            or sum(bid) > self._balances[player]
        ):
            raise ValueError("Bid five whole numbers of flop-chips, one per auction card.")
        self._bidding.popleft()
        self._bids[player] = bid
        self.events()
        self._events.append(HandEvent("bid", player))  # Never the amounts.
        if not self._bidding:
            self._resolve()

    def view(self, player: int) -> HandView:
        view = super().view(player)
        by_seat = list(self._table_balances)
        for position, seat in enumerate(self._seats):
            by_seat[seat] = self._balances[position]
        variant: dict[str, object] = {
            "flop_chips": self._balances[player],
            "flop_chips_by_seat": by_seat,
        }
        if self._auction:
            variant["auction_cards"] = [repr(card) for card in self._auction]
        if self._paid[player] is not None:
            variant["flop_chips_paid"] = self._paid[player]
        if self._bidding:
            return replace(view, phase="auction", to_call=0, variant=variant)
        return replace(view, variant=variant)

    def carry(self) -> tuple[int, ...]:
        return tuple(self._balances)

    def sealed(self) -> tuple[HandEvent, ...]:
        self.events()
        if not self._state.status and not self._sealed_settled:
            self._sealed_settled = True
            self._settle_prizes()
            for player, balance in enumerate(self._balances):
                self._sealed.append(HandEvent("flop_chips", player, balance))
        return tuple(self._sealed)

    def knockouts(self) -> list[tuple[int, float]]:
        """(Killer, share) for each knockout this hand, once it is over."""
        self.sealed()
        return self._knockouts

    def _open_auction(self) -> None:
        state = self._state
        self.events()
        self._auction = [state.deck_cards.popleft() for _ in range(AUCTION_CARDS)]
        cards = tuple(repr(card) for card in self._auction)
        self._events.append(HandEvent("auction", cards=cards))
        self._bidding.extend(i for i in state.player_indices if state.statuses[i])

    def _resolve(self) -> None:
        # Ties no longer use these five draws. Taking them keeps the seed's later cards unchanged.
        for _ in range(AUCTION_CARDS):
            self._rng.random()
        order = flop_order(self._bids)
        selected, dead = sorted(order[:FLOP_CARDS]), sorted(order[FLOP_CARDS:])
        for player, bid in self._bids.items():
            paid = sum(bid[card] for card in selected)
            self._balances[player] -= paid
            self._paid[player] = paid
            for card, amount in enumerate(bid):
                if amount:
                    self._sealed.append(
                        HandEvent("bid", player, amount, (repr(self._auction[card]),))
                    )
            if paid:
                self._sealed.append(HandEvent("flop_chips_paid", player, paid))
        # The flop, in auction order, then the turn and river from the rest of the deck. The
        # dead cards are burned before the flop and the turn, so neither can come back.
        cards = self._auction
        self._state.deck_cards.extendleft(
            reversed([cards[dead[0]], *(cards[i] for i in selected), cards[dead[1]]])
        )

    def _settle_prizes(self) -> None:
        state = self._state
        winners = {event.player for event in self._events if event.kind == "win"}
        for player, bid in self._bids.items():
            if not any(bid) and player in winners:
                self._sealed.append(HandEvent("prize_nothing_to_see_here", player))
            paid = self._paid[player] or 0
            if paid and winners - {player}:
                self._sealed.append(HandEvent("prize_kingmaker", player))
            # One count per auction at the amount paid, so the median survives summing, and the
            # total paid, High Roller's first tiebreak.
            self._sealed.append(HandEvent(f"prize_high_roller_top40_{paid}", player))
            if paid:  # An event's amount of 0 is not logged, and would count as 1.
                self._sealed.append(HandEvent("prize_high_roller_tiebreak1", player, paid))
        top = max(state.stacks)
        if list(state.stacks).count(top) == 1:
            self._sealed.append(HandEvent("prize_longest_reign", list(state.stacks).index(top)))
        put, won, live = self._settlement()
        self._knockouts = self._knocked_out(put, live)
        for player, lost in self._tough_losses(put, won, live):
            self._sealed.append(HandEvent("prize_tough_loss_max", player, lost))

    def _settlement(self) -> tuple[list[int], list[int], list[int]]:
        """What each player put in and won, and who did not fold. PokerKit merges pots once
        losing hands are mucked, so pots are rebuilt from what each player put in."""
        state, players = self._state, range(len(self._start))
        folded = {op.player_index for op in state.operations if isinstance(op, Folding)}
        won = [0] * len(self._start)
        for event in self._events:
            if event.kind == "win" and event.player is not None:
                won[event.player] += event.amount
        put = [self._start[p] - state.stacks[p] + won[p] for p in players]
        return put, won, [p for p in players if p not in folded]

    def _knocked_out(self, put: list[int], live: list[int]) -> list[tuple[int, float]]:
        """For each busted player, the winners of the pot holding its last chips: the pot of
        every player who put in at least as much. K winners get 1/K of the knockout each."""
        state = self._state
        if all(state.stacks):
            return []
        board = "".join(repr(dealt[0]) for dealt in state.board_cards)
        hands = {p: StandardHighHand.from_game("".join(self._dealt[p]), board) for p in live}
        knockouts: list[tuple[int, float]] = []
        for busted in (p for p in live if not state.stacks[p]):
            rivals = [p for p in live if put[p] >= put[busted] and p != busted]
            if not rivals:
                continue
            best = max(hands[p] for p in rivals)
            winners = [p for p in rivals if hands[p] == best]
            knockouts += [(p, 1 / len(winners)) for p in winners]
        return knockouts

    def _tough_losses(
        self, put: list[int], won: list[int], live: list[int]
    ) -> list[tuple[int, int]]:
        """Showdown players who won nothing while holding the best equity among the showdown
        players once the flop was bought, with the pot they lost: what they could have won,
        each player's chips up to their own. Equity is a share of the pot over every turn and
        river, with every dealt hole card and the auction cards known and burns unseen."""
        showdown = [p for p in live if p in self._bids]
        losers = [p for p in showdown if not won[p]]
        if len(showdown) < 2 or not losers:
            return []
        flop = tuple(dealt[0] for dealt in self._state.board_cards[:FLOP_CARDS])
        dealt = [tuple(Card.clean("".join(cards))) for cards in self._dealt]
        known = {*self._auction, *(card for cards in dealt for card in cards)}
        rest = [card for card in DECK if card not in known]
        shares = dict.fromkeys(showdown, 0)
        hands = [(*dealt[p], *flop) for p in showdown]
        for ranks in runout_strengths(hands, combinations(rest, 2)):
            best = max(ranks)
            winners = [p for p, rank in zip(showdown, ranks, strict=True) if rank == best]
            for p in winners:
                shares[p] += SHARE // len(winners)
        most = max(shares.values())
        return [(p, sum(min(chips, put[p]) for chips in put)) for p in losers if shares[p] == most]


VARIANT = FlopAuctionHoldem
