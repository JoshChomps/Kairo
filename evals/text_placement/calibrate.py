"""Measure the width of KiCad's schematic text, one character at a time.

A schematic is written that holds, for every printable ASCII character,
that character repeated 4 and 8 times as a text item. ``kicad-cli sch
export svg`` then draws it, and for each text KiCad's drawing states the
text's length. The difference between the two lengths, over four, is the
character's advance; what is left over is a constant KiCad adds to every
text.

Run from the repository root with KiCad installed::

    uv run python evals/text_placement/calibrate.py

It prints the table that ``kairo.text_placement.kicad_text`` holds, as
advances in units of the text size, and the largest disagreement between
the two ways of working out each constant.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import uuid
from pathlib import Path

from kairo.kicad.cli import KicadCli, RenderStatus
from kairo.kicad.emit import lay_out, lst, sym
from kairo.kicad.sexpr import to_text

SIZE = 1.27
CHARACTERS = [chr(code) for code in range(0x21, 0x7F)]
_TEXT = re.compile(r'textLength="([\d.]+)"[^>]*>([^<]*)</text>')
_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&apos;": "'"}


def sheet(texts: list[str], size: float = SIZE) -> str:
    """A schematic holding each text as a free text item.

    Args:
        texts: The texts, one per item.
        size: The text size, in mm.

    Returns:
        The schematic's content.
    """
    items = [
        lst(
            "text",
            text,
            lst("exclude_from_sim", sym("no")),
            lst("at", 20.32 + 60.96 * (n % 4), 20.32 + 2.54 * (n // 4), 0),
            lst(
                "effects",
                lst("font", lst("size", size, size)),
                lst("justify", sym("left"), sym("bottom")),
            ),
            lst("uuid", str(uuid.uuid5(uuid.NAMESPACE_OID, f"calibrate/{n}"))),
        )
        for n, text in enumerate(texts)
    ]
    root = lst(
        "kicad_sch",
        lst("version", 20260306),
        lst("generator", "kairo"),
        lst("generator_version", "calibrate"),
        lst("uuid", str(uuid.uuid5(uuid.NAMESPACE_OID, "calibrate"))),
        lst("paper", "A2"),
        lst("lib_symbols"),
        *items,
        lst("sheet_instances", lst("path", "/", lst("page", "1"))),
        lst("embedded_fonts", sym("no")),
    )
    return to_text(lay_out(root)) + "\n"


def measured_lengths(
    cli: KicadCli, texts: list[str], size: float = SIZE
) -> dict[str, float]:
    """Draw texts with KiCad and read back the length it gives each.

    Args:
        cli: The kicad-cli to draw with.
        texts: The texts to measure.
        size: The text size, in mm.

    Returns:
        Each text's length in mm.

    Raises:
        RuntimeError: If KiCad cannot draw the sheet, or a text is not in
            its drawing.
    """
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "calibrate.kicad_sch"
        path.write_text(sheet(texts, size), encoding="utf-8", newline="")
        rendered = cli.export_svg(path, Path(folder) / "svg")
        if rendered.status is not RenderStatus.OK:
            raise RuntimeError(f"KiCad could not draw the sheet: {rendered.reason}")
        drawing = rendered.files[0].read_text(encoding="utf-8")
    lengths: dict[str, float] = {}
    for length, text in _TEXT.findall(drawing):
        for entity, character in _ENTITIES.items():
            text = text.replace(entity, character)
        lengths[text] = float(length)
    missing = [text for text in texts if text not in lengths]
    if missing:
        raise RuntimeError(f"KiCad's drawing lacks {len(missing)} texts: {missing[:5]}")
    return lengths


def calibrate(cli: KicadCli) -> dict[str, object]:
    """Work out each character's advance and the constant per text.

    Args:
        cli: The kicad-cli to measure with.

    Returns:
        ``advance`` (per character, in units of the text size),
        ``constant`` (per text, same units), ``space`` (the advance of a
        space), and ``spread`` (the largest disagreement found between
        characters about the constant).
    """
    texts = [c * 4 for c in CHARACTERS] + [c * 8 for c in CHARACTERS]
    # A text of spaces alone draws nothing, so a space is measured between
    # two letters.
    texts += ["xx", "x    x"]
    lengths = measured_lengths(cli, texts)
    advance = {c: (lengths[c * 8] - lengths[c * 4]) / 4 / SIZE for c in CHARACTERS}
    constants = [lengths[c * 4] / SIZE - 4 * advance[c] for c in CHARACTERS]
    constant = sorted(constants)[len(constants) // 2]
    space = (lengths["x    x"] - lengths["xx"]) / 4 / SIZE
    return {
        "advance": {c: round(a, 5) for c, a in advance.items()},
        "constant": round(constant, 5),
        "space": round(space, 5),
        "spread": round(max(constants) - min(constants), 5),
    }


def main() -> None:
    """Measure with the installed KiCad and print the table as JSON."""
    cli, discovery = KicadCli.find()
    if cli is None:
        sys.exit(f"kicad-cli not found (searched: {'; '.join(discovery.searched)})")
    version = cli.version()
    table = calibrate(cli)
    table["kicad_version"] = version.raw if version else None
    print(json.dumps(table, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
