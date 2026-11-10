# Sample hand history

Use these files to build your analysis pipeline before the first round:

- `sample.jsonl.gz`: a synthetic participant download, compressed like the real file.
- `sample.jsonl`: the exact same JSON lines, uncompressed for inspection.
- `read.py`: a streaming reader and example summary, using only Python's standard library.

From the kit root:

```sh
python hand-history/read.py
python hand-history/read.py hand-history/sample.jsonl
python hand-history/read.py path/to/your-downloaded-hands.jsonl.gz
```

The sample contains fictional teams and IDs and complete locally simulated tables.
It was played with this kit's variant using the actual engine and participant export views.
The short sample hand cap and scripted decisions illustrate the data format; they are not
the event's settings or a suggested strategy. Read `RULES.md` for the variant's actions.
Examples include a win by folds, showdown, your own automatic fallback, an all-in runout,
and filler seats. Your seat changes between tables. If this kit includes `CASES.md`, start
there to find additional variant mechanics and edge cases by table, hand number and event
index; `coverage.json` provides the machine-readable index. These examples illustrate supported data shapes and mechanics, not every possible
outcome or a representative distribution of opponent strategies.
Some coverage collections combine tables with different illustrative configurations to
exercise more cases in a small file; `coverage.json` identifies those collections and lists
their settings. A real downloaded round uses its configured rules. The JSON record format
is the same, and the coverage notes are separate from the participant records.

Filter on `fillers[seat]` when analyzing scored play. Fillers still have chip balances and
equity values and can take paid places, but their points and any prize statistics do not count.
Other places keep their scheduled payouts; a filler winner does not promote second place
to the first-place payout. There can be fewer seats than the normal table size.

## File structure

Each line is one complete JSON object, ending in a newline. A download contains:

1. One header: `tournament_id`, `you` (your entry ID), and `entries` (entry IDs and team names).
2. One public table record.
3. One private record, `{"you": {...}}`, for that table.
4. Repeat the public/private pair for each remaining table.

Real downloads concatenate gzip members, including the header and table records. Python's
`gzip.open` reads the whole stream. A reader that stops after the first member only reads
the header. Stream the pairs instead of loading a whole round into memory. `read.py`
rejects invalid headers, mismatched pairs, invalid own-card arrays and invalid own automatic
references. Python's gzip reader rejects damaged or unfinished members. The file has no
expected table count or completion footer: truncation exactly after a complete pair cannot
be detected from the file alone. A valid header with no table pairs is readable too.
This is an example streaming reader, not a validator of every poker rule or event field.

## Fields and indexes

| Location                             | Meaning                                                                                                                                                              |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Header `you`                         | Your entry ID for this round; different from the private object's `you` key.                                                                                         |
| Table `iteration`, `table`           | Zero-based identifiers; together identify a table within the round.                                                                                                  |
| Table `variant`, `board_count`       | Optional variant ID and community-card layout. New exports include these; older files may omit them.                                                                 |
| Table `seats`                        | Entry ID at each zero-based seat. Resolve names through header `entries`. Your entry can play at more than one table per iteration when filling short tables.        |
| Table `fillers`                      | Parallel booleans: a filler plays but does not score. Use the private `seat` to locate the seat described by this pair.                                              |
| Table `payouts`                      | Points for each finishing place at this table.                                                                                                                       |
| Table `hands`                        | Played hands, in order. Each `hand` number starts at 1; array indexes start at 0.                                                                                    |
| Hand `button`, event `seat`          | Zero-based indexes into this table's `seats`; event `seat` can be absent.                                                                                            |
| Hand `blinds`                        | `[small_blind, big_blind, ante]`.                                                                                                                                    |
| Hand `stacks_before`, `stacks_after` | Chip counts by seat, including zero for a busted seat.                                                                                                               |
| Hand `equity_before`, `equity_after` | Expected tournament points by seat if the table ended there, including ICM. These are not hand-winning probabilities.                                                |
| Hand `cards`                         | End-of-hand public cards by seat: the cards that seat last made public, or `[]` when none were.                                                                      |
| Private `iteration`, `table`, `seat` | Match this private line to its public table and locate your seat. The seat can be a filler; check `fillers[seat]` when analyzing scored play.                        |
| Private `cards`                      | Your originally dealt cards for each element of `hands`, in dealing order. A busted seat has `[]`.                                                                   |
| Private `automatic`                  | `[hand_number, event_index]` pairs for actions played for your failed decisions. Event indexes are zero-based within that hand's `events`. An empty list means none. |
| Private `private_events`             | Optional owner-only details, attached to the public event where they take effect. See below.                                                                         |

Public `cards` is a final snapshot. Replay `events` in order to learn which cards were
visible at each decision; using the snapshot for earlier decisions would introduce future
information. Opponents' unrevealed cards are omitted. Organizer logs
contain additional private data and have a different structure from these participant files.
No event in the public line identifies another bot's automatic fallback.

New histories may include `private_events` in your private record. Each has a one-based
`hand` number, a zero-based `event_index` into that hand's public `events`, and a `kind`.
The list is empty when the variant has no additional private details.


Each hand also has `flop_chips_before` and `flop_chips_after`: every seat's flop-chip balance
by seat, like `stacks_before` and `stacks_after`. A seat's drop is what it paid at that hand's
auction. Other players' bids never appear. Older downloads may lack these fields.

- `bid`: one auction card in `cards` and your bid for it in `amount`, effective at your bid.
- `flop_chips` / `flop_chips_paid`: your balance or payment in `amount`. An initial balance
  uses `event_index: -1`; the payment and resulting balance use the auction's flop event.


Apply a private event only when its referenced public event has happened. Other players' private
details never appear in these records. The list is optional: old downloads and old organizer
logs may lack it.


All showdown reveals performed by the engine are normal game behavior; they are not failed bot
decisions and do not by themselves appear in your `automatic` list. Failure reasons
(timeout, exception or illegal action) are not included in this file.

## Events

Events are ordered objects with a `kind` and optional `seat`, `amount`, and `cards`.
Fields that do not apply are omitted; zero amounts are usually omitted too. A `stack`
event always includes `amount`, including zero. Cards use strings such as `As` and `Td`.

- `blind` and `ante`: chips posted; `call`: additional chips paid, including a short all-in call.
- `raise`: **total bet for the current betting round**, not the raise increment.
- `check`, `fold`: betting actions. `show`: automatic showdown disclosure, including publicly shown cards.
- Older histories may contain `muck` (a seat without cards); current games do not allow mucking.
- `board`: newly dealt community cards, not the entire board.
- `win`: chips awarded. A hand can have several such events, including split or side pots.
- `stack`: the seat's final chip count for the hand.

Uncalled excess bets can be returned without a separate refund event. Recorded contributions
therefore need not equal the sum of `win` amounts. Use `stacks_before` and `stacks_after`
as the authoritative balances, and compute a seat's net chip change as
`hand["stacks_after"][seat] - hand["stacks_before"][seat]`. Receiving a `win` award from a
side pot does not necessarily mean the seat gained chips overall. The equity arrays contain
rounded tournament points; compare totals with a tolerance rather than exact equality.

If organizers enable optional late-game resampling, this participant file shows only the
designated first continuation. Official points and prize counters average all continuations,
so this file alone cannot reconstruct those official totals. The other continuations are
kept in organizer logs and scoring results, not in these public/private pairs. Ordinary
rounds with resampling disabled have one continuation. The samples use ordinary play.

Variants can add event kinds; `RULES.md` describes this kit's. Accept additional fields and
kinds in your pipeline instead of assuming every hand has the same sequence. Preserve all
events in their original order when resolving `automatic` indexes, even if your analysis
ignores some event kinds.

To combine a parsed table with your private cards, adapt the loop in `read.py`:

```python
for index, hand in enumerate(table["hands"]):
    my_original_cards = own["cards"][index]
    my_starting_stack = hand["stacks_before"][own["seat"]]
    my_chip_change = hand["stacks_after"][own["seat"]] - my_starting_stack
    my_automatic_events = [
        hand["events"][event_index]
        for hand_number, event_index in own["automatic"]
        if hand_number == hand["hand"]
    ]
```
