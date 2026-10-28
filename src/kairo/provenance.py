"""Which build of Kairo is serving, and whether it is still current.

A client keeps a server running for as long as it is open, so a server
started before an edit runs the old code until the client restarts it,
although the files on disk are new. The Claude Desktop extension adds a
second way to fall behind: its manifest lists the tools as they were when
it was built. Neither can be seen from the client, so `server_info` says
what this process observes. Anything it cannot observe is ``None`` or
"unknown", never guessed from a folder name.

The idea of a tool that reports the serving build came from reading
Konnect's documentation; this design is Kairo's own (docs/research.md).
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kairo.process import ProcessError, run_process

#: Seconds to wait for git, which answers these in well under one.
GIT_TIMEOUT_S = 10.0
#: The most changed files named in an answer.
NAMED = 5


@dataclass(frozen=True, slots=True)
class Start:
    """What was true of Kairo's own code when this process started.

    Attributes:
        at: When the process started, in seconds since the epoch.
        package: The folder the ``kairo`` package was imported from.
        commit: The git commit of that folder's checkout, or ``None`` if
            it is not in a checkout or git could not be run.
        uncommitted: How many files under ``package`` differed from that
            commit, or ``None`` when the commit is unknown.
        stamps: Each source file's modification time, in nanoseconds.
    """

    at: float
    package: Path
    commit: str | None
    uncommitted: int | None
    stamps: dict[str, int]


def _sources(package: Path) -> Iterable[Path]:
    return (p for p in package.rglob("*.py") if "__pycache__" not in p.parts)


def _stamps(package: Path) -> dict[str, int]:
    found: dict[str, int] = {}
    for path in _sources(package):
        try:
            found[path.relative_to(package).as_posix()] = path.stat().st_mtime_ns
        except OSError:
            continue
    return found


def _git(package: Path, *args: str) -> str | None:
    try:
        result = run_process(
            ["git", "-C", str(package), *args], timeout_s=GIT_TIMEOUT_S
        )
    except (OSError, ProcessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def capture(package: Path | None = None) -> Start:
    """Record Kairo's code as it is now, once, when the server starts.

    Args:
        package: The package folder; this module's own if ``None``.

    Returns:
        The record, with the commit ``None`` where git could not say.
    """
    folder = package or Path(__file__).resolve().parent
    head = _git(folder, "rev-parse", "HEAD")
    commit = (head or "").strip() or None
    uncommitted: int | None = None
    if commit is not None:
        status = _git(
            folder, "status", "--porcelain", "--untracked-files=normal", "--", "."
        )
        if status is not None:
            uncommitted = sum(1 for line in status.splitlines() if line.strip())
    return Start(time.time(), folder, commit, uncommitted, _stamps(folder))


def changed_since(start: Start) -> list[str]:
    """Kairo's source files added, removed or changed since the process started.

    Args:
        start: The record made by `capture`.

    Returns:
        The files' paths inside the package, sorted. Any at all means the
        process is running older code than is on disk.
    """
    now = _stamps(start.package)
    names = set(now) | set(start.stamps)
    return sorted(n for n in names if now.get(n) != start.stamps.get(n))


def bundle_check(
    bundle_dir: Path | None, offered: Iterable[str], version: str
) -> dict[str, Any]:
    """Compare an installed extension's manifest with what this server offers.

    Args:
        bundle_dir: The folder the extension was unpacked into (the
            launcher passes its own folder), or ``None`` if Kairo was not
            started from an extension.
        offered: The names of every tool this build has, at any tier.
        version: This build's version.

    Returns:
        ``status`` (``not_an_extension``, ``unreadable``, ``current`` or
        ``rebuild``), and for a manifest that was read, the tools missing
        from it and those it lists that this build lacks.
    """
    if bundle_dir is None:
        return {"status": "not_an_extension"}
    manifest = bundle_dir / "manifest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        listed = {str(tool["name"]) for tool in data.get("tools", [])}
        built_as = str(data.get("version", ""))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {"status": "unreadable", "manifest": str(manifest), "reason": str(exc)}
    have = set(offered)
    missing = sorted(have - listed)
    extra = sorted(listed - have)
    current = not missing and not extra and built_as == version
    return {
        "status": "current" if current else "rebuild",
        "manifest": str(manifest),
        "manifest_version": built_as,
        "missing_from_manifest": missing,
        "only_in_manifest": extra,
    }


def describe(start: Start) -> dict[str, Any]:
    """The code part of `server_info`'s answer.

    Args:
        start: The record made by `capture` when the server started.

    Returns:
        Where the code runs from, its commit, and whether the process
        needs restarting to run what is now on disk.
    """
    changed = changed_since(start)
    return {
        "package": str(start.package),
        "python": sys.executable,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(start.at)),
        "commit": start.commit or "unknown",
        "uncommitted_files_at_start": start.uncommitted,
        "restart_needed": bool(changed),
        "changed_since_start": changed[:NAMED],
        "changed_count": len(changed),
    }
