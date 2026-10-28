"""A record of every tool call, which the model itself can read.

The loop guard (`kairo.guard`) stops a model that repeats itself, but a
model that has been refused cannot see the pattern across its own calls,
and a person checking a long run has only the client's transcript. So
each call is kept: in memory for `recent_calls`, and as one JSON line in
``calls.jsonl`` in Kairo's cache folder, which is Kairo's own and not a
project. The file starts afresh past `MAX_BYTES`, keeping one older copy.

A record holds the start of the arguments and of a refusal's message,
not all of either: enough to see what was sent and why it failed.

The idea of a call log a model reads came from reading Konnect's
documentation; this design is Kairo's own (docs/research.md).
"""

from __future__ import annotations

import json
import statistics
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

#: Calls kept in memory for `recent_calls`.
KEPT = 200
#: Characters kept of the arguments, and of a refusal's message.
SHOWN = 300
#: The log file is started afresh past this size, in bytes.
MAX_BYTES = 2_000_000
#: The file name inside the cache folder.
FILE_NAME = "calls.jsonl"

Outcome = Literal["ok", "refused", "error"]


@dataclass(frozen=True, slots=True)
class Call:
    """One tool call as it is recorded.

    Attributes:
        at: When it finished, as local time to the second.
        tool: The tool's name.
        seconds: How long it ran.
        outcome: ``ok``; ``refused`` (a stable code was given); or
            ``error`` (it failed some other way).
        code: The refusal's code, or the error's type; empty for ``ok``.
        arguments: The start of the arguments as JSON.
        message: The start of the refusal's or error's message.
    """

    at: str
    tool: str
    seconds: float
    outcome: Outcome
    code: str = ""
    arguments: str = ""
    message: str = ""


def _clip(text: str) -> str:
    return text if len(text) <= SHOWN else text[: SHOWN - 1] + "…"


class CallLog:
    """Every call this server has answered, newest last.

    Args:
        folder: Where to keep the log file, or ``None`` to keep calls in
            memory only. A file that cannot be written is given up on
            silently: losing the log must never fail a tool call.
    """

    def __init__(self, folder: Path | None) -> None:
        """Start an empty log."""
        self._calls: deque[Call] = deque(maxlen=KEPT)
        self._totals: dict[str, list[float]] = {}
        self._failures: dict[str, dict[str, int]] = {}
        self._lock = threading.Lock()
        self.path = folder / FILE_NAME if folder is not None else None

    def record(
        self,
        tool: str,
        seconds: float,
        outcome: Outcome,
        *,
        code: str = "",
        arguments: str = "",
        message: str = "",
    ) -> Call:
        """Keep one call.

        Args:
            tool: The tool's name.
            seconds: How long it ran.
            outcome: ``ok``, ``refused`` or ``error``.
            code: The refusal's code or the error's type.
            arguments: The arguments as JSON.
            message: The refusal's or error's message.

        Returns:
            The call as recorded.
        """
        call = Call(
            time.strftime("%Y-%m-%dT%H:%M:%S"),
            tool,
            round(seconds, 3),
            outcome,
            code,
            _clip(arguments),
            _clip(message),
        )
        with self._lock:
            self._calls.append(call)
            self._totals.setdefault(tool, []).append(call.seconds)
            if outcome != "ok":
                counts = self._failures.setdefault(tool, {})
                counts[code or outcome] = counts.get(code or outcome, 0) + 1
            self._write(call)
        return call

    def _write(self, call: Call) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                self.path.replace(self.path.with_suffix(".jsonl.1"))
            with self.path.open("a", encoding="utf-8") as out:
                out.write(json.dumps(asdict(call), ensure_ascii=False) + "\n")
        except OSError:
            self.path = None

    def recent(
        self, limit: int = 20, tool: str | None = None, failed_only: bool = False
    ) -> dict[str, Any]:
        """The latest calls and each tool's counts, for `recent_calls`.

        Args:
            limit: The most calls to return, newest last.
            tool: Only this tool's calls, if given.
            failed_only: Only calls that were refused or failed.

        Returns:
            ``calls``; ``by_tool``, each tool's calls, median seconds and
            failures by code, since the server started; and ``log_file``.
        """
        with self._lock:
            chosen = [
                c
                for c in self._calls
                if (tool is None or c.tool == tool)
                and (not failed_only or c.outcome != "ok")
            ]
            by_tool = {
                name: {
                    "calls": len(times),
                    "median_seconds": round(statistics.median(times), 3),
                    "failures": dict(self._failures.get(name, {})),
                }
                for name, times in sorted(self._totals.items())
                if tool is None or name == tool
            }
        shown = chosen[-max(limit, 0) :] if limit > 0 else []
        return {
            "calls": [asdict(c) for c in shown],
            "matching": len(chosen),
            "by_tool": by_tool,
            "log_file": str(self.path) if self.path is not None else None,
        }
