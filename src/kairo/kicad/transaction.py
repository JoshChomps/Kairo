"""The only place Kairo writes project files.

Every write goes through a `Session` on one project folder:

- A file is written to a temporary file beside it, flushed to disk, and
  moved into place in one step, so a crash never leaves half a schematic.
- Before a file is replaced, the version on disk is copied to a backup
  folder inside the project (``kairo/backups``).
- If any file of a write fails, or the check run on the written files
  reports a problem, every file is put back as it was.
- A file that changed on disk since the session last read or wrote it is
  never written over: the user has edited it in KiCad, and the caller
  must read it again first (``E_FILE_CHANGED``).
- What each write left is recorded in the project (``kairo/written.json``,
  each file's SHA-256). A later session, in another process, may write
  over a file it has not read only if the file still holds exactly that:
  Kairo wrote it and no one has touched it since. Anything else is refused
  as before.
- Backups are numbered on from the highest already in the backup folder,
  so a later session never writes over an earlier one's.
- Each write reports, per file, how many lines changed and what share of
  the file that is.
- `Session.undo` puts back what the last write replaced.
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

#: Where backups are kept, inside the project folder.
BACKUP_FOLDER = Path("kairo") / "backups"
#: Where each file's digest after Kairo's last write is kept.
WRITTEN_RECORD = Path("kairo") / "written.json"


class TransactionError(OSError):
    """Files could not be written; everything on disk is as it was.

    Attributes:
        code: ``E_FILE_CHANGED``, ``E_WRITE_FAILED``, ``E_BAD_NAME`` or
            ``E_NOTHING_TO_UNDO``.
    """

    def __init__(self, code: str, message: str) -> None:
        """Record what failed.

        Args:
            code: The error code.
            message: What went wrong.
        """
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class FileChange:
    """What a write did to one file.

    Attributes:
        path: The file.
        created: True if the file did not exist before.
        previous_lines: How many lines it had before; 0 for a new file.
        changed_lines: Lines removed or replaced plus lines added, counting
            a replaced run once at the longer of its two lengths.
        ratio: ``changed_lines`` over ``previous_lines``, or ``None`` for
            a new file.
    """

    path: Path
    created: bool
    previous_lines: int
    changed_lines: int
    ratio: float | None


@dataclass(frozen=True, slots=True)
class WriteReport:
    """How a write came out.

    Attributes:
        written: True if the files are now on disk as asked.
        changes: What happened to each file; empty if nothing was kept.
        problems: What the check reported, when that is why nothing was
            kept.
        backup: The folder holding the replaced versions, if any file was
            replaced.
    """

    written: bool
    changes: tuple[FileChange, ...] = ()
    problems: tuple[object, ...] = ()
    backup: Path | None = None


@dataclass(slots=True)
class _Step:
    """One write, as undo needs it."""

    replaced: dict[Path, Path]
    created: list[Path]
    before: dict[Path, str | None]


class Session:
    """Reads and writes on one project folder.

    Args:
        folder: The project folder. It is created by the first write.
    """

    def __init__(self, folder: Path) -> None:
        """Start with nothing read and nothing to undo."""
        self.folder = folder
        self._seen: dict[Path, str | None] = {}
        self._steps: list[_Step] = []
        self._count = _last_backup(folder / BACKUP_FOLDER)
        self._written = _read_record(folder / WRITTEN_RECORD)

    def read(self, name: str) -> str:
        """Read a project file and remember what it held.

        Args:
            name: The file's name within the project folder.

        Returns:
            Its text, line endings untouched.

        Raises:
            TransactionError: If the name leaves the folder.
            OSError: If the file cannot be read.
            UnicodeDecodeError: If it is not UTF-8.
        """
        path = self._path(name)
        data = path.read_bytes()
        self._seen[path] = _digest(data)
        return data.decode("utf-8")

    def write(
        self,
        files: Mapping[str, str],
        check: Callable[[Path], Sequence[object]] | None = None,
    ) -> WriteReport:
        """Write files into the project, all of them or none.

        Args:
            files: Each file's name within the folder and its new content,
                written as UTF-8 exactly as given.
            check: Called with the project folder once the files are in
                place. If it returns anything, those are problems: every
                file is put back and the report carries them.

        Returns:
            The report. ``written`` is False only when ``check`` found
            problems.

        Raises:
            TransactionError: ``E_FILE_CHANGED`` if a file to be replaced
                has changed since this session last read or wrote it, or
                was never read here and is not exactly as Kairo last wrote
                it; ``E_WRITE_FAILED`` if a write fails;
                ``E_BAD_NAME`` if a name leaves the folder. In each case
                the disk is as it was.
        """
        targets = {self._path(name): content for name, content in files.items()}
        previous: dict[Path, bytes | None] = {}
        for path in targets:
            if not path.exists():
                previous[path] = None
                continue
            data = path.read_bytes()
            known = (
                self._seen[path]
                if path in self._seen
                else self._written.get(self._key(path))
            )
            if known != _digest(data):
                raise TransactionError(
                    "E_FILE_CHANGED",
                    f"{path.name} has changed on disk since it was last read "
                    "here, so it was not written over. Read it again first.",
                )
            self._seen[path] = known
            previous[path] = data

        self._count += 1
        backup = self.folder / BACKUP_FOLDER / f"{self._count:04d}"
        step = _Step({}, [], {path: self._seen.get(path) for path in targets})
        try:
            for path, content in targets.items():
                old = previous[path]
                if old is None:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    _write_atomically(path, content.encode("utf-8"))
                    step.created.append(path)
                else:
                    kept = backup / path.relative_to(self.folder)
                    kept.parent.mkdir(parents=True, exist_ok=True)
                    _write_atomically(kept, old)
                    step.replaced[path] = kept
                    _write_atomically(path, content.encode("utf-8"))
        except OSError as exc:
            self._put_back(step)
            raise TransactionError(
                "E_WRITE_FAILED", f"could not write into {self.folder}: {exc}"
            ) from exc

        problems = tuple(check(self.folder)) if check is not None else ()
        if problems:
            self._put_back(step)
            return WriteReport(False, problems=problems)

        changes: list[FileChange] = []
        for path, content in targets.items():
            old = previous[path]
            self._seen[path] = _digest(content.encode("utf-8"))
            changes.append(file_change(path, old, content))
        self._steps.append(step)
        self._record(targets)
        return WriteReport(
            True, tuple(changes), backup=backup if step.replaced else None
        )

    def undo(self) -> list[Path]:
        """Put back what the last write replaced, and remove what it created.

        Returns:
            The files restored or removed.

        Raises:
            TransactionError: ``E_NOTHING_TO_UNDO`` if this session has no
                write left to undo; ``E_FILE_CHANGED`` if a file the write
                touched has changed since, in which case nothing is undone.
        """
        if not self._steps:
            raise TransactionError(
                "E_NOTHING_TO_UNDO", "this session has no write left to undo"
            )
        step = self._steps[-1]
        for path in (*step.replaced, *step.created):
            current = _digest(path.read_bytes()) if path.exists() else None
            if current != self._seen.get(path):
                raise TransactionError(
                    "E_FILE_CHANGED",
                    f"{path.name} has changed on disk since Kairo wrote it, so "
                    "the write was not undone.",
                )
        self._steps.pop()
        self._put_back(step)
        self._record([*step.replaced, *step.created])
        return [*step.replaced, *step.created]

    def _key(self, path: Path) -> str:
        return path.relative_to(self.folder.resolve()).as_posix()

    def _record(self, paths: Sequence[Path] | Mapping[Path, str]) -> None:
        """Note what the given files now hold, or that they are gone.

        A record that cannot be written costs only the next process's
        right to write over these files unread, so it is not an error.
        """
        for path in paths:
            if path.exists():
                self._written[self._key(path)] = _digest(path.read_bytes())
            else:
                self._written.pop(self._key(path), None)
        with contextlib.suppress(OSError):
            record = self.folder / WRITTEN_RECORD
            record.parent.mkdir(parents=True, exist_ok=True)
            body = {"version": 1, "files": dict(sorted(self._written.items()))}
            _write_atomically(record, json.dumps(body, indent=1).encode("utf-8"))

    def _put_back(self, step: _Step) -> None:
        """Restore a write's replaced files and remove the ones it created."""
        for path, kept in step.replaced.items():
            with contextlib.suppress(OSError):
                _write_atomically(path, kept.read_bytes())
        for path in step.created:
            with contextlib.suppress(OSError):
                path.unlink()
        for path, digest in step.before.items():
            if digest is None:
                self._seen.pop(path, None)
            else:
                self._seen[path] = digest
        if step.replaced:
            backup = next(iter(step.replaced.values()))
            while backup.parent.name != BACKUP_FOLDER.name:
                backup = backup.parent
            shutil.rmtree(backup, ignore_errors=True)

    def _path(self, name: str) -> Path:
        path = (self.folder / name).resolve()
        if (
            not path.is_relative_to(self.folder.resolve())
            or path == self.folder.resolve()
        ):
            raise TransactionError("E_BAD_NAME", f"{name!r} is not inside the project")
        return path


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_record(path: Path) -> dict[str, str]:
    """The digests an earlier session recorded; none if there is no good record."""
    try:
        body: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(body, dict):
        return {}
    files = cast("dict[str, object]", body).get("files")
    if not isinstance(files, dict):
        return {}
    return {
        str(name): digest
        for name, digest in cast("dict[object, object]", files).items()
        if isinstance(digest, str)
    }


def _last_backup(folder: Path) -> int:
    """The highest backup number already in the folder, or 0."""
    try:
        numbers = [int(p.name) for p in folder.iterdir() if p.name.isdigit()]
    except OSError:
        return 0
    return max(numbers, default=0)


def file_change(path: Path, old: bytes | None, new: str) -> FileChange:
    """Measure what replacing a file's content changes.

    Args:
        path: The file.
        old: What it holds now, or ``None`` if it does not exist.
        new: What it would hold.

    Returns:
        The count of changed lines and its ratio to the old length.
    """
    if old is None:
        return FileChange(path, True, 0, len(new.splitlines()), None)
    before = old.decode("utf-8", errors="replace").splitlines()
    after = new.splitlines()
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    changed = sum(
        max(i2 - i1, j2 - j1)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag != "equal"
    )
    return FileChange(
        path, False, len(before), changed, changed / len(before) if before else None
    )


def _write_atomically(target: Path, content: bytes) -> None:
    """Write one file by way of a temporary file beside it."""
    handle, name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    temporary = Path(name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
