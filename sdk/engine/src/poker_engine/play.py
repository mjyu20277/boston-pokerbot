"""Play one table with one bot per seat. A failing bot plays passively; the table always finishes.

Bots are stateless: the table keeps each seat's memory and time bank, and hands them back on
that seat's next turn. Both start afresh at every table, so nothing carries from one table to
the next.
"""

import copy
from collections.abc import Sequence
from dataclasses import dataclass

from poker_agent_protocol.game import Action
from poker_agent_protocol.runner import Bot, BotError, BotTimeout

from poker_engine.table import SitAndGo, TableResult


async def play_table(table: SitAndGo, bots: Sequence[Bot]) -> TableResult:
    if len(bots) != table.rules.players:
        raise ValueError("Every seat needs exactly one bot.")
    return await _play(table, bots, [b""] * len(bots), [0] * len(bots), [0] * len(bots))


async def _play(
    table: SitAndGo,
    bots: Sequence[Bot],
    memory: list[bytes],
    failures: list[int],
    decisions: list[int],
    threshold: int | None = None,
) -> TableResult:
    while (state := table.decision(stop_at_survivors=threshold)) is not None:
        seat = state.seat
        decisions[seat] += 1
        action: Action | None = None
        answered = False
        bank_ms = 0.0
        try:
            reply = await bots[seat].act(state, memory[seat])
            action, memory[seat], bank_ms = reply.action, reply.memory, reply.bank_ms
            answered = True
        except BotTimeout:
            bank_ms = state.time_bank_ms  # It waited out the whole bank.
        except BotError:
            pass  # The memory stays as it was, and these events are sent again next turn.
        if not table.apply(action, answered=answered, bank_ms=bank_ms):
            failures[seat] += 1
    return table.result(tuple(failures), tuple(decisions))


@dataclass
class ResampledTable:
    tables: list[SitAndGo]
    results: list[TableResult]
    prefix_hands: int
    prefix_decisions: tuple[int, ...]
    prefix_failures: tuple[int, ...]


async def play_resampled(table: SitAndGo, bots: Sequence[Bot]) -> ResampledTable:
    """One parent sample. Suffixes run sequentially, bounding demand per parent table.

    Branch zero retains the ordinary stream; extra branches change future deals only.
    The full engine state and executor-owned memory/banks are independent in every branch.
    """
    if len(bots) != table.rules.players:
        raise ValueError("Every seat needs exactly one bot.")
    memory = [b""] * len(bots)
    failures, decisions = [0] * len(bots), [0] * len(bots)
    prefix = await _play(table, bots, memory, failures, decisions, table.rules.resampling_threshold)
    alive = sum(stack > 0 for stack in table.stacks)
    if alive < 2 or table.hands_played >= table.rules.hand_cap:
        return ResampledTable(
            [table], [prefix], table.hands_played, tuple(decisions), tuple(failures)
        )
    assert table._hand is None
    prefix_hands = table.hands_played
    if table.rules.resampling_continuations == 1:
        result = await _play(table, bots, list(memory), list(failures), list(decisions))
        return ResampledTable([table], [result], prefix_hands, tuple(decisions), tuple(failures))
    checkpoint = copy.deepcopy(table)
    tables, results = [], []
    for branch in range(table.rules.resampling_continuations):
        continuation = copy.deepcopy(checkpoint)
        if branch:
            continuation._seed = f"{checkpoint._seed}:continuation:{branch}"
        result = await _play(continuation, bots, list(memory), list(failures), list(decisions))
        tables.append(continuation)
        results.append(result)
    return ResampledTable(
        tables, results, checkpoint.hands_played, tuple(decisions), tuple(failures)
    )
