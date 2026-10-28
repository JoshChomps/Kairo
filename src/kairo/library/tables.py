"""Read KiCad library tables: which libraries exist, and under what names.

A symbol or footprint is named ``<nickname>:<name>``, and the nickname comes
from a library table (``sym-lib-table`` or ``fp-lib-table``), not from the
library's file name. Tables were surveyed in a KiCad 10.0.6 install, its
demo projects and this machine's user configuration (2026-10-08):

- A table is ``(sym_lib_table ...)`` or ``(fp_lib_table ...)`` holding
  ``(lib (name ..) (type ..) (uri ..) (options ..) (descr ..))`` entries.
  Older tables have no ``(version ..)``.
- Type ``KiCad`` is a ``.kicad_sym`` file or a ``.pretty`` folder. Type
  ``Table`` points at another table, which is how KiCad 10's user table
  brings in the install's default libraries. Other types (``Legacy``) are
  reported, not read.
- A uri may use ``${NAME}`` or ``$(NAME)`` variables.

An entry Kairo cannot use is returned as a `TableProblem` saying why; it is
never dropped silently.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from kairo.kicad.sexpr import Atom, SexprError, SList, parse_file

Kind = Literal["symbol", "footprint"]

_HEADS: dict[Kind, str] = {"symbol": "sym_lib_table", "footprint": "fp_lib_table"}
_ENTRY_FIELDS = frozenset({"name", "type", "uri", "options", "descr"})
_VARIABLE = re.compile(r"\$\{([^}]*)\}|\$\(([^)]*)\)")


@dataclass(frozen=True, slots=True)
class Library:
    """One library a table names.

    Attributes:
        kind: ``symbol`` or ``footprint``.
        nickname: The name before the colon in ``<nickname>:<name>``.
        path: The ``.kicad_sym`` file or ``.pretty`` folder.
    """

    kind: Kind
    nickname: str
    path: Path


@dataclass(frozen=True, slots=True)
class TableProblem:
    """A table, or one entry in it, that Kairo could not use.

    Attributes:
        table: The table file.
        nickname: The entry's name, or empty when the whole table is at fault.
        reason: What is wrong.
    """

    table: Path
    nickname: str
    reason: str


def install_variables(
    share: Path, major: int, environ: Mapping[str, str]
) -> dict[str, str]:
    """The path variables KiCad defines for its own libraries.

    ``KICAD<major>_SYMBOL_DIR`` and ``KICAD<major>_FOOTPRINT_DIR`` point at
    the install's ``symbols`` and ``footprints`` folders unless the
    environment sets them.

    Args:
        share: The install's data folder, the one holding ``symbols``.
        major: KiCad's major version, for example 10.
        environ: The environment to take overrides from.

    Returns:
        Variable names mapped to paths.
    """
    variables = {
        f"KICAD{major}_SYMBOL_DIR": str(share / "symbols"),
        f"KICAD{major}_FOOTPRINT_DIR": str(share / "footprints"),
    }
    for name in variables:
        if environ.get(name):
            variables[name] = environ[name]
    return variables


def read_library_table(
    path: Path, kind: Kind, variables: Mapping[str, str]
) -> tuple[list[Library], list[TableProblem]]:
    """Read a library table, following any tables it points at.

    Args:
        path: The table file.
        kind: Which kind of table it must be.
        variables: Values for the variables uris may use. A project table
            needs ``KIPRJMOD`` (the project folder).

    Returns:
        The libraries in table order, each nickname once (the first entry
        wins), and every entry or table that could not be used.
    """
    libraries: dict[str, Library] = {}
    problems: list[TableProblem] = []
    _read_table(path, kind, variables, libraries, problems, set())
    return list(libraries.values()), problems


def _read_table(
    path: Path,
    kind: Kind,
    variables: Mapping[str, str],
    libraries: dict[str, Library],
    problems: list[TableProblem],
    open_tables: set[Path],
) -> None:
    if path in open_tables:
        problems.append(TableProblem(path, "", "the table includes itself"))
        return
    try:
        root = parse_file(path).root
    except (OSError, UnicodeDecodeError, SexprError) as exc:
        problems.append(TableProblem(path, "", f"could not be read: {exc}"))
        return
    if root.head != _HEADS[kind]:
        problems.append(
            TableProblem(path, "", f"is a {root.head!r}, not a {_HEADS[kind]!r}")
        )
        return
    for entry in root.lists("lib"):
        fields = {child.head: child for child in entry.lists()}
        nickname = _value(fields.get("name"))
        kind_of_entry = _value(fields.get("type"))
        uri = _value(fields.get("uri"))
        if nickname is None or kind_of_entry is None or uri is None:
            problems.append(
                TableProblem(path, nickname or "", "has no name, type or uri")
            )
            continue
        unknown = sorted(str(head) for head in fields.keys() - _ENTRY_FIELDS)
        if unknown:
            problems.append(
                TableProblem(
                    path, nickname, f"has ({unknown[0]} ...), which Kairo does not read"
                )
            )
            continue
        target, missing = _expand(uri, variables)
        if missing:
            problems.append(
                TableProblem(path, nickname, f"uses ${{{missing}}}, which is not set")
            )
            continue
        target_path = Path(target)
        if not target_path.is_absolute():
            problems.append(
                TableProblem(path, nickname, f"path {target} is not absolute")
            )
        elif kind_of_entry == "Table":
            _read_table(
                target_path, kind, variables, libraries, problems, open_tables | {path}
            )
        elif kind_of_entry != "KiCad":
            problems.append(
                TableProblem(
                    path, nickname, f"is of type {kind_of_entry!r}, which is not read"
                )
            )
        elif nickname in libraries:
            problems.append(
                TableProblem(
                    path,
                    nickname,
                    f"repeats a nickname already used for {libraries[nickname].path}",
                )
            )
        else:
            libraries[nickname] = Library(kind, nickname, target_path)


def _value(node: SList | None) -> str | None:
    if node is None or len(node.items) != 2:
        return None
    item = node.items[1]
    if not isinstance(item, Atom):
        return None
    try:
        return item.value
    except SexprError:
        return None


def _expand(uri: str, variables: Mapping[str, str]) -> tuple[str, str | None]:
    """Replace variables in a uri; also return the first one that is not set."""
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1) if match.group(1) is not None else match.group(2)
        if name not in variables:
            missing.append(name)
            return match.group(0)
        return variables[name]

    return _VARIABLE.sub(replace, uri), missing[0] if missing else None
