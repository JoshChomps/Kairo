"""A second benchmark board: the sound reference designs on one dense 2-layer board.

    uv run python evals/routing/boards/mixed_signal.py FOLDER

Every case so far was the BMS (4 layers, power pours, a fine-pitch monitor).
This one differs on purpose: two layers, no pours, ground routed as tracks,
small parts packed close, so a router tuned to the BMS alone shows it here.
Each reference design keeps its parts and nets; references are renumbered
by 100 per design (R1 of the fourth design is R401), nets are prefixed by
the design's name except ground, which is shared. Designs written to fail
(a refusal, an overload) are left out. The design is written beside this
file and built into FOLDER with apply_design, as the MCP tool would.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from kairo.design_tools import DesignTools
from kairo.server import Settings, Workspace

HERE = Path(__file__).parent
DESIGNS = HERE.parent.parent / "reference_designs"
#: Designs that are wrong on purpose, for a refusal or a failing check.
LEFT_OUT = {"buck_refusal", "rc_lowpass_refusal", "ldo_overloaded"}
#: The compass cells the blocks are dealt into, in turn.
CELLS = [
    "north-west",
    "center-north",
    "north-east",
    "west",
    "center-middle",
    "east",
    "south-west",
    "center-south",
    "south-east",
]


def merged() -> dict[str, Any]:
    """The reference designs as one design with a 2-layer board."""
    nets: dict[str, Any] = {"GND": {"role": "ground"}}
    components: dict[str, Any] = {}
    connections: dict[str, list[str]] = {}
    no_connect: list[str] = []
    blocks: dict[str, list[str]] = {}
    sides: dict[str, str] = {}
    names = sorted(p.parent.name for p in DESIGNS.glob("*/design.json"))
    for n, name in enumerate(n for n in names if n not in LEFT_OUT):
        source = json.loads((DESIGNS / name / "design.json").read_text("utf-8"))
        offset = 100 * (n + 1)

        def ref(old: str, offset: int = offset) -> str:
            m = re.fullmatch(r"([A-Z]+)(\d+)", old)
            assert m, old
            return f"{m.group(1)}{int(m.group(2)) + offset}"

        def pin(text: str) -> str:
            part, _, rest = text.partition(".")
            return f"{ref(part)}.{rest}"

        def net(old: str, name: str = name) -> str:
            return "GND" if old == "GND" else f"{name}_{old}"

        for old, spec in source["nets"].items():
            if old != "GND":
                nets[net(old)] = spec
        for old, spec in source["components"].items():
            components[ref(old)] = spec
        for old, pins in source["connections"].items():
            connections.setdefault(net(old), []).extend(pin(p) for p in pins)
        no_connect += [pin(p) for p in source.get("no_connect", [])]
        for block, refs in source.get(
            "blocks", {"main": list(source["components"])}
        ).items():
            key = f"{name}_{block}"
            blocks[key] = [ref(r) for r in refs]
            sides[key] = CELLS[n % len(CELLS)]
    return {
        "design": "mixed_signal",
        "policy": {"margins": "stated"},
        "nets": nets,
        "components": components,
        "connections": connections,
        "no_connect": no_connect,
        "blocks": blocks,
        "placement": {"order": list(blocks)},
        "board": {
            "width": "70mm",
            "height": "55mm",
            "copper_layers": 2,
            "clearance": "0.4mm",
            "sides": sides,
            "net_classes": [
                {
                    "name": "Default",
                    "track": "0.25mm",
                    "clearance": "0.2mm",
                    "via_diameter": "0.6mm",
                    "via_drill": "0.3mm",
                }
            ],
            "rules": {
                "min_track": "0.15mm",
                "min_clearance": "0.15mm",
                "min_drill": "0.15mm",
                "min_via_diameter": "0.25mm",
                "min_annular": "0.05mm",
                "min_hole_clearance": "0.2mm",
                "min_hole_to_hole": "0.2mm",
                "copper_edge_clearance": "0.2mm",
            },
        },
    }


def main() -> None:
    """Write the merged design beside this file and build it into a folder."""
    design = merged()
    (HERE / "mixed_signal.json").write_text(
        json.dumps(design, indent=1) + "\n", encoding="utf-8"
    )
    settings = Settings(
        (Path(sys.argv[1]).parent,),
        Path(os.environ["LOCALAPPDATA"]) / "kairo",
        tier="write",
        calls_per_ten_minutes=0,
    )
    result = DesignTools(Workspace(settings)).apply_design(design, sys.argv[1])
    print(json.dumps(result, default=str)[:1500])
    print(len(design["components"]), "parts,", len(design["nets"]), "nets")


if __name__ == "__main__":
    main()
