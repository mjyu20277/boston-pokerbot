# Boston

Standard no-limit hold'em with two private cards, except that the flop is bought at a sealed auction paid in a separate currency, flop-chips.

## Rules

- Every bot starts each table with 1,000 flop-chips. They are used only for bidding, carry over from hand to hand at that table, are never added to, and are never worth points. They never enter a pot.
- Play normal preflop betting. If only one player is left, the hand ends: no auction cards are dealt and no flop-chips move.
- Otherwise, five auction cards are dealt face up, numbered 0 to 4 in dealing order. Every player still in the hand bids, including all-in players. There is no betting, folding, or other action during the auction.
- A bid is five whole numbers of flop-chips, one per auction card, adding up to at most your balance. A bid of 0 on a card is the same as not bidding on it. Spending your whole balance is allowed.
- Each card's score is the total bid on it by all players. The three highest-scoring cards become the flop, shown in auction order. The other two are dead for the rest of the hand and can never be the turn or river.
- Ties between equal scores go to the card dealt first: card 0 beats card 1, and so on. If no card gets a bid, the flop is cards 0, 1 and 2.
- Every player pays what they bid on each selected card. Bids on the two dead cards cost nothing. Paid flop-chips are destroyed, never refunded, even if you fold later or lose the hand.
- A bid can only help the card it is placed on: there is no negative bid or veto. To keep a card off the flop, you must outbid it on three other cards.
- Turn, river, betting, side pots and showdown follow the hold'em reference. Every player who reaches showdown automatically reveals both hole cards; there is no mucking. Folded hands and a winner whose opponents all folded stay hidden.
- When all-in betting is complete and no further betting is possible, hands are shown before the undealt board cards run out. For a preflop all-in, finish the sealed auction first, then show both hole cards of every remaining player before dealing the selected flop. Bidders never see those hidden cards while choosing bids. Otherwise hands are shown after river betting ends.
- A failed bid (timeout, exception, or invalid bid) counts as bidding 0 on every card. It never folds the hand or removes any betting option.
- The variant supports 2–10 players.

## Bot interface

- At `state.phase == "auction"`, return `Action("bid", cards=(b0, b1, b2, b3, b4))`, where `b0` is your bid on auction card 0. The `cards` field carries the five bid amounts here, not card indexes. `amount` must be 0. An empty `cards` bids 0 on every card. `state.can("bid").max_amount` is your balance: the most your five bids may add up to.
- `state.variant["flop_chips"]` is your current balance on every turn, net of anything you have paid. `state.variant["flop_chips_by_seat"]` lists every seat's current balance by seat, including yours and busted seats'. Balances change only once every bid of an auction is in, so during an auction they show no bid of that auction. `state.variant["auction_cards"]` lists the five auction cards once they are dealt, and an `auction` event shows them. After the auction, `state.variant["flop_chips_paid"]` is what you paid this hand, and `state.board` holds the three selected cards. The two dead cards are the auction cards that are not on the board.
- Bids are requested one player at a time. Each produces a public `bid` event naming only the bidder. No bot ever sees another player's bid amounts, card scores, who bid on which card, or how ties were broken. A seat's balance falls by what it paid for the flop cards, which shows its total bid on them but not how that was split between cards.
- Your downloaded hand history lists every seat's balance before and after each hand, in `flop_chips_before` and `flop_chips_after`.
- Betting uses the existing action API; showdown reveals are automatic without a bot turn.

A minimal bid handler is:

```python
if state.phase == "auction":
    return Action("bid", cards=(0, 0, 0, 0, 0)), memory
```

Budget your flop-chips: a table can last a couple of hundred hands, and the balance is all you get.
