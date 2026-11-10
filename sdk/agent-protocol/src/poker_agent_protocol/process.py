"""Bot processes over stdin/stdout. The command decides the isolation.

`python -m poker_agent_sdk.guest BOT_DIR` runs a bot as a plain process for trusted local
play; in production the command connects to a copy of the bot inside its microVM. Bots are
stateless, so a `BotPool` of identical copies answers any table's request with whichever copy
is free. Within one copy, requests go strictly one at a time:

- Each deadline starts when its request is sent, so one table never spends another's time.
  It is the decision limit, plus the delivery allowance, plus what is left of the bot's time
  bank at that table. Only time beyond the limit and allowance is taken from the bank.
- A reply already buffered when the deadline passes still counts: host lag never folds a bot.
- After a timeout, the next request first waits (uncharged, up to `late_ms`) for the late
  reply, so one stall costs one decision. That waiting holds up the bot's other tables, so
  it draws on a per-process `stall_budget_ms`; a bot that overspends it, or is still busy
  after `late_ms`, is killed. Occasional stalls are fine; a habitual staller cannot drag a
  job out.
- Any reply that is not for the outstanding request, or memory over the table's limit (the
  SDK cuts honest bots' memory to fit), breaks the protocol: the bot is killed.
- A killed copy restarts on its next request. After `max_starts` starts it is given up on,
  and the pool uses its other copies; with none left, the bot plays passively.
- A launcher that cannot start the sandbox at all (exit status `SANDBOX_UNAVAILABLE`, or a
  command that cannot run) raises `SandboxUnavailable` instead: that start is not charged to
  the bot, and the caller fails the work rather than play the bot passively.
"""

import asyncio
import collections
import contextlib
import dataclasses
import json
import os
import signal
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from time import perf_counter
from typing import Any
from uuid import uuid4

from poker_agent_protocol.game import GameState, action_from_dict, state_to_dict
from poker_agent_protocol.runner import BotError, BotTimeout, Reply, SandboxUnavailable
from poker_agent_protocol.wire import HEADER, ProtocolError, decode, encode_act, frame_lengths

ERROR_CHARS = 1000  # Of a bot's own error report.
# The launcher's exit status when the machine, not the bot, failed to start its sandbox
# (EX_TEMPFAIL). Only the launcher can choose it: a sandboxed bot never exits the launcher.
SANDBOX_UNAVAILABLE = 75
# Bots never inherit the host environment, which may hold worker credentials.
CLEAN_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LC_ALL": "C.UTF-8", "HOME": "/tmp"}


def plain(text: str) -> str:
    """A bot's own text, safe to store and send on: no NULs or other control characters
    (Postgres and JSON reject some), and no lone surrogates (UTF-8 cannot encode them)."""
    text = text.encode("utf-8", "replace").decode("utf-8")
    return "".join(
        c if c in "\n\t" or c.isprintable() else "\N{REPLACEMENT CHARACTER}" for c in text
    )


class BrokenStream(Exception):
    """The bot closed its output or sent bytes that are not a valid frame."""


class BotProcess:
    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] = CLEAN_ENV,
        startup_ms: int = 30_000,
        restart_ms: int = 15_000,
        grace_ms: int = 2,
        late_ms: int = 250,
        stall_budget_ms: int = 2000,
        max_starts: int = 3,
        log_bytes: int = 64 * 1024,
        max_reply_bytes: int = 16 * 1024,
    ) -> None:
        """`grace_ms` covers message delivery beyond a decision's budget."""
        self.argv = tuple(argv)
        self.cwd = cwd
        self.env = dict(env)
        # The first start may boot a VM and import torch; a restart forks a warm copy, so a
        # bot cannot hold its tables for the full startup time again and again.
        self.startup_ms = startup_ms
        self.restart_ms = restart_ms
        self.grace_ms = grace_ms
        self.late_ms = late_ms
        self.stall_budget_ms = stall_budget_ms
        self.max_starts = max_starts
        self.log_bytes = log_bytes
        # Replies are a few hundred bytes; decoding a padded megabyte would stall every table.
        self.max_reply_bytes = max_reply_bytes
        self.logs = bytearray()
        # Why the bot failed, most recent last: its own tracebacks (from error replies) and our
        # reasons for killing it. Bounded, and untrusted: show it only to the team and admins.
        self.errors: collections.deque[str] = collections.deque(maxlen=20)
        self.generation = 0  # Increases with every process start.
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._next_id = 0
        self._late: int | None = None  # Request sent but not yet answered.
        self._stalled = 0.0  # Seconds other requests waited on this process's late replies.
        # A header read before a deadline interrupted the frame, and the request's memory limit.
        self._lengths: tuple[int, int] | None = None
        self._memory_limit = 0
        self._truncated = False  # Whether this start's memory cut was reported yet.
        self._drain: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._process is not None

    @property
    def gave_up(self) -> bool:
        return self._process is None and self.generation >= self.max_starts

    def take_errors(self) -> list[str]:
        """The recorded failures since the last call."""
        errors = list(self.errors)
        self.errors.clear()
        return errors

    def _fail(self, reason: str, elapsed_ms: float | None = None) -> BotError:
        self.errors.append(reason)
        return BotError(reason, elapsed_ms=elapsed_ms)

    def _bot_error(
        self, reply: dict[str, Any], reason: str, elapsed_ms: float | None = None
    ) -> BotError:
        detail = reply.get("detail")
        text = plain(detail[-ERROR_CHARS:]) if isinstance(detail, str) else ""
        self.errors.append(text or reason)
        return BotError(reason, elapsed_ms=elapsed_ms)

    async def stop(self) -> None:
        async with self._lock:
            await self._kill()

    async def start(self) -> None:
        """Start the process now rather than on its first request; BotError if it cannot."""
        async with self._lock:
            if self._process is None:
                await self._start()

    async def act(self, state: GameState, memory: bytes) -> Reply:
        return await self.act_json(
            json.dumps(state_to_dict(state), separators=(",", ":")).encode(),
            memory,
            decision_ms=state.config.decision_ms,
            time_bank_ms=state.time_bank_ms,
            memory_bytes=state.config.memory_bytes,
        )

    async def act_json(
        self,
        state: bytes,
        memory: bytes,
        *,
        decision_ms: float,
        time_bank_ms: float,
        memory_bytes: int,
    ) -> Reply:
        """`act` with the state already encoded as JSON, so a sampling server can pass an
        executor's request through without parsing it."""
        async with self._lock:
            if self._process is None:
                await self._start()
            self._memory_limit = memory_bytes
            free_ms = decision_ms + self.grace_ms
            reply, new_memory, elapsed_ms = await self._request(
                state, memory, budget_ms=decision_ms + time_bank_ms
            )
            bank_ms = min(time_bank_ms, max(0.0, elapsed_ms - free_ms))
            if reply.get("type") != "action":
                raise self._bot_error(reply, "The bot's act raised an error.", elapsed_ms)
            if reply.get("truncated") is True and not self._truncated:
                self._truncated = True
                self.errors.append(
                    f"The bot's memory was longer than {self._memory_limit} bytes and was cut"
                    " to that length."
                )
            try:
                return Reply(action_from_dict(reply.get("action")), new_memory, bank_ms, elapsed_ms)
            except ValueError:
                raise self._fail("The bot returned an invalid action.", elapsed_ms) from None

    async def _request(
        self, state: bytes, memory: bytes, *, budget_ms: float
    ) -> tuple[dict[str, Any], bytes, float]:
        """Send one request; its reply, and the milliseconds from sending to reading it. Call
        with the lock held."""
        loop = asyncio.get_running_loop()
        if self._late is not None:
            # Not charged to this request: wait for the previous one to finish first.
            started = loop.time()
            try:
                await self._expect(self._late, started + self.late_ms / 1000, late=True)
            finally:
                self._stalled += loop.time() - started
            if self._stalled * 1000 > self.stall_budget_ms:
                await self._kill()
                raise self._fail("The bot stalled its tables for too long.")
        self._next_id += 1
        request = self._late = self._next_id  # Outstanding until answered, even if cancelled.
        await self._send(encode_act(request, state, memory))
        sent = loop.time()
        reply, new_memory = await self._expect(request, sent + (budget_ms + self.grace_ms) / 1000)
        return reply, new_memory, (loop.time() - sent) * 1000

    async def _expect(
        self, request: int, deadline: float, *, late: bool = False
    ) -> tuple[dict[str, Any], bytes]:
        """The reply to `request`. Any other frame breaks the protocol."""
        try:
            reply = await self._receive(deadline)
        except BotTimeout:
            if late:
                await self._kill()
                raise self._fail("The bot stopped responding.") from None
            raise
        if reply[0].get("id") != request:
            await self._kill()
            raise self._fail("The bot sent a reply to a request it was not asked.")
        self._late = None
        return reply

    async def _start(self) -> None:
        if self.generation >= self.max_starts:
            raise self._fail("The bot failed too many times to restart.")
        self.generation += 1
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env=self.env,
                start_new_session=True,
            )
        except OSError as error:
            self.generation -= 1  # The machine's failure, not one of the bot's starts.
            raise SandboxUnavailable(f"The bot's launcher could not run: {error}") from error
        self._late = self._lengths = None
        self._stalled = 0.0
        self._truncated = False
        self._drain = asyncio.create_task(self._keep_logs(self._process))
        allowed = self.startup_ms if self.generation == 1 else min(self.restart_ms, self.startup_ms)
        deadline = asyncio.get_running_loop().time() + allowed / 1000
        try:
            reply, _ = await self._receive(deadline, starting=True)
            if reply.get("type") == "error":  # It could not even load: say why.
                raise self._bot_error(reply, "The bot could not load.")
            if reply.get("type") != "ready":
                raise self._fail("The bot did not report ready.")
        except BaseException as error:  # Including cancellation: never leave a process behind.
            await self._kill()
            if isinstance(error, SandboxUnavailable):
                self.generation -= 1
            raise

    async def _kill(self) -> int | None:
        """Kill the process group and reap it, never waiting long on what the bot left open.
        Returns its exit status, if it was reaped in time."""
        process, self._process = self._process, None
        self._late = None
        if process is None:
            return None
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        # Unread output would keep the pipe transport paused and wait() blocked forever.
        assert process.stdout is not None
        unread = asyncio.create_task(process.stdout.read())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), 1)
        if self._drain is not None:  # Keep the bot's last error messages, briefly.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._drain, 0.5)
        unread.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await unread
        # A child that left the process group may still hold the pipes; release our ends.
        transport = getattr(process, "_transport", None)
        if transport is not None:
            transport.close()
        return process.returncode

    def _launcher_reason(self) -> str:
        """The launcher's last word on why the sandbox did not start, for the organizers."""
        text = plain(self.logs.decode("utf-8", "replace"))
        reasons = [line.strip() for line in text.splitlines() if line.strip()]
        return reasons[-1][-300:] if reasons else "The machine could not start the bot's sandbox."

    async def _send(self, frame: bytes) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise BotError("The bot is not running.")
        process.stdin.write(frame)
        try:
            await asyncio.wait_for(process.stdin.drain(), self.late_ms / 1000)
        except (TimeoutError, ConnectionError):
            await self._kill()
            raise self._fail("The bot stopped reading its input.") from None

    async def _receive(
        self, deadline: float, *, starting: bool = False
    ) -> tuple[dict[str, Any], bytes]:
        loop = asyncio.get_running_loop()
        # The second, immediate attempt reads a reply that arrived while the loop was busy:
        # a past deadline still lets already-buffered data be consumed without waiting.
        for limit in (deadline, loop.time()):
            try:
                async with asyncio.timeout_at(max(limit, loop.time())):
                    return await self._read_frame()
            except TimeoutError:
                continue
            except BrokenStream:
                # Outside the timeout, so the kill itself is never cancelled.
                status = await self._kill()
                if starting and status == SANDBOX_UNAVAILABLE:
                    raise SandboxUnavailable(self._launcher_reason()) from None
                raise self._fail("The bot exited or sent an invalid message.") from None
        raise BotTimeout("The bot missed its deadline.")

    async def _read_frame(self) -> tuple[dict[str, Any], bytes]:
        process = self._process
        if process is None or process.stdout is None:
            raise BotError("The bot is not running.")
        try:
            # readexactly consumes nothing when cancelled, so a timeout never splits a frame.
            if self._lengths is None:
                self._lengths = frame_lengths(await process.stdout.readexactly(HEADER.size))
                length, memory = self._lengths
                if length > self.max_reply_bytes or memory > self._memory_limit:
                    raise BrokenStream
            length, memory = self._lengths
            data = await process.stdout.readexactly(length + memory)
            self._lengths = None
            return decode(data[:length]), data[length:]
        except (asyncio.IncompleteReadError, ProtocolError):
            raise BrokenStream from None

    async def _keep_logs(self, process: asyncio.subprocess.Process) -> None:
        assert process.stderr is not None
        while chunk := await process.stderr.read(64 * 1024):
            self.logs += chunk
            del self.logs[: max(0, len(self.logs) - self.log_bytes)]


# Fixed, non-cumulative buckets, followed by one overflow bucket. A snapshot can report
# approximate latency percentiles without storing a growing list of individual decisions.
DIAGNOSTIC_BUCKET_MS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 5000)


@dataclasses.dataclass
class _Durations:
    count: int = 0
    sum_ms: float = 0.0
    max_ms: float = 0.0
    buckets: list[int] = dataclasses.field(
        default_factory=lambda: [0] * (len(DIAGNOSTIC_BUCKET_MS) + 1)
    )

    def add(self, seconds: float) -> None:
        milliseconds = max(0.0, seconds * 1000)
        self.count += 1
        self.sum_ms += milliseconds
        self.max_ms = max(self.max_ms, milliseconds)
        self.buckets[bisect_left(DIAGNOSTIC_BUCKET_MS, milliseconds)] += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "sum_ms": self.sum_ms,
            "max_ms": self.max_ms,
            "buckets": self.buckets.copy(),
        }


class _PoolDiagnostics:
    """Optional counters for one pool's lifetime. No observation list or bot data."""

    def __init__(self) -> None:
        self.pool_id = uuid4().hex
        self.created = self.queue_changed = perf_counter()
        self.queued = self.peak_queued = self.peak_active = 0
        self.queued_ms = 0.0
        self.completed = self.errors = self.cancelled = self.queue_errors = 0
        self.queue_wait = _Durations()
        self.occupancy = _Durations()
        self.cancelled_queue_wait = _Durations()
        # At most one acquisition per configured copy, including its restart/late-reply wait.
        self.taken: dict[BotProcess, float] = {}

    def queue_change(self, now: float, delta: int) -> None:
        self.queued_ms += self.queued * (now - self.queue_changed) * 1000
        self.queue_changed = now
        self.queued += delta
        self.peak_queued = max(self.peak_queued, self.queued)

    def snapshot(self, copies: int) -> dict[str, Any]:
        now = perf_counter()
        return {
            "enabled": True,
            "pool_id": self.pool_id,
            "copies": copies,
            "elapsed_seconds": now - self.created,
            "bucket_upper_ms": list(DIAGNOSTIC_BUCKET_MS),
            "queued": self.queued,
            "active": len(self.taken),
            "peak_queued": self.peak_queued,
            "peak_active": self.peak_active,
            # Integrals include unfinished requests, so differences between snapshots do
            # not lose the occupancy of a copy, or a queue wait spanning both snapshots.
            "queued_ms": self.queued_ms + self.queued * (now - self.queue_changed) * 1000,
            "occupied_ms": self.occupancy.sum_ms
            + sum((now - acquired) * 1000 for acquired in self.taken.values()),
            "queue_wait": self.queue_wait.snapshot(),
            "occupancy": self.occupancy.snapshot(),
            "cancelled_queue_wait": self.cancelled_queue_wait.snapshot(),
            "completed": self.completed,
            "errors": self.errors,
            "cancelled": self.cancelled,
            "queue_errors": self.queue_errors,
        }


class BotPool:
    """Identical copies of one bot; each request goes to whichever copy is free. Implements
    `Bot`. Copies that were given up on leave the rotation while others still work."""

    def __init__(
        self, argv: Sequence[str], *, copies: int = 1, diagnostics: bool = False, **options: Any
    ) -> None:
        self.processes = [BotProcess(argv, **options) for _ in range(copies)]
        # One error log for every copy, so the latest errors are the latest of any copy.
        for process in self.processes[1:]:
            process.errors = self.processes[0].errors
        self._idle: asyncio.Queue[BotProcess] = asyncio.Queue()
        for process in self.processes:
            self._idle.put_nowait(process)
        self._diagnostics = _PoolDiagnostics() if diagnostics else None

    def diagnostics(self) -> dict[str, Any]:
        """Non-consuming, bounded counters since this pool was created. Queue waits do not
        include network/server dispatch; occupancy includes request-triggered startup, late
        replies and errors. Histograms count finished acquisitions/waits; integrals also
        include in-flight work. An immediate acquisition can set peak_queued to one, and
        its small queue_wait includes the queue lookup and timing overhead.
        """
        if self._diagnostics is None:
            return {"enabled": False, "copies": len(self.processes)}
        return self._diagnostics.snapshot(len(self.processes))

    async def _take(self) -> BotProcess:
        stats = self._diagnostics
        if stats is None:
            return await self._idle.get()
        started = perf_counter()
        stats.queue_change(started, 1)
        try:
            process = await self._idle.get()
        except BaseException as error:
            ended = perf_counter()
            stats.queue_change(ended, -1)
            if isinstance(error, asyncio.CancelledError):
                stats.cancelled_queue_wait.add(ended - started)
            else:
                stats.queue_errors += 1
            raise
        acquired = perf_counter()
        stats.queue_change(acquired, -1)
        stats.queue_wait.add(acquired - started)
        stats.taken[process] = acquired
        stats.peak_active = max(stats.peak_active, len(stats.taken))
        return process

    def _release(self, process: BotProcess, error: BaseException | None) -> None:
        stats = self._diagnostics
        if stats is not None:
            stats.occupancy.add(perf_counter() - stats.taken.pop(process))
            if isinstance(error, asyncio.CancelledError):
                stats.cancelled += 1
            elif error is not None:
                stats.errors += 1
            else:
                stats.completed += 1
        if not process.gave_up or self.gave_up:
            self._idle.put_nowait(process)

    @property
    def gave_up(self) -> bool:
        return all(process.gave_up for process in self.processes)

    @property
    def logs(self) -> bytes:
        return b"".join(bytes(process.logs) for process in self.processes)

    def generations(self) -> list[int]:
        return [process.generation for process in self.processes]

    def take_errors(self) -> list[str]:
        return self.processes[0].take_errors()

    async def start(self) -> None:
        # Wait for every copy to finish even when one fails first. Otherwise callers can
        # report a half-started pool, or play against copies still failing their imports.
        outcomes = await asyncio.gather(
            *(process.start() for process in self.processes), return_exceptions=True
        )
        # A machine failure must never be hidden by another copy's bot failure.
        for outcome in outcomes:
            if isinstance(outcome, BaseException) and not isinstance(outcome, BotError):
                raise outcome
        for outcome in outcomes:
            if isinstance(outcome, BotError):
                raise outcome

    async def act(self, state: GameState, memory: bytes) -> Reply:
        process = await self._take()
        error: BaseException | None = None
        try:
            return await process.act(state, memory)
        except BaseException as caught:
            error = caught
            raise
        finally:
            self._release(process, error)

    async def act_json(self, state: bytes, memory: bytes, **limits: Any) -> Reply:
        """`BotProcess.act_json` on whichever copy is free; waiting for one is not charged."""
        process = await self._take()
        error: BaseException | None = None
        try:
            return await process.act_json(state, memory, **limits)
        except BaseException as caught:
            error = caught
            raise
        finally:
            self._release(process, error)

    async def stop(self) -> None:
        await asyncio.gather(*(process.stop() for process in self.processes))
