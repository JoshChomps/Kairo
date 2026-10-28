"""An SQLite index of symbols, pins, footprints and pads, kept in step with the files.

Parsing every official library takes minutes, so what the resolver needs is
read once and stored. `LibraryIndex.update` re-reads only files whose size
or modification time changed, drops files that are gone, and records every
file it could not read with the reason; such a file is listed by
`LibraryIndex.problems` and is never silently missing.

A derived symbol's pins are stored once, with the symbol whose body it uses.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kairo.kicad.formats import UnsupportedFormatError
from kairo.kicad.sexpr import SexprError, parse
from kairo.library.reader import (
    Footprint,
    LibraryFormatError,
    Pad,
    Pin,
    PinAlternate,
    Symbol,
    read_footprint,
    read_symbol_library,
)
from kairo.library.tables import Library

# Raised whenever the tables below change, so an index written by older code
# is rebuilt rather than misread.
_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE files (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    nickname TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL,
    size INTEGER NOT NULL,
    error TEXT
);
CREATE TABLE symbols (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    lib TEXT NOT NULL,
    name TEXT NOT NULL,
    extends TEXT,
    body TEXT NOT NULL,
    reference TEXT NOT NULL,
    value TEXT NOT NULL,
    footprint TEXT NOT NULL,
    datasheet TEXT NOT NULL,
    description TEXT NOT NULL,
    keywords TEXT NOT NULL,
    footprint_filters TEXT NOT NULL,
    is_power INTEGER NOT NULL,
    unit_count INTEGER NOT NULL,
    pin_count INTEGER NOT NULL,
    UNIQUE (lib, name)
);
CREATE INDEX symbols_file ON symbols(file_id);
CREATE TABLE pins (
    id INTEGER PRIMARY KEY,
    symbol_id INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    number TEXT NOT NULL,
    name TEXT NOT NULL,
    electrical_type TEXT NOT NULL,
    shape TEXT NOT NULL,
    unit INTEGER NOT NULL,
    body_style INTEGER NOT NULL,
    x REAL NOT NULL,
    y REAL NOT NULL,
    angle REAL NOT NULL,
    length REAL NOT NULL,
    hidden INTEGER NOT NULL,
    alternates TEXT NOT NULL
);
CREATE INDEX pins_symbol ON pins(symbol_id);
CREATE TABLE footprints (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    lib TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    tags TEXT NOT NULL,
    pad_count INTEGER NOT NULL,
    UNIQUE (lib, name)
);
CREATE INDEX footprints_file ON footprints(file_id);
CREATE TABLE pads (
    id INTEGER PRIMARY KEY,
    footprint_id INTEGER NOT NULL REFERENCES footprints(id) ON DELETE CASCADE,
    number TEXT NOT NULL,
    type TEXT NOT NULL,
    shape TEXT NOT NULL
);
CREATE INDEX pads_footprint ON pads(footprint_id);
"""

# Footprints are small, so they go to worker processes in batches; a symbol
# library can be megabytes and goes alone.
_FOOTPRINT_BATCH = 64
# Below this many files, starting worker processes costs more than it saves.
_MIN_FILES_FOR_WORKERS = 16


@dataclass(frozen=True, slots=True)
class FileProblem:
    """A library file that is not in the index, and why.

    Attributes:
        path: The file or folder.
        nickname: The library it belongs to.
        reason: What went wrong reading it.
    """

    path: Path
    nickname: str
    reason: str


@dataclass(frozen=True, slots=True)
class UpdateReport:
    """What one `LibraryIndex.update` did.

    Attributes:
        read: Files read because they were new or had changed.
        unchanged: Files left as they were.
        removed: Files dropped because they are gone or no longer listed.
        problems: Every file that is not in the index, old or new.
    """

    read: int
    unchanged: int
    removed: int
    problems: tuple[FileProblem, ...]


@dataclass(frozen=True, slots=True)
class SymbolSummary:
    """A symbol as a search result.

    Attributes:
        lib_id: ``<nickname>:<name>``.
        description: The symbol's description.
        keywords: Its keywords.
        pin_count: Pins across every unit and body style.
        unit_count: Units in the symbol.
    """

    lib_id: str
    description: str
    keywords: str
    pin_count: int
    unit_count: int


@dataclass(frozen=True, slots=True)
class FootprintSummary:
    """A footprint as a search result.

    Attributes:
        lib_id: ``<nickname>:<name>``.
        description: The footprint's description.
        tags: Its tags.
        pad_count: Pads, numbered or not.
    """

    lib_id: str
    description: str
    tags: str
    pad_count: int


_Task = tuple[str, str]
_Row = tuple[str | int | float | None, ...]
# What a worker process sends back for one file: each symbol with its pins,
# or the footprint with its pads, as plain rows ready to insert. Rows cross
# the process boundary several times faster than the reader's objects do.
_Rows = list[tuple[_Row, list[_Row]]]
_Read = tuple[str, _Rows, str | None]

_INSERTS = {
    "symbol": (
        "INSERT INTO symbols (file_id, lib, name, extends, body, reference, value, "
        "footprint, datasheet, description, keywords, footprint_filters, is_power, "
        "unit_count, pin_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        "INSERT INTO pins (symbol_id, number, name, electrical_type, shape, unit, "
        "body_style, x, y, angle, length, hidden, alternates) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    ),
    "footprint": (
        "INSERT INTO footprints (file_id, lib, name, description, tags, pad_count) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        "INSERT INTO pads (footprint_id, number, type, shape) VALUES (?, ?, ?, ?)",
    ),
}


class LibraryIndex:
    """The index, in one SQLite database.

    Args:
        database: The database file, created if absent. The default keeps
            the index in memory.
    """

    def __init__(self, database: Path | str = ":memory:") -> None:
        """Open the database, rebuilding it if older code wrote it."""
        # One connection may be used from several threads; callers that do so
        # take turns (the server holds a lock around every use).
        self._db = sqlite3.connect(str(database), check_same_thread=False)
        (version,) = self._db.execute("PRAGMA user_version").fetchone()
        if version != _SCHEMA_VERSION:
            for (name,) in self._db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall():
                self._db.execute(f'DROP TABLE "{name}"')
            self._db.executescript(_SCHEMA)
            self._db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            self._db.commit()
        # Deleting a file's row deletes its symbols, pins, footprints and pads.
        self._db.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        """Close the database."""
        self._db.close()

    def update(
        self, libraries: Sequence[Library], *, workers: int | None = None
    ) -> UpdateReport:
        """Bring the index in step with a set of libraries.

        Afterwards the index holds exactly these libraries. Files whose size
        and modification time are unchanged are not read again.

        Args:
            libraries: Every library to index, each nickname once per kind.
            workers: Worker processes for reading files; one fewer than the
                machine's processors if ``None``, and none at all for a
                handful of files.

        Returns:
            Counts of what was read, kept and removed, and every problem.

        Raises:
            ValueError: If two libraries of one kind share a nickname.
        """
        wanted: dict[str, tuple[str, str, int, int]] = {}
        missing: list[tuple[str, str, str, str]] = []
        nicknames: set[tuple[str, str]] = set()
        for library in libraries:
            if (library.kind, library.nickname) in nicknames:
                raise ValueError(
                    f"two {library.kind} libraries are called {library.nickname!r}"
                )
            nicknames.add((library.kind, library.nickname))
            try:
                for path, stat in _files_of(library):
                    wanted[path] = (
                        library.kind,
                        library.nickname,
                        stat.st_mtime_ns,
                        stat.st_size,
                    )
            except OSError as exc:
                missing.append(
                    (str(library.path), library.kind, library.nickname, str(exc))
                )

        stored = {
            path: (kind, nickname, mtime_ns, size)
            for path, kind, nickname, mtime_ns, size in self._db.execute(
                "SELECT path, kind, nickname, mtime_ns, size FROM files"
            )
        }
        # A library that could not be listed last time has a row of its own;
        # it is replaced, not counted as a removed file.
        still_missing = {path for path, _, _, _ in missing}
        gone = [path for path in stored if path not in wanted]
        todo = [path for path, facts in wanted.items() if stored.get(path) != facts]
        results = _read_files([(wanted[path][0], path) for path in todo], workers)

        with self._db:
            self._db.executemany(
                "DELETE FROM files WHERE path = ?", [(path,) for path in gone + todo]
            )
            for path, kind, nickname, reason in missing:
                self._db.execute(
                    "INSERT INTO files (path, kind, nickname, mtime_ns, size, error) "
                    "VALUES (?, ?, ?, 0, 0, ?)",
                    (path, kind, nickname, reason),
                )
            for path, rows, error in results:
                self._store(path, *wanted[path], rows, error)
        return UpdateReport(
            read=len(todo),
            unchanged=len(wanted) - len(todo),
            removed=sum(1 for path in gone if path not in still_missing),
            problems=tuple(self.problems()),
        )

    def problems(self) -> list[FileProblem]:
        """Every library file that could not be indexed, in path order.

        Returns:
            The files and the reasons.
        """
        return [
            FileProblem(Path(path), nickname, error)
            for path, nickname, error in self._db.execute(
                "SELECT path, nickname, error FROM files "
                "WHERE error IS NOT NULL ORDER BY path"
            )
        ]

    def symbol(self, lib_id: str) -> Symbol | None:
        """Look a symbol up by its exact ``<nickname>:<name>``.

        Args:
            lib_id: The library nickname and symbol name.

        Returns:
            The symbol with its pins in library order, or ``None`` if the
            index has no such symbol.
        """
        lib, _, name = lib_id.partition(":")
        row = self._db.execute(
            "SELECT name, extends, body, reference, value, footprint, datasheet, "
            "description, keywords, footprint_filters, is_power, unit_count "
            "FROM symbols WHERE lib = ? AND name = ?",
            (lib, name),
        ).fetchone()
        if row is None:
            return None
        pins = tuple(
            Pin(
                number=number,
                name=pin_name,
                electrical_type=electrical_type,
                shape=shape,
                unit=unit,
                body_style=body_style,
                x=x,
                y=y,
                angle=angle,
                length=length,
                hidden=bool(hidden),
                alternates=tuple(PinAlternate(*alt) for alt in json.loads(alternates)),
            )
            for (
                number,
                pin_name,
                electrical_type,
                shape,
                unit,
                body_style,
                x,
                y,
                angle,
                length,
                hidden,
                alternates,
            ) in self._db.execute(
                "SELECT number, name, electrical_type, shape, unit, body_style, "
                "x, y, angle, length, hidden, alternates FROM pins "
                "WHERE symbol_id = (SELECT id FROM symbols WHERE lib = ? AND name = ?) "
                "ORDER BY id",
                (lib, row[2]),
            )
        )
        return Symbol(
            name=row[0],
            extends=row[1],
            body=row[2],
            reference=row[3],
            value=row[4],
            footprint=row[5],
            datasheet=row[6],
            description=row[7],
            keywords=row[8],
            footprint_filters=row[9],
            is_power=bool(row[10]),
            unit_count=row[11],
            pins=pins,
        )

    def symbol_file(self, lib_id: str) -> Path | None:
        """The library file a symbol was read from.

        Args:
            lib_id: The library nickname and symbol name.

        Returns:
            The ``.kicad_sym`` file, or ``None`` if the index has no such
            symbol.
        """
        lib, _, name = lib_id.partition(":")
        row = self._db.execute(
            "SELECT files.path FROM symbols JOIN files ON files.id = symbols.file_id "
            "WHERE symbols.lib = ? AND symbols.name = ?",
            (lib, name),
        ).fetchone()
        return None if row is None else Path(row[0])

    def footprint_file(self, lib_id: str) -> Path | None:
        """The ``.kicad_mod`` file a footprint was read from.

        Args:
            lib_id: The library nickname and footprint name.

        Returns:
            The file, or ``None`` if the index has no such footprint.
        """
        lib, _, name = lib_id.partition(":")
        row = self._db.execute(
            "SELECT files.path FROM footprints JOIN files "
            "ON files.id = footprints.file_id "
            "WHERE footprints.lib = ? AND footprints.name = ?",
            (lib, name),
        ).fetchone()
        return None if row is None else Path(row[0])

    def footprint_exists(self, lib_id: str) -> bool:
        """Whether the index holds a footprint.

        Args:
            lib_id: The library nickname and footprint name.

        Returns:
            True if it does.
        """
        lib, _, name = lib_id.partition(":")
        row = self._db.execute(
            "SELECT 1 FROM footprints WHERE lib = ? AND name = ?", (lib, name)
        ).fetchone()
        return row is not None

    def symbol_names(self, library: str) -> list[str]:
        """Every symbol name in one library, for suggesting a near miss.

        Args:
            library: The library's nickname.

        Returns:
            The names in order, or nothing if no such library is indexed.
        """
        return [
            name
            for (name,) in self._db.execute(
                "SELECT name FROM symbols WHERE lib = ? ORDER BY name", (library,)
            )
        ]

    def symbol_libraries(self) -> list[str]:
        """The nickname of every indexed symbol library that holds a symbol.

        Returns:
            The nicknames in order.
        """
        return [
            lib
            for (lib,) in self._db.execute(
                "SELECT DISTINCT lib FROM symbols ORDER BY lib"
            )
        ]

    def footprint(self, lib_id: str) -> Footprint | None:
        """Look a footprint up by its exact ``<nickname>:<name>``.

        Args:
            lib_id: The library nickname and footprint name.

        Returns:
            The footprint with its pads in file order, or ``None`` if the
            index has no such footprint.
        """
        lib, _, name = lib_id.partition(":")
        row = self._db.execute(
            "SELECT id, name, description, tags FROM footprints "
            "WHERE lib = ? AND name = ?",
            (lib, name),
        ).fetchone()
        if row is None:
            return None
        pads = tuple(
            Pad(number, kind, shape)
            for number, kind, shape in self._db.execute(
                "SELECT number, type, shape FROM pads WHERE footprint_id = ? "
                "ORDER BY id",
                (row[0],),
            )
        )
        return Footprint(name=row[1], description=row[2], tags=row[3], pads=pads)

    def find_symbols(self, query: str, *, limit: int = 20) -> list[SymbolSummary]:
        """Search symbols by words in their name, library, description or keywords.

        Every word must appear somewhere, in any letter case; a word written
        ``<nickname>:<name>`` is its two halves. A symbol whose
        ``<nickname>:<name>`` or name is the query comes first, then names
        that start with it, then shorter names.

        Args:
            query: One or more words.
            limit: The most results to return.

        Returns:
            The matches, best first.
        """
        rows = self._search(
            "symbols",
            ("name", "lib", "description", "keywords"),
            "lib, name, description, keywords, pin_count, unit_count",
            query,
            limit,
        )
        return [
            SymbolSummary(f"{lib}:{name}", description, keywords, pins, units)
            for lib, name, description, keywords, pins, units in rows
        ]

    def find_footprints(self, query: str, *, limit: int = 20) -> list[FootprintSummary]:
        """Search footprints by words in their name, library, description or tags.

        Matching and order are as for `find_symbols`.

        Args:
            query: One or more words.
            limit: The most results to return.

        Returns:
            The matches, best first.
        """
        rows = self._search(
            "footprints",
            ("name", "lib", "description", "tags"),
            "lib, name, description, tags, pad_count",
            query,
            limit,
        )
        return [
            FootprintSummary(f"{lib}:{name}", description, tags, pads)
            for lib, name, description, tags, pads in rows
        ]

    def _search(
        self,
        table: str,
        searched: tuple[str, ...],
        columns: str,
        query: str,
        limit: int,
    ) -> list[Any]:
        # The library and the name are kept apart, so a lib_id given back as
        # a query is searched as its two halves.
        words = [half for word in query.split() for half in word.split(":") if half]
        if not words:
            return []
        one_word = "(" + " OR ".join(f"{c} LIKE ? ESCAPE '!'" for c in searched) + ")"
        where = " AND ".join([one_word] * len(words))
        patterns = [f"%{_like(word)}%" for word in words for _ in searched]
        whole = " ".join(words)
        return self._db.execute(
            f"SELECT {columns} FROM {table} WHERE {where} "
            "ORDER BY lib || ':' || name = ? COLLATE NOCASE DESC, "
            "name = ? COLLATE NOCASE DESC, name LIKE ? ESCAPE '!' DESC, "
            "length(name), lib, name LIMIT ?",
            (*patterns, query.strip(), whole, f"{_like(whole)}%", limit),
        ).fetchall()

    def _store(
        self,
        path: str,
        kind: str,
        nickname: str,
        mtime_ns: int,
        size: int,
        rows: _Rows,
        error: str | None,
    ) -> None:
        file_id = self._db.execute(
            "INSERT INTO files (path, kind, nickname, mtime_ns, size, error) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (path, kind, nickname, mtime_ns, size, error),
        ).lastrowid
        insert_parent, insert_child = _INSERTS[kind]
        for parent, children in rows:
            parent_id = self._db.execute(
                insert_parent, (file_id, nickname, *parent)
            ).lastrowid
            self._db.executemany(insert_child, [(parent_id, *c) for c in children])


def _symbol_rows(symbols: list[Symbol]) -> _Rows:
    rows: _Rows = []
    for s in symbols:
        parent: _Row = (
            s.name,
            s.extends,
            s.body,
            s.reference,
            s.value,
            s.footprint,
            s.datasheet,
            s.description,
            s.keywords,
            s.footprint_filters,
            s.is_power,
            s.unit_count,
            len(s.pins),
        )
        # A derived symbol's pins are stored once, with its body's symbol.
        pins: list[_Row] = (
            []
            if s.body != s.name
            else [
                (
                    p.number,
                    p.name,
                    p.electrical_type,
                    p.shape,
                    p.unit,
                    p.body_style,
                    p.x,
                    p.y,
                    p.angle,
                    p.length,
                    p.hidden,
                    json.dumps(
                        [(a.name, a.electrical_type, a.shape) for a in p.alternates]
                    ),
                )
                for p in s.pins
            ]
        )
        rows.append((parent, pins))
    return rows


def _footprint_rows(footprint: Footprint) -> _Rows:
    parent: _Row = (
        footprint.name,
        footprint.description,
        footprint.tags,
        len(footprint.pads),
    )
    return [(parent, [(p.number, p.type, p.shape) for p in footprint.pads])]


def _files_of(library: Library) -> Iterator[tuple[str, os.stat_result]]:
    """The files that make up a library, with their sizes and times."""
    if library.kind == "symbol":
        yield str(library.path), library.path.stat()
        return
    with os.scandir(library.path) as entries:
        for entry in sorted(entries, key=lambda e: e.name):
            if entry.name.endswith(".kicad_mod") and entry.is_file():
                yield entry.path, entry.stat()


def _read_files(tasks: list[_Task], workers: int | None) -> list[_Read]:
    if workers is None:
        workers = max((os.cpu_count() or 1) - 1, 1)
    if workers == 1 or len(tasks) < _MIN_FILES_FOR_WORKERS:
        return _read_batch(tasks)
    batches: list[list[_Task]] = [[t] for t in tasks if t[0] == "symbol"]
    footprints = [t for t in tasks if t[0] == "footprint"]
    batches += [
        footprints[i : i + _FOOTPRINT_BATCH]
        for i in range(0, len(footprints), _FOOTPRINT_BATCH)
    ]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return [read for batch in pool.map(_read_batch, batches) for read in batch]


def _read_batch(tasks: list[_Task]) -> list[_Read]:
    return [_read_file(kind, path) for kind, path in tasks]


def _read_file(kind: str, path: str) -> _Read:
    try:
        document = parse(Path(path).read_bytes().decode("utf-8"))
        if kind == "symbol":
            return path, _symbol_rows(read_symbol_library(document)), None
        footprint = read_footprint(document)
        # KiCad names a footprint after its file; a file whose contents
        # disagree would be indexed under a name KiCad may not use.
        stem = Path(path).stem
        if footprint.name != stem:
            raise LibraryFormatError(
                f"footprint is named {footprint.name!r} inside a file named {stem!r}"
            )
        return path, _footprint_rows(footprint), None
    except (
        OSError,
        UnicodeDecodeError,
        SexprError,
        UnsupportedFormatError,
        LibraryFormatError,
    ) as exc:
        return path, [], f"{type(exc).__name__}: {exc}"


def _like(text: str) -> str:
    """Escape LIKE's wildcards so the text matches itself."""
    return text.replace("!", "!!").replace("%", "!%").replace("_", "!_")
