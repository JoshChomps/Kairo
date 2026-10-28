"""Freeze a built project's routing input as a benchmark case.

    uv run python evals/routing/freeze.py DESIGN.json PROJECT NAME [PLACES.json]

The board is laid out as make_board lays it out (placed, unless PLACES
gives the places; poured, stitched, fanned out) and the router's whole
input is saved as cases/NAME.json, to replay without KiCad.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from kairo.ir import Design
from kairo.kicad.cli import KicadCli
from kairo.pcb.autoroute import case_for, case_to_json
from kairo.pcb.board_writer import PlacedPart, Stackup, Track, Via, Zone
from kairo.pcb.layout import lay_out_board
from kairo.pcb.specctra import NetClass
from kairo.server import Settings, Workspace

HERE = Path(__file__).parent


def main() -> None:
    """Lay the board out and save what its router would be given."""
    design_file, project, name, *rest = sys.argv[1:]
    design = Design.model_validate(
        json.loads(Path(design_file).read_text(encoding="utf-8"))
    )
    workspace = Workspace(
        Settings((Path(project).parent,), Path(os.environ["LOCALAPPDATA"]) / "kairo")
    )
    index = workspace.library()
    cli, _ = KicadCli.find(None)
    places = (
        {k: tuple(v) for k, v in json.loads(Path(rest[0]).read_text()).items()}
        if rest
        else None
    )
    out = HERE / "cases" / f"{name}.json"

    def record(
        parts: Sequence[PlacedPart],
        outline: Sequence[tuple[float, float]],
        zones: Sequence[Zone],
        stackup: Stackup,
        fixed: Sequence[Via],
        fixed_tracks: Sequence[Track],
        classes: Sequence[NetClass],
    ) -> tuple[list[Track], list[Via]]:
        case = case_for(
            index, parts, outline, zones, stackup, fixed, fixed_tracks, classes
        )
        out.write_text(
            json.dumps(case_to_json(case), separators=(",", ":")), encoding="utf-8"
        )
        return [], []

    assert cli is not None
    lay_out_board(design, Path(project), index, cli, places=places, router=record)
    print(out, out.stat().st_size)


if __name__ == "__main__":
    main()
