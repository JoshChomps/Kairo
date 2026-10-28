"""The shape of a design, for a model to read before it writes one.

A model reads this at the start of every design, and a chat client sends
it again with every later turn, so it is kept as small as it can be
without losing what a field means: the patch operations are given only
when asked for, since only an edit needs them; the titles pydantic adds,
which repeat each field's name, are left out; and the indentation of the
descriptions is closed up.
"""

from __future__ import annotations

import re
from typing import Any, Literal, cast

from pydantic import TypeAdapter

from kairo.ir.models import PARAMETERS, Design
from kairo.ir.patch import PatchOp

Part = Literal["design", "patch", "board", "all"]

_SPACE = re.compile(r"\s+")

_RULES = [
    "Write every quantity as text with its unit: 3.3V, 300mA, 22uF, "
    "1%, 40degC, 90degC/W. A number with no unit is refused.",
    "Connect pins by name as REF.PIN, using the names get_symbol_pins "
    "gives. A pin number is accepted only for a symbol whose pins "
    "have no names, such as a resistor.",
    "Never give a value you were not given: leave it out. For a missing "
    "tolerance Kairo uses its own wide default and the result says so; "
    "any other missing value leaves the result unverified, with what to "
    "supply. policy.margins 'stated' turns the defaults off.",
    "No coordinates, pin numbers for named pins, or identifiers: Kairo owns those.",
]


#: A whole design at its smallest, to show the shape at a glance: nets,
#: components and connections are objects keyed by name, never lists.
EXAMPLE: dict[str, Any] = {
    "design": "example",
    "nets": {"VIN": {"role": "power", "voltage": "5V"}, "GND": {"role": "ground"}},
    "components": {
        "R1": {
            "lib_id": "Device:R",
            "value": "10k",
            "footprint": "Resistor_SMD:R_0603_1608Metric",
        }
    },
    "connections": {"VIN": ["R1.1"], "GND": ["R1.2"]},
}


def design_schema(part: Part = "all") -> dict[str, Any]:
    """Describe exactly what a design may hold.

    Args:
        part: ``design`` for what writing a design needs: its schema, the
            parameter names with their units, and the rules a schema
            cannot state. ``patch`` for the operations that change a
            built design. ``board`` for the printed circuit board's
            constraints, which only make_board reads, so a design's schema
            leaves them out. ``all`` for all three.

    Returns:
        The parts asked for, each JSON schema with its titles left out
        and its descriptions' white space closed up.

    Raises:
        ValueError: If ``part`` is not one of the four.
    """
    if part not in ("design", "patch", "board", "all"):
        raise ValueError(f"part is design, patch, board or all; got {part!r}")
    out: dict[str, Any] = {}
    whole = _slim(Design.model_json_schema())
    if part in ("design", "all"):
        out["example"] = EXAMPLE
        out["design"] = whole if part == "all" else _without_board(whole)
    if part == "board":
        out["board"] = _board_only(whole)
        out["more"] = (
            "board goes in the design beside nets and components; make_board "
            "reads it once apply_design has built the schematic"
        )
    if part in ("patch", "all"):
        out["patch_operations"] = _slim(TypeAdapter(list[PatchOp]).json_schema())
    if part in ("design", "all"):
        out["parameters"] = {
            name: unit if unit is not None else "text"
            for name, unit in sorted(PARAMETERS.items())
        }
        out["rules"] = list(_RULES)
    if part == "patch":
        out["more"] = (
            "These are the operations edit_design takes, not the shape of a "
            "design: design_schema with part 'design' gives that"
        )
    if part == "design":
        out["more"] = (
            "design_schema with part 'patch' gives the operations edit_design "
            "takes, and part 'board' the board make_board lays out"
        )
    return out


#: The definitions only a board uses.
_BOARD_DEFS = ("Board", "Pour", "NetClass", "FabRules", "RouteGuide")


def _without_board(schema: dict[str, Any]) -> dict[str, Any]:
    """A design's schema with the board's shape left for part 'board'."""
    out = dict(schema)
    out["$defs"] = {k: v for k, v in schema["$defs"].items() if k not in _BOARD_DEFS}
    out["properties"] = {
        **schema["properties"],
        "board": {
            "description": (
                "The printed circuit board's constraints, for make_board: "
                "design_schema with part 'board' gives its shape"
            )
        },
    }
    return out


def _board_only(schema: dict[str, Any]) -> dict[str, Any]:
    """The board's schema, with every definition it refers to."""
    defs = schema["$defs"]
    keep: set[str] = set()
    todo = ["Board"]
    while todo:
        name = todo.pop()
        if name not in keep:
            keep.add(name)
            todo += _REF.findall(str(defs[name]))
    return {**defs["Board"], "$defs": {k: defs[k] for k in sorted(keep - {"Board"})}}


#: A reference to a definition in a JSON schema.
_REF = re.compile(r"#/[$]defs/(\w+)")


def _slim(node: Any) -> Any:
    """A JSON schema with nothing in it that says a thing twice.

    A ``title`` that is text is pydantic's restatement of a name; one
    that is a mapping is a field that happens to be called title, and
    is kept. An optional field's ``null`` alternative and its ``default:
    null`` are left out too: a field missing from ``required`` says it.
    """
    if isinstance(node, dict):
        slim: dict[str, Any] = {}
        for key, value in node.items():  # pyright: ignore[reportUnknownVariableType]
            if key == "title" and isinstance(value, str):
                continue
            if key == "description" and isinstance(value, str):
                slim[key] = _SPACE.sub(" ", value).strip()
            else:
                slim[str(key)] = _slim(value)  # pyright: ignore[reportUnknownArgumentType]
        options = slim.get("anyOf")
        if isinstance(options, list) and "default" in slim and slim["default"] is None:
            kept = [o for o in cast("list[Any]", options) if o != {"type": "null"}]
            del slim["default"], slim["anyOf"]
            if len(kept) == 1 and isinstance(kept[0], dict):
                slim = {**cast("dict[str, Any]", kept[0]), **slim}
            else:
                slim["anyOf"] = kept
        return slim
    if isinstance(node, list):
        return [_slim(item) for item in node]  # pyright: ignore[reportUnknownVariableType]
    return node
