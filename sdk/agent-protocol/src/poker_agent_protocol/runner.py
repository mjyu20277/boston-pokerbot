"""What the engine needs from a bot. Implementations decide where the bot runs."""

from typing import NamedTuple, Protocol

from poker_agent_protocol.game import Action, GameState


class BotError(Exception):
    """The bot errored, crashed, or broke the protocol. This decision plays the passive action.
    `elapsed_ms` is the time the host measured before the bot failed, when it measured one."""

    def __init__(self, *args: object, elapsed_ms: float | None = None) -> None:
        super().__init__(*args)
        self.elapsed_ms = elapsed_ms


class BotTimeout(BotError):
    """The bot missed its deadline. Its late reply is ignored."""


class SandboxUnavailable(Exception):
    """The machine could not start the bot's sandbox. Not the bot's fault, so never passive
    play: the work fails and is retried, and the reason goes to the organizers."""


class Reply(NamedTuple):
    action: Action
    memory: bytes
    # Time this decision took from the table's time bank: what the host measured beyond the
    # decision limit and delivery allowance, at most `state.time_bank_ms`.
    bank_ms: float = 0.0
    # Milliseconds from sending the request to reading the reply, when the host measured it.
    elapsed_ms: float | None = None


class Bot(Protocol):
    """A stateless bot: one call per decision, at any table. What the bot keeps between its
    turns at a table is the memory it returns, which the table hands back next time, and so
    is its time bank."""

    async def act(self, state: GameState, memory: bytes) -> Reply: ...
