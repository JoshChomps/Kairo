"""Change one thing in a schematic a person drew, and nothing else.

The file is read into the lossless tree of `kairo.kicad.sexpr`, one atom
is replaced, and the tree is written back, so every other byte of the
file is as its author left it. What is offered is deliberately narrow: a
part's value. Anything that would move a wire or a symbol needs the
import to a design that the plan describes, which is not built.

How a part's reference is stored was read from real KiCad 8, 9 and 10
files (``tests/fixtures/real``): each placed symbol has an ``instances``
list with one ``path`` per place the sheet is used, each carrying the
``reference`` it has there; the ``Reference`` property holds the same
for a sheet used once.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from kairo.kicad.sexpr import Atom, Document, SList, parse


class EditError(ValueError):
    """An edit cannot be made: its code says why.

    Attributes:
        code: ``E_REF_NOT_FOUND``, ``E_SHARED_SHEET`` or ``E_NOT_A_SCHEMATIC``.
    """

    def __init__(self, code: str, message: str) -> None:
        """Keep the code beside the message."""
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class ValueEdit:
    """What a change of value did.

    Attributes:
        files: Each changed file's name, relative to the project folder,
            and its new text.
        old: The value before, as each changed symbol had it.
        symbols: How many symbols were changed; more than one for a part
            drawn as several units.
    """

    files: dict[str, str]
    old: tuple[str, ...]
    symbols: int


def sheet_files(root: Path) -> list[Path]:
    """Every sheet file a schematic uses, the root first.

    Args:
        root: The root ``.kicad_sch`` file.

    Returns:
        The files, each once, in the order they are first referred to.

    Raises:
        EditError: ``E_NOT_A_SCHEMATIC`` if a file does not parse as one.
    """
    found: list[Path] = []
    waiting = [root]
    while waiting:
        path = waiting.pop(0)
        if path in found or not path.is_file():
            continue
        found.append(path)
        document = read_sheet(path)
        for sheet in document.root.lists("sheet"):
            for prop in sheet.lists("property"):
                values = prop.values()
                if len(values) >= 2 and values[0] in ("Sheetfile", "Sheet file"):
                    waiting.append((path.parent / values[1]).resolve())
    return found


def read_sheet(path: Path) -> Document:
    """Parse a schematic file, keeping its bytes' line endings.

    Args:
        path: The ``.kicad_sch`` file.

    Returns:
        The lossless tree.

    Raises:
        EditError: ``E_NOT_A_SCHEMATIC`` if it does not parse as one.
    """
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            document = parse(handle.read())
    except (OSError, ValueError) as exc:
        raise EditError("E_NOT_A_SCHEMATIC", f"{path.name}: {exc}") from exc
    if document.root.head != "kicad_sch":
        raise EditError("E_NOT_A_SCHEMATIC", f"{path.name} is not a schematic")
    return document


def references(symbol: SList, project: str | None = None) -> list[str]:
    """Every reference a placed symbol has, one per place its sheet is used.

    A symbol copied from another project keeps that project's references
    beside its own (seen on the pedalboard fixture, where a sheet's parts
    carry two ``project`` entries). KiCad uses the entry named after the
    open project, so that is the one read when the project is given and
    the symbol has an entry for it.

    Args:
        symbol: A placed symbol.
        project: The project's name, which is its file's name without the
            ending; ``None`` to read every entry.

    Returns:
        The references, from its ``instances`` list, or from its
        ``Reference`` property when it has none.
    """
    refs: list[str] = []
    instances = symbol.first("instances")
    if instances is not None:
        entries = instances.lists("project")
        own = [e for e in entries if e.values()[:1] == [project]]
        for entry in own or entries:
            for path in entry.lists("path"):
                reference = path.first("reference")
                if reference is not None and reference.values():
                    refs.append(reference.values()[0])
    if not refs:
        for prop in symbol.lists("property"):
            values = prop.values()
            if len(values) >= 2 and values[0] == "Reference":
                refs.append(values[1])
    return refs


def set_value(root: Path, ref: str, value: str) -> ValueEdit:
    """Work out the files that give a part a new value.

    Nothing is written: the caller writes the returned text through
    `kairo.kicad.transaction`.

    Args:
        root: The root ``.kicad_sch`` file.
        ref: The part's reference, such as ``R34``.
        value: The new value.

    Returns:
        The changed files and what the value was.

    Raises:
        EditError: ``E_REF_NOT_FOUND`` if no symbol has the reference;
            ``E_SHARED_SHEET`` if the symbol is on a sheet used more than
            once, where one value serves several parts.
    """
    folder = root.parent
    files: dict[str, str] = {}
    old: list[str] = []
    count = 0
    for path in sheet_files(root):
        document = read_sheet(path)
        changed = False
        for symbol in document.root.lists("symbol"):
            if symbol.first("lib_id") is None:
                continue
            found = references(symbol, root.stem)
            if ref not in found:
                continue
            if len(set(found)) > 1:
                raise EditError(
                    "E_SHARED_SHEET",
                    f"{ref} is on a sheet used {len(found)} times, where it "
                    f"is also {', '.join(sorted(set(found) - {ref}))}; one "
                    "value serves them all, so it is not changed for one",
                )
            for prop in symbol.lists("property"):
                atoms = [item for item in prop.items if isinstance(item, Atom)]
                if len(atoms) >= 3 and atoms[1].value == "Value":
                    old.append(atoms[2].value)
                    position = prop.items.index(atoms[2])
                    prop.items[position] = Atom.string(value, atoms[2].before)
                    changed = True
                    count += 1
        if changed:
            files[path.relative_to(folder).as_posix()] = document.to_text()
    if count == 0:
        raise EditError(
            "E_REF_NOT_FOUND", f"no symbol in {root.name} or its sheets is {ref}"
        )
    return ValueEdit(files, tuple(old), count)
