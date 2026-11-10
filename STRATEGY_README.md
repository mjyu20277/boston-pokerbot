# Boston Poker Bot Strategy

These are my notes on how I thought about the bot. The game is basically normal no-limit
hold'em, except before the flop we get to bid separate flop-chips on which cards become the
flop. The hard part is that only the top 3 places score:

- 1st: 50 points
- 2nd: 30 points
- 3rd: 20 points
- 4th-8th: 0 points

So the bot cannot just hide forever. If we keep folding and finish 4th or 5th, that still
means 0 points. But we also cannot just gamble randomly, because busting early is also 0.

The main idea became:

```text
Survive early.
Raise good hands.
Stop calling bad spots.
Use flop-chips before they become useless.
Push harder when top 3 is close.
```

## What We Saw In The Data

The data made a few things pretty obvious:

- Busting before hand 50 was almost always terrible.
- Surviving past hand 100 was strongly connected to scoring.
- Preflop raises were much better than passive calls.
- Postflop calls, especially turn and river calls, were very bad.
- Tables where we used more flop-chips did much better.
- Pure folding was too passive because it often led to 4th/5th place.
- Random all-ins were usually bad. Pressure was good, but panic was bad.

The simplest version of the lesson:

> We want pressure, not panic.

## Betting Mindset

Preflop, I wanted the bot to mostly play raise-or-fold.

Strong hands should raise. Weak hands should fold. Calling should be rare, because calling
lets other players control the hand and keeps us stuck in awkward spots.

The hands I wanted to trust most were:

- big pairs like TT+
- AK
- AQ, AJ, AT
- strong broadway hands

The hands I wanted to be careful with were:

- weak aces
- low pairs
- random suited connectors
- trash hands that look tempting but do not win enough

Postflop, the biggest rule was to stop donating chips. If we have a strong hand, we can bet
or raise. If we have a medium hand, checking is fine. But if someone makes a big bet on the
turn or river and we only have a medium hand, we should usually fold.

That is why the bot's postflop logic is basically:

```text
Strong hand: bet or raise.
Good draw: sometimes semi-bluff.
Medium hand: check if free.
Weak hand facing pressure: fold.
Big turn/river call: usually no.
```

## Auction Mindset

Flop-chips are only useful if we spend them. They do not become points, and they do not help
after we bust. So I wanted the bot to spend them, but not randomly.

The auction cards we care about most are cards that:

- pair one of our hole cards
- help us make trips
- help a flush
- help a straight
- support already strong high-card hands

One rule we discussed was that if an auction card pairs something in our hand, it is often
worth bidding around `1/40` of our flop-chip balance. That is not magic, but it is a simple
way to make sure we do not ignore very direct improvement cards.

I also wanted the bot to catch up later. If we are deep into the table and still have too
many flop-chips, that is bad. The rough goal is to use most of them by the late game,
especially if we are getting close to the top 3.

## Top-Three Mindset

The payout structure matters a lot. Since 4th and 5th get nothing, we need to be more willing
to fight when only a few players are left.

That does not mean shoving everything. It means:

- open a little wider
- raise strong hands
- pressure short stacks
- spend remaining flop-chips harder
- avoid big calls with medium hands
- avoid random all-ins unless we are very short or very strong

This was the biggest shift in thinking. At first I was thinking too much about survival.
Then the results showed that survival without top-three conversion was not enough.

## Strategy Files

### `agent.py`

This is the main bot. It separates the logic into pieces:

- preflop betting
- postflop betting
- auction bidding
- hand strength estimates
- pot odds / call pressure
- action safety checks

The separation made it easier to tune the bot without mixing up the auction logic and normal
poker logic.

### `strategies/always_fold/agent.py`

This was a baseline experiment. The idea was to see what happens if we play extremely
conservatively, with only a few premium-hand exceptions.

What we learned: it can survive, but it is too passive. Too many 4th/5th finishes means too
many 0-point tables.

### `strategies/survival_raise_fold/agent.py`

This was the cleaner conservative version: raise good hands, fold bad hands, avoid calling.

This is closer to the right mindset, but it still needs enough auction spending and enough
late pressure to actually reach top 3.

### `strategies/combo_rotation/agent.py`

This is the experiment runner. It rotates between different profiles so we can test ideas
across many tables.

The profiles I think are most worth keeping are:

- `field_mimic`: tries to copy the stronger field pattern: more raises, more auction use.
- `hybrid_best`: the main balanced version.
- `bubble_converter`: tries harder when 4th/5th is not enough.
- `auction_95`: tests heavier flop-chip spending.
- `pair_hunter`: bids more when auction cards pair our hole cards.
- `big_stack_bully`: pressures the table when we have a stack advantage.

The profiles I trust less are:

- pure always-fold styles
- very nitty styles
- anything that jams too often
- anything that saves too many flop-chips

## Current Takeaway

The bot should not play scared, and it should not play reckless. The best version is
selectively aggressive:

```text
Fold the junk.
Raise the good hands.
Spend flop-chips on cards that actually help.
Stop paying off big bets with medium hands.
Push harder near top 3.
Do not punt the stack for no reason.
```

That is the mindset behind the current design.
