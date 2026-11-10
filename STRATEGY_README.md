# Boston Poker Bot Strategy Notes

This document explains the strategy mindset behind our Boston no-limit hold'em bot and the
experimental strategy variants in `strategies/`.

Boston is normal no-limit hold'em plus a sealed flop auction. The key practical lesson from
our hand-history analysis is that this tournament does not reward simply surviving into
fourth or fifth place. The table pays only the top three places:

- 1st place: 50 points
- 2nd place: 30 points
- 3rd place: 20 points
- 4th-8th place: 0 points

So the bot is built around a simple tension:

1. Avoid early disasters.
2. Build enough chip pressure to reach top three.
3. Spend flop-chips before they become useless.

## High-Level Mindset

The strategy is not trying to play perfect poker. The decision limit is extremely tight, so
the bot uses fast heuristics. The winning mindset is:

- Fold weak hands without regret.
- Raise strong hands instead of passively calling.
- Spend flop-chips on cards that directly help our hand.
- Check medium hands when checking is free.
- Avoid expensive calls, especially on the turn and river.
- Become more aggressive near the top-three bubble.
- Do not randomly go all-in just because the table is late.

In short:

```text
Bad hand: fold.
Good hand: raise.
Medium hand: check if free, fold if expensive.
Strong made hand: value bet.
Strong draw: sometimes semi-bluff.
Late table: apply pressure, but do not punt.
Auction: buy cards that improve us, and use most flop-chips by late table.
```

## What The Data Told Us

The strongest signals from the analyzed hand histories were:

- Busting before hand 50 was basically worth 0 points.
- Surviving past hand 100 was strongly correlated with scoring.
- Tables with high flop-chip usage performed far better.
- Preflop raises were profitable.
- Postflop calls were very expensive.
- Top-performing opponents raised often and spent a lot of flop-chips.
- Pure folding strategies often reached 4th/5th, which is still 0 points.

The most important behavioral clusters were:

| Behavior | Result |
| --- | --- |
| High preflop raise + high flop-chip spend | Strongest scoring profile |
| Survive 100+ hands without all-in punts | Strong scoring profile |
| Low raise + low flop-chip spend | Very poor scoring profile |
| Low flop-chip usage | Very poor scoring profile |
| Frequent all-ins | Usually bad in our data |

The practical conclusion is:

> We want pressure, not panic. Raise more good hands, spend more auction chips, but avoid
> unnecessary all-ins and bad calls.

## Main Agent Structure

The main bot in `agent.py` is structured around separate helper components:

- `Config`: central thresholds and tuning constants.
- `StrategyEngine`: routes each turn by phase.
- `HandEvaluator`: estimates preflop strength, made-hand strength, draws, and auction-card value.
- `OpponentTracker`: tracks table action tendencies.
- `PotOddsMath`: computes call pressure, pot odds, stacks, and raise sizes.
- `ActionSafety`: validates actions before returning them.

This separation matters because Boston has two different games inside one hand:

1. Betting: normal no-limit poker.
2. Auction: sealed bidding for the flop.

Keeping them separate made it easier to tune each without breaking the other.

## Current Core Thresholds

The main `agent.py` strategy currently uses these important thresholds:

### Preflop

| Parameter | Value | Meaning |
| --- | ---: | --- |
| `PREFLOP_OPEN_THRESHOLD` | `0.50` | Minimum heuristic strength to open-raise. |
| `PREFLOP_CALL_THRESHOLD` | `0.66` | Minimum strength for rare preflop calls. |
| `PREFLOP_PREMIUM_THRESHOLD` | `0.78` | Premium hands like big pairs and AK-like strength. |
| `PREFLOP_THREE_BET_THRESHOLD` | `0.70` | Strength needed to re-raise. |
| `SHORT_STACK_BIG_BLINDS` | `10` | Below this, use shove/fold logic. |
| `PREFLOP_SHORT_STACK_THRESHOLD` | `0.58` | Short-stack shove threshold. |

The intended behavior:

- Raise strong openable hands.
- Re-raise premium hands.
- Avoid flat calls unless the hand is very strong and the price is small.
- Fold weak and speculative hands.

### Top-Three Push Mode

The bot becomes more aggressive when the table is late or short-handed:

| Parameter | Value | Meaning |
| --- | ---: | --- |
| `TOP3_PUSH_PLAYERS` | `5` | Start top-three pressure when 5 or fewer players remain. |
| `TOP3_PUSH_HAND` | `70` | Start top-three pressure after hand 70. |
| `TOP3_OPEN_DISCOUNT` | `0.08` | Lower open threshold in top-three mode. |
| `TOP3_THREE_BET_THRESHOLD` | `0.64` | Lower re-raise threshold in top-three mode. |
| `TOP3_SHORT_STACK_BIG_BLINDS` | `16` | Wider short-stack pressure zone. |
| `TOP3_SHORT_STACK_THRESHOLD` | `0.50` | Lower short-stack shove threshold. |

The idea is that 4th and 5th place score nothing, so the bot cannot simply fold forever near
the bubble.

### Postflop

| Parameter | Value | Meaning |
| --- | ---: | --- |
| `VALUE_BET_THRESHOLD` | `0.72` | Bet or raise strong hands. |
| `MONSTER_THRESHOLD` | `0.86` | Use larger value sizing. |
| `CALL_THRESHOLD` | `0.55` | Minimum equity for normal calls. |
| `DRAW_CALL_THRESHOLD` | `0.45` | Minimum draw equity for cheap calls. |
| `SEMI_BLUFF_THRESHOLD` | `0.48` | Minimum equity for draw-based raises. |
| `POT_ODDS_MARGIN` | `0.08` | Equity buffer over pot odds. |
| `MAX_NORMAL_CALL_PRESSURE` | `0.22` | Max stack pressure for normal calls. |
| `MAX_SPECULATIVE_CALL_PRESSURE` | `0.10` | Max stack pressure for draw calls. |
| `LATE_STREET_VALUE_CALL_THRESHOLD` | `0.68` | Turn/river calls require real value. |

Postflop logic is intentionally harsh:

- Strong hand: raise.
- Draw plus fold equity: semi-bluff raise.
- Medium hand: check if possible.
- Facing a large turn/river bet: usually fold.

This came directly from the data. Flop, turn, and river calls were major leaks.

### Bet Sizing

| Situation | Sizing |
| --- | ---: |
| Normal value bet | `0.65x pot` |
| Monster value bet | `0.85x pot` |
| Semi-bluff | `0.50x pot` |

All sizes are clamped to the legal raise range before returning the action.

## Auction Strategy

Flop-chips are separate from poker chips. They cannot win points directly, and unused
flop-chips are worthless when we bust. The bot tries to spend them when they can improve our
actual poker hand.

### Auction Budget

| Parameter | Value | Meaning |
| --- | ---: | --- |
| `AUCTION_BASE_BUDGET` | `8` | Small speculative budget. |
| `AUCTION_STRONG_BUDGET` | `18` | Budget for good but not premium spots. |
| `AUCTION_PREMIUM_BUDGET` | `35` | Budget for premium preflop spots. |
| `AUCTION_EARLY_HAND_CAP` | `35` | Spend less in the earliest hands. |
| `AUCTION_EARLY_BUDGET_FACTOR` | `0.55` | Early-hand discount. |
| `AUCTION_TARGET_SPEND_FRACTION` | `0.80` | Baseline target spend by late table. |
| `AUCTION_TARGET_HAND_HORIZON` | `130` | Try to hit spend target by this hand. |
| `AUCTION_CATCHUP_FRACTION` | `0.70` | How hard to catch up if underspending. |
| `AUCTION_MAX_CATCHUP_BUDGET` | `80` | Normal catch-up cap. |
| `TOP3_AUCTION_TARGET_SPEND_FRACTION` | `0.95` | Higher spend target in top-three mode. |
| `TOP3_AUCTION_MAX_CATCHUP_BUDGET` | `140` | Higher catch-up cap in top-three mode. |

The bot scores each auction card by asking:

- Does this card pair one of our hole cards?
- Does this card help a flush draw?
- Does this card connect to our ranks for straight potential?
- Is this a high card that works with our strong hand?
- Are we late enough that unused flop-chips are becoming wasteful?

Special pair logic:

- If an auction card pairs one of our hole cards, the bot ensures a bid floor of roughly
  `1/40` of the current flop-chip balance.
- This matches the intuition that pairing our private cards is one of the cleanest ways to
  turn the auction into real hand strength.

### Auction Landing Boost

If the board contains cards we successfully targeted in the auction, the bot gives our
postflop equity a boost:

| Parameter | Value |
| --- | ---: |
| `BID_FLOP_EQUITY_BOOST` | `0.50` |

This does not mean our win chance literally becomes 50 percentage points higher. It means
the heuristic treats a successful auction as meaningful evidence that the flop helped our
plan.

## Experimental Strategy Folder

The `strategies/` folder keeps separate agents for testing.

### `strategies/always_fold/agent.py`

Purpose: baseline survival experiment.

Mindset:

- Fold almost everything.
- Only make narrow exceptions for premium starting hands and very strong auction outcomes.

What we learned:

- Too much folding often creates 4th/5th-place finishes.
- Since 4th/5th score 0, pure survival is not enough.

### `strategies/survival_raise_fold/agent.py`

Purpose: conservative raise/fold bot.

Mindset:

- Raise strong hands.
- Fold weak hands.
- Avoid costly postflop calls.
- Spend auction chips more carefully than the main bot.

What we learned:

- This is safer than always-fold.
- It can still be too passive if it does not spend enough flop-chips.

### `strategies/combo_rotation/agent.py`

Purpose: A/B testing harness.

This strategy rotates through multiple profiles by hashing `state.config.table_id` into
20 stable buckets. In a 20,000-table practice run, each bucket should get about 1,000 tables.

Current active bucket plan:

| Buckets | Profile | Purpose |
| --- | --- | --- |
| 0, 1, 19 | `field_mimic` | Mimic successful field pattern: more raising, high auction use. |
| 2, 3 | `bubble_converter` | Convert 4th/5th into top-three finishes. |
| 4, 5, 6 | `hybrid_best` | Main blended profile: raise/fold, high auction, low calls. |
| 7, 8, 9, 18 | `late_jam_nyx` | Late pressure profile, but should be watched for all-in risk. |
| 10, 11 | `short_stack_converter` | Short-stack pressure from about 24 big blinds. |
| 12, 13 | `big_stack_bully` | Pressure shorter stacks when we have stack advantage. |
| 14, 15 | `allin_auction_boost` | Spend harder when committed or all-in situations arise. |
| 16 | `auction_95` | Very high auction-spend target. |
| 17 | `pair_hunter` | Bid harder on auction cards that pair our hole cards. |

Important current lesson:

The rotation should keep profiles that combine survival, raising, and auction spend. It
should reduce or remove profiles whose main identity is folding too much or jamming too
often.

## Strategies To Keep

Based on hand-history analysis, the strongest families are:

### 1. High-Raise, High-Auction Strategy

Why it works:

- Builds enough chips to reach top three.
- Uses flop-chips before they expire.
- Puts pressure on opponents who overfold or overcall.

Keep profiles like:

- `field_mimic`
- `hybrid_best`
- `auction_95`
- `pair_hunter`

### 2. Bubble Converter

Why it works:

- The payout cliff is brutal.
- 4th and 5th are worth 0.
- Around 5 players left, we must stop playing only to survive.

Keep:

- `bubble_converter`
- Some `big_stack_bully`

### 3. Low Postflop Call Strategy

Why it works:

- Our data showed postflop calls losing large amounts.
- Raising strong hands is better than calling with medium hands.

Keep:

- `no_late_calls` behavior inside blended profiles.
- Turn/river call restrictions.

## Strategies To Reduce Or Remove

### Always-Fold / Pure Nit

Problem:

- Often survives too passively.
- Does not build enough stack.
- Produces too many 4th/5th-place finishes.

### Low Auction Spend

Problem:

- Flop-chips do not become points.
- Unused flop-chips disappear when we bust.
- Data showed low flop-chip usage was one of the clearest losing signals.

### Panic All-In

Problem:

- All-in-heavy tables performed poorly in the analyzed data.
- The better version is pressure without unnecessary stack death.

Preferred replacement:

- Large raises with strong hands.
- Shove only with premiums, strong short-stack spots, or unavoidable stack pressure.

## Future Tuning Ideas

Useful next experiments:

1. Replace late max-all-in jams with large non-all-in raises where legal.
2. Push auction target toward `0.90-0.95`, but only on hands that can realistically continue.
3. Keep turn/river calls extremely rare.
4. Track exact top-three conversion by strategy bucket in future runs.
5. Save the selected strategy name in memory/debug logs if the platform exposes it later.
6. Compare `hybrid_best`, `field_mimic`, `auction_95`, and `pair_hunter` over equal table counts.

## Practical Submission Notes

The competition uploads `agent.py` by default. Files under `strategies/` are for experiments
unless we intentionally copy one into the root `agent.py`.

Before submitting, run:

```sh
python3 -m py_compile agent.py strategies/always_fold/agent.py strategies/survival_raise_fold/agent.py strategies/combo_rotation/agent.py
```

And if the SDK is installed:

```sh
poker-agent check agent.py
```

The bot should have zero automatic fallback actions. A timeout, exception, or illegal action
turns into an engine fallback, which usually checks or folds and silently destroys expected
value.
