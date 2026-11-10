"""`poker-agent pack`, `check`, and `play`. Only `play` runs bot code, as ordinary local processes."""

import argparse
import asyncio
import json
import secrets
import shlex
import sys
from collections import Counter
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

from poker_agent_protocol.process import BotPool
from poker_engine.play import play_table
from poker_engine.scoring import table_places, table_points
from poker_engine.seating import seat_iteration
from poker_engine.table import SitAndGo, TableRules
from poker_engine.variant import DEFAULT_VARIANT, installed_variants, load_variant

from poker_agent_sdk.bundle import SubmissionError, check_submission, pack
from poker_agent_sdk.history import HistoryWriter


def main() -> None:
    from poker_agent_sdk.local_redzone import add_arguments, run_from_arguments

    parser = argparse.ArgumentParser(prog="poker-agent")
    commands = parser.add_subparsers(dest="command", required=True)
    pack_command = commands.add_parser("pack", help="Zip a bot directory for submission.")
    pack_command.add_argument("source", type=Path)
    pack_command.add_argument("destination", type=Path)
    check_command = commands.add_parser("check", help="Check a submission without running it.")
    check_command.add_argument("submission", type=Path)
    play = commands.add_parser("play", help="Play local bot directories against each other.")
    play.add_argument(
        "bots", type=Path, nargs="+", help="Directories with agent.py; repeat to add seats."
    )
    play.add_argument("--tables", type=int, default=20)
    play.add_argument("--seed", default=None)
    installed = installed_variants()
    # A starter kit installs only its competition's variant, so that is the default there.
    play.add_argument(
        "--variant",
        default=installed[0] if len(installed) == 1 else DEFAULT_VARIANT,
        help=f"Installed: {', '.join(installed)}",
    )
    play.add_argument("--decision-ms", type=int, default=1)
    play.add_argument("--hand-cap", type=int, default=250)
    history = play.add_mutually_exclusive_group()
    history.add_argument(
        "--history",
        type=Path,
        help="Choose a hand-history filename (default: a new file under hand-history/).",
    )
    history.add_argument("--no-history", action="store_true", help="Do not save a hand history.")
    play.add_argument("--hero", type=int, default=0, help="History perspective: bot index, from 0.")
    replay = commands.add_parser("redzone", help="Watch a downloaded or local hand history.")
    add_arguments(replay)
    arguments = parser.parse_args()
    try:
        if arguments.command == "pack":
            report = pack(arguments.source, arguments.destination)
        elif arguments.command == "check":
            report = check_submission(arguments.submission.read_bytes())
        elif arguments.command == "redzone":
            run_from_arguments(arguments)
            return
        else:
            asyncio.run(play_local(arguments))
            return
    except (SubmissionError, OSError, ValueError, KeyError) as error:
        parser.exit(2, f"{error}\n")
    print(
        json.dumps(
            {"sha256": report.sha256, "size_bytes": report.size_bytes, "files": report.files},
            indent=2,
        )
    )


async def play_local(arguments: argparse.Namespace) -> None:
    bots: list[Path] = [bot.resolve() for bot in arguments.bots]
    if not 2 <= len(bots) <= 10 or not all((bot / "agent.py").is_file() for bot in bots):
        raise ValueError("Give 2 to 10 bot directories that each contain agent.py.")
    if arguments.tables < 1:
        raise ValueError("Give at least one table.")
    if not 0 <= arguments.hero < len(bots):
        raise ValueError("--hero must be a bot index, starting at 0.")
    seed = arguments.seed or secrets.token_hex(8)
    rules = TableRules(
        variant=arguments.variant,
        players=len(bots),
        decision_ms=arguments.decision_ms,
        hand_cap=arguments.hand_cap,
    )
    variant = load_variant(rules.variant)
    # Two copies per directory, shared by every seat that repeats it, as in production.
    processes = {
        bot: BotPool(
            [sys.executable, "-m", "poker_agent_sdk.guest", str(bot)], copies=2, cwd=str(bot)
        )
        for bot in set(bots)
    }
    running = asyncio.Semaphore(8)  # One event loop measures every deadline; keep it responsive.
    labels = [f"{bot.name}#{index}" for index, bot in enumerate(bots)]
    points: dict[str, list[float]] = {label: [] for label in labels}
    places: dict[str, Counter[int]] = {label: Counter() for label in labels}
    failures: Counter[str] = Counter()
    history: HistoryWriter | None = None

    async def one_table(number: int) -> None:
        seating = seat_iteration(labels, seed=seed, iteration=number, table_size=len(bots))[0]
        table = SitAndGo(
            rules=rules, variant=variant, table_id=f"local-{number}", seed=f"{seed}:{number}"
        )
        # The table keeps each seat's memory, so a directory used twice plays two seats.
        seats = [processes[bots[labels.index(seat.entry)]] for seat in seating]
        async with running:
            result = await play_table(table, seats)
            if history is not None:
                # Encode/write away from the event loop that measures other bots' deadlines.
                await asyncio.to_thread(
                    history.table,
                    {
                        "variant": rules.variant,
                        "iteration": number,
                        "table": 0,
                        "seats": [seat.entry for seat in seating],
                        "fillers": [seat.filler for seat in seating],
                        "payouts": list(table.payouts),
                        "hands": table.log,
                    },
                )
        finishes = zip(table_points(result, table.payouts), table_places(result), strict=True)
        for seat, (assignment, (value, place)) in enumerate(zip(seating, finishes, strict=True)):
            points[assignment.entry].append(value)
            failures[assignment.entry] += result.failures[seat]
            places[assignment.entry][place] += 1

    with ExitStack() as stack:
        if not arguments.no_history:
            destination = arguments.history
            if destination is None:
                directory = Path("hand-history")
                directory.mkdir(exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                destination = directory / f"local-{stamp}-{secrets.token_hex(4)}.jsonl.gz"
            history = stack.enter_context(
                HistoryWriter(destination, labels=labels, hero=arguments.hero, seed=seed)
            )
        tasks = [asyncio.create_task(one_table(number)) for number in range(arguments.tables)]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for process in processes.values():
                await process.stop()
    for bot, process in processes.items():
        if process.logs:
            print(f"--- {bot.name} stderr (last {len(process.logs)} bytes) ---", file=sys.stderr)
            print(process.logs.decode(errors="replace"), file=sys.stderr)
    print(
        json.dumps(
            {
                "seed": seed,
                "tables": arguments.tables,
                "bots": {
                    label: {
                        "average_points": round(sum(points[label]) / len(points[label]), 3),
                        "places": dict(sorted(places[label].items())),
                        "failed_decisions": failures[label],
                    }
                    for label in labels
                },
            },
            indent=2,
        )
    )
    if history is not None:
        # Keep stdout as the existing JSON summary for scripts that consume it.
        print(f"Hand history saved to {history.path.resolve()}", file=sys.stderr)
        print(f"Watch with: replay {shlex.quote(str(history.path))}", file=sys.stderr)
