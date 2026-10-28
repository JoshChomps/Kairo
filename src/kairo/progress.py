"""Tell a waiting client that a long tool call is still working.

Some calls take minutes: the first library index, a build that runs
KiCad's rule check, a worst-case sweep. An MCP client that hears nothing
may give up on the call, and the model behind it cannot tell a slow
answer from a lost one. While a tool runs, the server sends a progress
notification every `INTERVAL_S` seconds (to a client that asked for
them), saying how long it has run and the stage the work last named.

Work names its stage with `stage`, from whatever thread it runs on; a
call with no listener ignores it, so the same code runs outside the
server and in tests.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar

import anyio

# Often enough that a client's own timeout (commonly a minute or more,
# reset by each notification where the client supports that) does not
# run out, and seldom enough to cost nothing.
INTERVAL_S = 5.0

_listener: ContextVar[Callable[[str], None] | None] = ContextVar(
    "kairo_progress", default=None
)


def stage(name: str) -> None:
    """Name what the current call is doing now.

    Args:
        name: A few words, such as "running KiCad's rule check".
    """
    listener = _listener.get()
    if listener is not None:
        listener(name)


@contextmanager
def listening(listener: Callable[[str], None]) -> Generator[None]:
    """Send the stages named inside this block to a listener.

    A worker thread started inside the block with anyio sees the same
    listener, since anyio runs it in a copy of this context.

    Args:
        listener: Called with each stage's name.

    Yields:
        Nothing.
    """
    token = _listener.set(listener)
    try:
        yield
    finally:
        _listener.reset(token)


async def heartbeat(
    send: Callable[[float, str], Awaitable[None]],
    tool: str,
    latest: Callable[[], str],
    interval_s: float | None = None,
) -> None:
    """Send progress every interval until cancelled.

    Args:
        send: Sends one notification: seconds so far (which only grows,
            as MCP requires of progress) and a message.
        tool: The tool's name, for the message.
        latest: The stage last named, or "" if none.
        interval_s: Seconds between notifications; `INTERVAL_S` if not
            given.
    """
    every = INTERVAL_S if interval_s is None else interval_s
    started = time.monotonic()
    while True:
        await anyio.sleep(every)
        elapsed = time.monotonic() - started
        doing = latest() or "working"
        await send(round(elapsed, 1), f"{tool}: {doing} ({elapsed:.0f} s so far)")
