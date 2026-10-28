"""Stop a model that has fallen into a loop before it spends without end.

A model on a paid service is charged for every call it makes, and one
that has lost its way can call tools for as long as it is let. Three
limits end that, and each tells the model plainly to stop and report
to the person:

- **the same call with the same answer, several times running.** Asking
  again cannot give anything new, so the call is refused. A call whose
  answer changed (the person edited the schematic in between) starts
  the count again.
- **the same tool failing the same way, several times running.** A
  model that sends one tool a wrong shape again and again, changing
  it a little each time, is refused that tool until some other call
  has succeeded, which is what reading the schema or the pins is.
- **too many calls in a short time.** More calls in ten minutes than a
  person working with a model makes is taken to be a loop, and calls
  are refused until the ten minutes have passed.

The write tools keep their own, stricter limit on failed attempts (see
`kairo.design_tools`); these three cover every tool. Someone running
a model of their own, where a long run costs nothing, can turn the
last limit off.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import deque
from collections.abc import Callable

#: How many times running the same call may give the same answer before
#: it is refused.
REPEATS = 3
#: How many times running one tool may fail with the same error code
#: before it is refused.
FAILURES = 5
#: The most calls in one window before calls are refused.
CALLS_PER_WINDOW = 150
#: The window, in seconds.
WINDOW_S = 600.0


class LoopError(RuntimeError):
    """A call is refused because the caller looks to be in a loop.

    Attributes:
        code: ``E_REPEATED_CALL``, ``E_REPEATED_FAILURE`` or
            ``E_TOO_MANY_CALLS``.
    """

    def __init__(self, code: str, message: str) -> None:
        """Keep the code beside the message."""
        super().__init__(f"{code}: {message}")
        self.code = code


class CallGuard:
    """Counts a server's tool calls and refuses those of a loop.

    One guard serves a whole server run, and tools may run on several
    threads, so it keeps its counts under a lock.
    """

    def __init__(
        self,
        *,
        repeats: int = REPEATS,
        failures: int = FAILURES,
        calls_per_window: int = CALLS_PER_WINDOW,
        window_s: float = WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Set the limits.

        Args:
            repeats: How many times running the same call may give the
                same answer; the next is refused.
            failures: How many times running one tool may fail with the
                same error code; the next call of it is refused.
            calls_per_window: The most calls in one window; 0 for no
                such limit.
            window_s: The window, in seconds.
            clock: What tells the time, for tests.
        """
        self._repeats = repeats
        self._per_window = calls_per_window
        self._window = window_s
        self._clock = clock
        self._lock = threading.Lock()
        self._times: deque[float] = deque()
        self._last_call = ""
        self._last_answer = ""
        self._same = 0
        self._failures = failures
        self._failed_tool = ""
        self._failed_code = ""
        self._failed = 0

    def before(self, tool: str, arguments: str) -> None:
        """Count a call that is about to be made, or refuse it.

        Args:
            tool: The tool's name.
            arguments: Its arguments as text, the same text for the same
                arguments.

        Raises:
            LoopError: ``E_TOO_MANY_CALLS`` if the window is full;
                ``E_REPEATED_CALL`` if this call has just given the same
                answer the allowed number of times running;
                ``E_REPEATED_FAILURE`` if this tool has just failed the
                same way the allowed number of times running.
        """
        now = self._clock()
        with self._lock:
            while self._times and now - self._times[0] >= self._window:
                self._times.popleft()
            if self._per_window and len(self._times) >= self._per_window:
                wait = self._window - (now - self._times[0])
                raise LoopError(
                    "E_TOO_MANY_CALLS",
                    f"{len(self._times)} tool calls were made in the last "
                    f"{self._window / 60:g} minutes, which is more than working "
                    "through a design takes. Stop calling tools and tell the "
                    f"user what you were trying to do. Calls are accepted again "
                    f"in {wait:.0f} s.",
                )
            self._times.append(now)
            if (
                f"{tool}\n{arguments}" == self._last_call
                and self._same >= self._repeats
            ):
                raise LoopError(
                    "E_REPEATED_CALL",
                    f"{tool} has been called {self._same} times running with the "
                    "same arguments and has given the same answer each time. "
                    "Calling it again will not change that. Stop, and tell the "
                    "user what the answer was and what you need.",
                )
            if (
                self._failures
                and tool == self._failed_tool
                and self._failed >= self._failures
            ):
                raise LoopError(
                    "E_REPEATED_FAILURE",
                    f"{tool} has failed {self._failed} times running with "
                    f"{self._failed_code}. Trying it again a little differently "
                    "is not working. Stop, and tell the user what the error "
                    "says; or read what it points to (the schema, the pins) "
                    "before calling it once more.",
                )

    def after(
        self, tool: str, arguments: str, answer: str, error: str | None = None
    ) -> None:
        """Record what a call answered.

        Args:
            tool: The tool's name.
            arguments: Its arguments as text.
            answer: What it answered, or the text of its error.
            error: The error's code if the call failed, else ``None``.
        """
        call = f"{tool}\n{arguments}"
        digest = hashlib.sha256(answer.encode("utf-8")).hexdigest()
        with self._lock:
            if call == self._last_call and digest == self._last_answer:
                self._same += 1
            else:
                self._last_call = call
                self._last_answer = digest
                self._same = 1
            if error is None:
                self._failed = 0
            elif (tool, error) == (self._failed_tool, self._failed_code):
                self._failed += 1
            else:
                self._failed_tool, self._failed_code = tool, error
                self._failed = 1
