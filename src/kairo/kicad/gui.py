"""Open a project in the KiCad window, for a person to look at.

Kairo cannot make a running KiCad 10 reload a schematic: measured on
KiCad 10.0.6 with its API server on, the schematic editor answers
``GetOpenDocuments`` and has no handler for ``RefreshEditor``,
``RevertDocument`` or ``RunAction`` (KiCad's developer documentation
gives schematic editor support to KiCad 11). So all that can be done
from outside is to start KiCad on the project, and to tell from KiCad's
own lock file that it is already open.

The lock file's name and contents were read from one KiCad 10.0.6 left
beside a project it had open (2026-10-10):
``~<project>.kicad_pro.lck`` holding ``{"hostname": ..., "username":
...}``. The schematic editor started on a lone sheet, with no project
file beside it, left no lock file.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

#: Windows process flags: no console of its own, and not in the caller's
#: process group, so the window outlives the server that opened it.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def find_kicad(kicad_cli: Path) -> Path | None:
    """The KiCad program installed beside a kicad-cli.

    Args:
        kicad_cli: The kicad-cli executable.

    Returns:
        ``kicad`` (``kicad.exe`` on Windows) in the same folder, or
        ``None`` if there is none.
    """
    name = "kicad.exe" if kicad_cli.suffix.lower() == ".exe" else "kicad"
    candidate = kicad_cli.parent / name
    return candidate if candidate.is_file() else None


def lock_file(project: Path) -> Path:
    """Where KiCad keeps its lock while it has a project open.

    Args:
        project: The ``.kicad_pro`` file.

    Returns:
        The lock file's path, whether or not it exists.
    """
    return project.with_name(f"~{project.name}.lck")


def holder(project: Path) -> str | None:
    """Who has a project open, by KiCad's lock file.

    A lock file can outlive a KiCad that was killed, so this says the
    project appears to be open, not that it is.

    Args:
        project: The ``.kicad_pro`` file.

    Returns:
        ``<user> on <machine>`` from the lock file, ``someone`` if the
        file cannot be read, or ``None`` if there is no lock file.
    """
    lock = lock_file(project)
    if not lock.is_file():
        return None
    try:
        data = json.loads(lock.read_text(encoding="utf-8"))
        return f"{data['username']} on {data['hostname']}"
    except (OSError, ValueError, KeyError, TypeError):
        return "someone"


def open_project(kicad: Path, target: Path) -> None:
    """Start KiCad on a project and leave it running.

    Args:
        kicad: The KiCad program, from `find_kicad`.
        target: The ``.kicad_pro`` file to open.

    Raises:
        OSError: If the program cannot be started.
    """
    flags = (
        _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    )
    subprocess.Popen(
        [str(kicad), str(target)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        start_new_session=sys.platform != "win32",
    )
