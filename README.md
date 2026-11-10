# No Limit: starter kit

Everything you need to build, test, and submit a bot. The full guide, with the rules and the
schedule, is on the competition site under **Getting started**.

Our Boston strategy notes, data takeaways, and experimental profile explanations are in
[`STRATEGY_README.md`](STRATEGY_README.md).

## 1. Set up (Python 3.12)

On Linux, macOS 14 or newer, or Windows (WSL works best):

```sh
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e ./sdk/agent-protocol -e ./sdk/engine -e ./sdk/agent-sdk
```

`requirements.txt` holds the exact libraries the competition runs; nothing else is available
to your bot. The SDK gives you the `Agent` base class, a local tournament simulator, and the
upload checker.

The SDK packages are installed in editable mode: changing their Python source under `sdk/`
takes effect on the next run without reinstalling. Reinstall after changing package
dependencies. Local engine changes do not change the competition server; use the released
rules when testing a submission.

## 2. Write your bot

Edit `agent.py`. `act(state, memory)` runs on your turn and returns your action and the memory
you want back on your next turn at that table: bytes, empty at the start of every table, up to
64 KB. It must return within 1 ms; beyond that it spends your time bank, 100 ms per table and
full again at every table (`state.time_bank_ms` is what is left). Memory travels within your
1 ms, so keep it under 16 KB.
Your bot keeps nothing else between calls.
`examples/` has two simple bots to play against.

## 3. Test it locally

```sh
poker-agent check agent.py
poker-agent play . examples/check_or_fold examples/min_raiser --tables 200
```

`poker-agent play` runs real sit-and-go tables between bot folders (repeat a folder to give it more seats)
and prints each bot's average points, finishing places, and failed decisions. Aim for zero
failed decisions: a betting failure checks or folds for you. Showdown reveals happen
automatically, without a bot decision.

Each run also saves a replay file in `hand-history/` and prints its location and a `replay`
command. The filename includes the time and a random suffix, so repeated runs keep their own
files. Add `--no-history` when you only want statistics.

### Watch your hands

After setup, type this to open the viewer in your browser:

```sh
replay
```

Drag your hand-history file onto the page, or click **Choose file**. Dropping another file
replaces the replay and starts at its first hand. An invalid file leaves the current replay
loaded. Files stay on your computer, and the viewer never changes the originals.

Use the saved file from your local run, or choose its filename explicitly:

```sh
poker-agent play . examples/check_or_fold examples/min_raiser --tables 20 --history hands.jsonl.gz
replay hands.jsonl.gz
```

Passing a filename loads it immediately. To try the included sample, choose
`hand-history/sample.jsonl.gz`. `redzone` and `poker-agent redzone` also work as aliases.
If you already installed an earlier kit, reinstall `pip install -e ./sdk/agent-sdk` after
updating it to add the `replay` command. Keep your Python environment activated as in setup.

The viewer opens in your browser and runs entirely on your computer. Choose a table and hand,
play or pause, change speed, step through actions, scrub, or open the action log. Fullscreen
uses the same cards, table, and animations as the competition's RedZone. Keep the command
running while watching; press Ctrl+C to close it. No account, internet connection, Node.js,
or website setup is needed. Use `--no-open` to print the local URL without opening a browser;
`--port 8765` picks a fixed port (by default it chooses a free one).

Local runs save histories by default; `--history FILE` chooses a destination and `--no-history`
turns saving off. These options cannot be combined. Histories use the participant download
format: a gzip file of JSON lines containing an
entry header, then public table records paired with your private cards and automatic actions.
The first bot is your perspective; `--hero 1` selects the second bot, and so on, even when seats
are shuffled. Repeated bot folders are distinct entries. The filename must be new. The usual
summary still prints to the terminal; that summary alone does not contain replayable hands.

You can also pass your team's **hand-history download** from a completed tournament directly
to `replay`. Both `.jsonl` and `.jsonl.gz` work, including concatenated gzip members.
The viewer indexes the file on disk and loads one table at a time. Empty downloads show an
empty state; malformed or truncated records produce an error. A file cut cleanly between
complete table pairs cannot be distinguished from a shorter download by this format.

Opponents' cards stay face down until a public reveal or show action. Win odds are omitted
because hidden cards are unavailable.


Final chips come from the recorded settlement, including split pots and refunds.
Filler seats are labeled and retain their actual paid places; their appearances do not score.
Local play still uses the existing seating and scoring rules.

## 4. Submit

Upload `agent.py` on your team page, or put `agent.py` and the files it reads in a folder of
their own and zip it with `poker-agent pack my_bot my_bot.zip`. Do not pack this kit's folder:
`pack` checks every file, and the kit's `sdk/` uses the local simulator, which the competition
machines do not have. Every upload plays a short test match first; when it shows
**Ready**, it becomes your team's active bot. To play an earlier upload instead, choose it on your
team page.

## Table rules (defaults)

- 8 players, 2,000 chips each. Blinds rise every 20 hands: 10/20, 20/40, 30/60, 50/100,
  80/160, 125/250, 200/400, 300/600, 500/1000, 800/1600, 1200/2400, 2000/4000
  (the last level repeats).
- A table ends when one bot has every chip, or after 250 hands. Places pay 50/30/20 points;
  at the hand cap the remaining bots split the rest by ICM (`poker_agent_sdk.icm`).
- Two bots busting on the same hand: the one that started it with more chips places higher.

## Showing hands

Every player who reaches showdown automatically reveals all hole cards, including
losing hands. There is no mucking and no showdown call to `act`. A fold does not reveal any
additional cards. If everyone else folds, the winner does not have to show; cards already
public stay public.


Public `show` events arrive in `state.events` on your next turn at the table, alongside the
payouts and final stacks. There may be no next turn if you bust or the table ends; completed
hand histories still record the shows. `RULES.md` describes your variant's other phases and
when all-in hands become visible.

## Hand histories

Build your pipeline before the first round with [the sample hand history](hand-history/README.md).
`hand-history/sample.jsonl.gz` matches the participant download format for this kit's variant;
an uncompressed copy and a streaming Python reader are included. Try
`python hand-history/read.py` from this folder.

When a round completes, your team page offers its hand history: every table your bot played
that round, as one gzip file of JSON lines. Opponents are named, and you see what a player at
the table saw: your own cards every hand, and other players' cards only when made public.

- The first line is `{"tournament_id", "you", "entries"}`: your entry ID and the team behind
  every entry ID.
- Then each table is two lines: the table (`iteration`, `table`, `seats` as entry IDs,
  `fillers`, `payouts`, `hands`), followed by `{"you": {"iteration", "table", "seat", "cards", "automatic"}}` with
  your seat, your cards in each hand, and the actions the table played for your bot after a
  timeout or error, as `[hand, event index]`.
- Each hand has `button`, `blinds`, `stacks_before`, `stacks_after`, `cards` (public cards
  only), `events` (`blind`, `call`, `raise` to an amount, `fold`, `board`, `show`, `win`, ...),
  and each seat's points if the table stopped there (`equity_before`, `equity_after`).

The sample guide explains event amounts, card visibility, seat indexes, and pairing the
records. The reader streams one table at a time and reads all concatenated gzip members.

## Modifying the kit

- `agent.py`: your strategy and table memory.
- `examples/`: simple reference opponents.
- `sdk/agent-protocol/src/poker_agent_protocol/game.py`: state, actions, and wire encoding.
- `sdk/engine/src/poker_engine/variant.py`: the hand/variant interface.
- `RULES.md`: the rules of your competition's variant.
- `sdk/engine/src/poker_engine/variants/`: that variant's implementation.
- `sdk/engine/src/poker_engine/holdem.py`: the hold'em reference most variants build on.
- `sdk/engine/src/poker_engine/table.py`: blinds, stacks, payouts, and hand limits.
- `sdk/agent-sdk/src/poker_agent_sdk/cli.py`: local simulation and submission commands.

Repeat a local match with `--seed YOUR_SEED` to compare changes with the same initial
randomness. Identical results require the same rules and bot actions as well as the seed.
Local play uses the variant this kit was made for; `RULES.md` describes it. Strategies
written for one variant's cards and legal actions may need revising for another.

## Using an AI assistant

`AGENTS.md` (and `CLAUDE.md`, which points to it) tells AI coding assistants the rules and
limits in this kit, so open this folder in your assistant and it will pick them up.
The setup skill is one file on the competition site. Install it once with
`curl -fsSL --create-dirs -o ~/.agents/skills/no-limit-2026-setup/SKILL.md <site>/no-limit-2026-setup/SKILL.md`
for Codex, or the same line with `~/.claude/skills/no-limit-2026-setup/SKILL.md` for Claude
Code (`<site>` is the competition site's address, including `/tournament` when it is there).
Or paste its address into your assistant and ask it to download the file and follow it. With an
API key from the **API keys** section of your Account page, it registers your SSH key, connects
Codex to your team's build machine once it opens, and puts this kit on the machine or
downloads it here. The build machine is where you build and test your bot in an environment
similar to the one the tournament runs it in.
