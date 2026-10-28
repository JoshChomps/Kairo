"""Which KiCad file-format versions Kairo reads, and the check that refuses the rest.

Every KiCad S-expression file names its format in a ``(version N)`` token,
where N is a date. Kairo accepts exactly the versions it has seen written by
released KiCad 9 and 10 builds, and refuses everything else with an error
that says what it found, rather than guessing how an unknown format
behaves. Files from development builds (``generator_version`` 9.99) are
refused too.

Evidence (survey of a KiCad 10.0.6 install, 2026-10-06): each version below
appears in files whose ``generator_version`` is the release shown, except
schematic version 20260306, which ``kicad-cli sch upgrade --force`` writes
on 10.0.6. Adding a version needs a file saved by that KiCad as a fixture.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from kairo.kicad.sexpr import Atom, Document, SexprError


@dataclass(frozen=True, slots=True)
class FileKind:
    """One kind of KiCad file and the format versions Kairo reads for it.

    Attributes:
        head: The head of the file's top-level list, for example ``kicad_sch``.
        name: A plain name for messages.
        versions: Each accepted version, mapped to the KiCad release that
            writes it.
    """

    head: str
    name: str
    versions: Mapping[int, str]


SCHEMATIC = FileKind(
    "kicad_sch",
    "schematic",
    {20241209: "9.0", 20250114: "9.0", 20260101: "10.0", 20260306: "10.0.6"},
)
SYMBOL_LIBRARY = FileKind(
    "kicad_symbol_lib", "symbol library", {20241209: "9.0", 20251024: "10.0"}
)
FOOTPRINT = FileKind(
    "footprint", "footprint", {20241228: "9.0", 20241229: "9.0", 20260206: "10.0"}
)
BOARD = FileKind("kicad_pcb", "board", {20241229: "9.0", 20260206: "10.0"})


class UnsupportedFormatError(ValueError):
    """A file is not the expected kind, or not in a format version Kairo reads."""


def check_format(document: Document, kind: FileKind) -> int:
    """Check a parsed file is of the given kind and in a format Kairo reads.

    Args:
        document: The parsed file.
        kind: What the file should be.

    Returns:
        The file's format version.

    Raises:
        UnsupportedFormatError: If the top-level list has the wrong head, has
            no ``(version N)`` with a whole number, or names a version not in
            ``kind.versions``.
    """
    try:
        root = document.root
    except SexprError as exc:
        raise UnsupportedFormatError(f"not a KiCad {kind.name}: {exc}") from exc
    if root.head != kind.head:
        raise UnsupportedFormatError(
            f"not a KiCad {kind.name}: top-level list is {root.head!r}, "
            f"expected {kind.head!r}"
        )
    found = root.first("version")
    raw = found.items[1] if found is not None and len(found.items) == 2 else None
    if not isinstance(raw, Atom) or raw.quoted or not raw.raw.isdigit():
        raise UnsupportedFormatError(f"{kind.name} has no (version N) token")
    version = int(raw.raw)
    if version not in kind.versions:
        generator = root.first("generator_version")
        written_by = generator.values()[0] if generator and generator.values() else None
        by = f" (written by KiCad {written_by})" if written_by else ""
        known = ", ".join(f"{v} (KiCad {r})" for v, r in sorted(kind.versions.items()))
        raise UnsupportedFormatError(
            f"{kind.name} format {version}{by} is not one Kairo reads; it reads {known}"
        )
    return version
