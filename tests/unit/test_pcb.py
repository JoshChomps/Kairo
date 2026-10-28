"""The board modules: pours, banks, placement rules and the router's session."""

# The banks and outlines are tested through their own functions.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, cast

import pytest

from kairo.pcb.place import (
    Item,
    Outline,
    PlacementError,
    _banked,
    floorplan,
    place,
    region,
    turned,
)
from kairo.pcb.specctra import read_ses
from kairo.pcb.zones import _outline, shared

SQUARE = Outline((-1.0, -1.0, 1.0, 1.0), (("1", -0.5, 0.0), ("2", 0.5, 0.0)))


def test_a_staircase_edge_is_one_diagonal() -> None:
    triangle = {(c, r) for r in range(6) for c in range(r + 1)}
    assert _outline(triangle) == [(0, 0), (6, 6), (0, 6)]
    # A square has no staircase to cut.
    square = {(c, r) for r in range(4) for c in range(4)}
    assert sorted(_outline(square)) == [(0, 0), (0, 4), (4, 0), (4, 4)]


def test_a_shared_layer_goes_to_the_nearest_pads_net() -> None:
    zones = shared(
        {"A": [(10.0, 10.0)], "B": [(30.0, 10.0)]}, "F.Cu", (0.0, 0.0, 40.0, 20.0)
    )
    by_net = {z.net: z for z in zones}
    assert set(by_net) == {"A", "B"}
    # Each pour stops halfway, at x = 20.
    assert max(x for x, _ in by_net["A"].outline) <= 20.5
    assert min(x for x, _ in by_net["B"].outline) >= 19.5


def test_a_compass_side_is_its_third_of_the_board() -> None:
    assert region("east", 150, 90) == (100.0, 0.0, 150.0, 90.0)
    assert region("west-south", 150, 90) == (0.0, 60.0, 50.0, 90.0)
    with pytest.raises(PlacementError, match="'up' is not one of"):
        region("up", 150, 90)


def test_parts_in_parallel_are_one_row() -> None:
    fets = [
        Item(f"Q{n}", SQUARE, "fets", "east", None, {"1": "SRC", "2": "DRN"})
        for n in range(1, 5)
    ]
    gated = [
        Item(
            f"Q{n}",
            Outline(
                (-1.0, -1.0, 1.0, 1.0),
                (("1", 0.0, -0.5), ("2", 0.0, 0.5), ("3", 0.5, 0.0)),
            ),
            "fets",
            "east",
            None,
            {"1": "SRC", "2": "DRN", "3": f"G{n}"},
        )
        for n in range(5, 7)
    ]
    other = Item("R1", SQUARE, "fets", "east", None, {"1": "SRC", "2": "X"})
    units, rows = _banked([*fets, *gated, other], clearance=0.5)
    names = sorted(u.ref for u in units)
    assert names == ["R1", "bank:Q1,Q2,Q3,Q4", "bank:Q5,Q6"]
    # SRC and DRN sit left and right on Q1 to Q4, so the row runs down;
    # on Q5 and Q6 they sit above and below, so it runs across.
    assert [dx for _, dx, _ in rows["bank:Q1,Q2,Q3,Q4"]] == [0.0] * 4
    assert [dy for _, _, dy in rows["bank:Q5,Q6"]] == [0.0, 0.0]


def test_placement_keeps_courtyards_apart_and_parts_on_their_edge() -> None:
    items = [
        Item("J1", SQUARE, "conn", "west", "west", {"1": "A", "2": "B"}),
        *[
            Item(f"R{n}", SQUARE, "body", "center", None, {"1": "A", "2": "B"})
            for n in range(1, 9)
        ],
    ]
    places = place(items, 30, 20, clearance=0.5, effort=0.2)
    assert places["J1"][0] == pytest.approx(1.25)
    boxes = {
        ref: (x - 1.25, y - 1.25, x + 1.25, y + 1.25)
        for ref, (x, y, _) in places.items()
    }
    for a, (l1, t1, r1, b1) in boxes.items():
        assert l1 >= 0 and t1 >= 0 and r1 <= 30 and b1 <= 20
        for b, (l2, t2, r2, b2) in boxes.items():
            if a < b:
                assert not (l1 < r2 and l2 < r1 and t1 < b2 and t2 < b1), (a, b)
    # The same design is placed the same way every time.
    assert place(items, 30, 20, clearance=0.5, effort=0.2) == places


def test_a_session_gives_tracks_and_vias_in_board_millimetres() -> None:
    session = """(session "b.ses"
  (routes
    (resolution um 10)
    (library_out
      (padstack "Via[0-3]_600:300_um" (shape (circle F.Cu 6000 0 0)) (attach off))
    )
    (network_out
      (net "/SDA"
        (wire (path F.Cu 2500 100000 -200000 150000 -200000 150000 -250000))
        (via "Via[0-3]_600:300_um" 150000 -250000)
      )
    )
  )
)"""
    tracks, vias = read_ses(session)
    assert [(t.start, t.end, t.width, t.layer, t.net) for t in tracks] == [
        ((10.0, 20.0), (15.0, 20.0), 0.25, "F.Cu", "/SDA"),
        ((15.0, 20.0), (15.0, 25.0), 0.25, "F.Cu", "/SDA"),
    ]
    assert [(v.at, v.size, v.drill, v.net) for v in vias] == [
        ((15.0, 25.0), 0.6, 0.3, "/SDA")
    ]


def test_a_plane_pad_gets_a_via_clear_of_other_nets() -> None:
    from kairo.pcb.fanout import fan_out

    outline = Outline(
        (-1.0, -0.5, 1.0, 0.5),
        (("1", -0.75, 0.0), ("2", 0.75, 0.0)),
        ((0.6, 0.8), (0.6, 0.8)),
        ("F.Cu", "F.Cu"),
    )
    fanned = fan_out(
        {"C1": outline},
        {"C1": (10.0, 10.0, 0.0)},
        {"C1": {"1": "+3V3", "2": "GND"}},
        ["GND"],
        (0.0, 0.0, 20.0, 20.0),
    )
    assert fanned.left == []
    (via,) = fanned.vias
    # Straight out of pad 2, away from the part's centre and from pad 1.
    assert via.net == "GND" and via.at[0] > 10.75 + 0.3
    (track,) = fanned.tracks
    assert track.start == (10.75, 10.0) and track.end == via.at


def test_one_shape_on_two_layers_is_two_padstacks() -> None:
    from kairo.pcb.specctra import _padstack

    top, _ = _padstack("rect", 2.0, 3.0, 0.0, ["F.Cu"])
    bottom, _ = _padstack("rect", 2.0, 3.0, 0.0, ["B.Cu"])
    assert top != bottom


def test_back_to_back_fets_are_two_rows() -> None:
    # Discharge FETs: source on BAT, drain on MID; charge FETs: drain on
    # MID, source on PACK. One footprint, one group, two banks; a small
    # driver FET of another footprint in the same block stays apart.
    def fet(n: int, source: str) -> Item:
        return Item(f"Q{n}", SQUARE, "fets", "east", None, {"1": source, "2": "MID"})

    small = Item(
        "Q9",
        Outline((-0.5, -0.5, 0.5, 0.5), (("1", -0.2, 0.0), ("2", 0.2, 0.0))),
        "fets",
        "east",
        None,
        {"1": "BAT", "2": "DRV"},
    )
    items = [*(fet(n, "BAT") for n in (1, 2, 3)), *(fet(n, "PACK") for n in (4, 5, 6))]
    units, _ = _banked([*items, small], clearance=0.5)
    assert sorted(u.ref for u in units) == ["Q9", "bank:Q1,Q2,Q3", "bank:Q4,Q5,Q6"]


def test_a_crowded_column_is_given_more_of_the_board() -> None:
    big = Outline((-10.0, -10.0, 10.0, 10.0), (("1", 0.0, 0.0),))
    items = [
        *(Item(f"Q{n}", big, "power", "east") for n in range(6)),
        Item("R1", SQUARE, "logic", "west"),
    ]
    plan = floorplan(items, 150, 90, 0.5)
    west, east = plan["west"], plan["east"]
    # Thirds would give each 50 mm; the power column needs far more.
    assert east[2] - east[0] > 80 and west[2] - west[0] >= 150 * 0.15 - 1e-9
    assert east[2] == 150 and west[0] == 0 and west[2] < east[0]


def test_an_edge_part_stands_on_its_body_and_its_keepout_hangs_off() -> None:
    # A module whose courtyard runs 6 mm above its body (an antenna
    # keep-out): on the north edge the body is flush and the rest is
    # beyond the board.
    module = Outline(
        (-5.0, -10.0, 5.0, 5.0),
        (("1", -4.0, 3.0), ("2", 4.0, 3.0)),
        ((1.0, 1.0), (1.0, 1.0)),
        ("F.Cu", "F.Cu"),
        body=(-4.5, -4.0, 4.5, 5.0),
        copper=(-4.5, 2.5, 4.5, 3.5),
    )
    places = place(
        [Item("U1", module, "radio", "north", "north", {"1": "A", "2": "B"})],
        40,
        30,
        clearance=0.5,
        effort=0.05,
    )
    _, y, turn = places["U1"]
    assert turn == 0.0 and y == pytest.approx(4.0)


def test_a_plane_via_keeps_its_class_clearance_and_out_of_keepouts() -> None:
    from kairo.pcb.fanout import fan_out

    outline = Outline(
        (-1.0, -0.5, 1.0, 0.5),
        (("1", -0.75, 0.0), ("2", 0.75, 0.0)),
        ((0.6, 0.8), (0.6, 0.8)),
        ("F.Cu", "F.Cu"),
    )
    # A high-voltage pad 1.4 mm right of the ground pad: 0.25 mm of
    # clearance leaves room for the via straight out, 0.64 mm does not.
    hv = Outline((-0.5, -0.5, 0.5, 0.5), (("1", 0.0, 0.0),), ((0.6, 0.8),), ("F.Cu",))
    common = (
        {"C1": outline, "D1": hv},
        {"C1": (10.0, 10.0, 0.0), "D1": (12.75, 10.0, 0.0)},
        {"C1": {"1": "+3V3", "2": "GND"}, "D1": {"1": "PACK_P"}},
        ["GND"],
        (0.0, 0.0, 20.0, 20.0),
    )
    loose = fan_out(*common)
    assert loose.vias[0].at[1] == pytest.approx(10.0)
    strict = fan_out(*common, clearances={"PACK_P": 0.64})
    assert strict.vias[0].at[1] != pytest.approx(10.0)
    # A keep-out over the whole right side sends the via elsewhere too.
    kept = fan_out(*common, keepouts=[(10.6, 0.0, 20.0, 20.0)])
    assert all(v.at[0] < 10.6 - 0.3 for v in kept.vias)


def test_classes_reach_the_router_and_the_project_file() -> None:
    from kairo.ir import Board
    from kairo.pcb.rules import apply_rules, routing_classes

    board = Board.model_validate(
        {
            "width": "50mm",
            "height": "40mm",
            "net_classes": [
                {"name": "Default", "track": "0.25mm", "clearance": "0.2mm"},
                {
                    "name": "HV",
                    "nets": ["PACK_P"],
                    "track": "1mm",
                    "clearance": "0.64mm",
                },
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
        }
    )
    nets = ["PACK_P", "/power/PACK_P_SNS", "/power/PACK_P", "GND"]
    classes = {c.name: c for c in routing_classes(board, nets)}
    assert classes["HV"].nets == ("PACK_P", "/power/PACK_P")
    assert classes["HV"].clearance == 0.64
    assert set(classes["default"].nets) == {"/power/PACK_P_SNS", "GND"}
    project: dict[str, object] = {
        "net_settings": {"classes": [], "meta": {"version": 0}}
    }
    apply_rules(project, board, nets)
    settings = project["net_settings"]
    assert isinstance(settings, dict)
    assert [c["name"] for c in cast("list[dict[str, str]]", settings["classes"])] == [
        "Default",
        "HV",
    ]
    assert settings["netclass_patterns"] == [
        {"netclass": "HV", "pattern": "PACK_P"},
        {"netclass": "HV", "pattern": "/power/PACK_P"},
    ]
    # A new project's version 0 block: KiCad 10 would ignore the patterns.
    assert settings["meta"] == {"version": 4}
    rules = project["board"]["design_settings"]["rules"]  # type: ignore[index]
    assert rules["min_through_hole_diameter"] == 0.15


def test_a_board_without_a_default_class_is_refused() -> None:
    from pydantic import ValidationError

    from kairo.ir import Design

    with pytest.raises(ValidationError, match="Default"):
        Design.model_validate(
            {
                "design": "d",
                "nets": {"A": {"role": "signal"}},
                "components": {},
                "connections": {},
                "board": {
                    "width": "10mm",
                    "height": "10mm",
                    "net_classes": [
                        {
                            "name": "HV",
                            "nets": ["A"],
                            "track": "1mm",
                            "clearance": "1mm",
                        }
                    ],
                },
            }
        )


def test_a_summary_keeps_a_land_patterns_own_findings_apart() -> None:
    from kairo.pcb.evaluate import evaluate
    from kairo.pcb.layout import Laid
    from kairo.pcb.make import Made, summarize

    def item(text: str) -> dict[str, str]:
        return {"description": text}

    report = {
        "violations": [
            {
                "type": "hole_clearance",
                "items": [item("Pad A1 [GND] of J5 on F.Cu"), item("NPTH pad of J5")],
            },
            {
                "type": "clearance",
                "items": [
                    item("Track [GND] on F.Cu, length 1 mm"),
                    item("Pad 16 [/VC0] of U1 on F.Cu"),
                ],
            },
            {"type": "silk_overlap", "items": [item("Reference field of R1")]},
        ],
        "unconnected_items": [
            {
                "items": [
                    item("Pad 1 [PACK_P] of R63 on F.Cu"),
                    item("Zone [PACK_P] on F.Cu"),
                ]
            }
        ],
    }
    laid = Laid("", {}, {}, 1, {}, cast("dict[str, object]", report))
    summary = summarize(Made(laid, evaluate("(kicad_pcb)"), "test"))
    assert list(summary["land_pattern"]) == ["hole_clearance"]
    assert list(summary["blocking"]) == ["clearance"]
    assert summary["cosmetic"] == {"silk_overlap": 1}
    assert summary["unconnected"]["by_net"] == {"PACK_P": 1}
    assert summary["ready"] is False


def test_the_newest_freerouting_runs_on_a_new_enough_java(tmp_path: Path) -> None:
    from kairo.pcb.route import find_freerouting

    for name in ("freerouting-2.1.0.jar", "freerouting-2.5.0.jar"):
        (tmp_path / name).write_bytes(b"")
    old = tmp_path / "jdk-21.0.2" / "bin"
    new = tmp_path / "jdk-25.0.4.1+1" / "bin"
    for folder in (old, new):
        folder.mkdir(parents=True)
        (folder / "java.exe").write_bytes(b"")
    tool = find_freerouting(tmp_path, passes=5)
    assert tool is not None
    assert tool.jar.name == "freerouting-2.5.0.jar" and "jdk-25" in tool.java
    assert tool.passes == 5
    # Java 21 alone cannot run it.
    shutil.rmtree(tmp_path / "jdk-25.0.4.1+1")
    assert find_freerouting(tmp_path) is None


class _OneFootprint:
    """A library index holding one footprint file."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def footprint_file(self, footprint: str) -> Path:
        return self.path


def test_two_classes_keep_the_larger_clearance_between_them(tmp_path: Path) -> None:
    from kairo.pcb.board_writer import PlacedPart
    from kairo.pcb.specctra import NetClass, write_dsn

    footprint = tmp_path / "R.kicad_mod"
    footprint.write_text(
        '(footprint "R" (layer "F.Cu")'
        ' (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu"))'
        ' (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu")))',
        encoding="utf-8",
    )
    parts = [
        PlacedPart("R1", "L:R", "1k", 10, 10, 0, "/a", "", "", {"1": "HV", "2": "SIG"}),
        PlacedPart("R2", "L:R", "1k", 20, 10, 0, "/b", "", "", {"1": "HV", "2": "SIG"}),
    ]
    text = write_dsn(
        "b",
        parts,
        [(0, 0), (30, 0), (30, 20), (0, 20)],
        cast("Any", _OneFootprint(footprint)),
        layers=("F.Cu", "B.Cu"),
        classes=[
            NetClass("hv", 1.0, 0.64, ("HV",)),
            NetClass("default", 0.25, 0.2, ("SIG",)),
        ],
    )
    assert '(class_class (classes "hv" "default") (rule (clearance 640)))' in text


def test_each_filled_island_is_a_zone_of_its_own() -> None:
    from kairo.pcb.zones import filled_islands

    board = """(kicad_pcb
  (zone (net "+BATT") (layer "F.Cu") (polygon (pts (xy 0 0) (xy 9 0) (xy 9 9)))
    (filled_polygon (layer "F.Cu") (pts (xy 0 0) (xy 3 0) (xy 3 3)))
    (filled_polygon (layer "F.Cu") (pts (xy 6 6) (xy 9 6) (xy 9 9))))
  (zone (net "GND") (layer "In1.Cu") (polygon (pts (xy 0 0) (xy 9 0) (xy 9 9))))
)"""
    islands = filled_islands(board)
    assert [(z.net, z.layer, z.outline[0]) for z in islands] == [
        ("+BATT", "F.Cu", (0.0, 0.0)),
        ("+BATT", "F.Cu", (6.0, 6.0)),
    ]


def test_islands_are_stitched_where_they_overlap_from_layer_to_layer() -> None:
    from kairo.pcb.zones import island_stitches

    # +BATT on F.Cu broken in two (a track crossed it); on B.Cu whole.
    board = """(kicad_pcb
  (zone (net "+BATT") (layer "F.Cu")
    (filled_polygon (layer "F.Cu") (pts (xy 0 0) (xy 4 0) (xy 4 4) (xy 0 4)))
    (filled_polygon (layer "F.Cu") (pts (xy 6 0) (xy 10 0) (xy 10 4) (xy 6 4))))
  (zone (net "+BATT") (layer "B.Cu")
    (filled_polygon (layer "B.Cu") (pts (xy 0 0) (xy 10 0) (xy 10 4) (xy 0 4))))
)"""
    vias = island_stitches(board, ["+BATT"], ["F.Cu", "B.Cu"])
    # One via joins each F.Cu island to the B.Cu one: two in all.
    assert len(vias) == 2
    assert {v.at[0] < 5 for v in vias} == {True, False}
    # A via of the net already joining one of them leaves only the other.
    joined = board.replace(
        "\n)", '\n  (via (at 2 2) (size 0.6) (drill 0.3) (net "+BATT"))\n)'
    )
    assert [
        v.at[0] > 5 for v in island_stitches(joined, ["+BATT"], ["F.Cu", "B.Cu"])
    ] == [True]


def test_pour_stitches_keep_a_holes_spacing_from_through_pads() -> None:
    from kairo.pcb.geometry import point_box
    from kairo.pcb.zones import island_stitches

    board = """(kicad_pcb
  (zone (net "+BATT") (layer "F.Cu")
    (filled_polygon (layer "F.Cu") (pts (xy 0 0) (xy 10 0) (xy 10 4) (xy 0 4))))
  (zone (net "+BATT") (layer "B.Cu")
    (filled_polygon (layer "B.Cu") (pts (xy 0 0) (xy 4 0) (xy 4 4) (xy 0 4))))
)"""
    # One stitch joins the two layers' islands. The first point that fits,
    # (1, 1), lies in a through pad's copper.
    pad = (0.5, 0.5, 1.5, 1.5)
    free = island_stitches(board, ["+BATT"], ["F.Cu", "B.Cu"])
    assert [v.at for v in free] == [(1.0, 1.0)]
    kept = island_stitches(board, ["+BATT"], ["F.Cu", "B.Cu"], through=[pad])
    assert kept
    assert all(point_box(v.at, pad) >= 0.35 for v in kept)


def test_filled_islands_reach_the_router_as_copper_areas(tmp_path: Path) -> None:
    from kairo.pcb.board_writer import PlacedPart, Zone
    from kairo.pcb.specctra import NetClass, write_dsn

    footprint = tmp_path / "R.kicad_mod"
    footprint.write_text(
        '(footprint "R" (layer "F.Cu")'
        ' (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu"))'
        ' (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu")))',
        encoding="utf-8",
    )
    part = PlacedPart("R1", "L:R", "1k", 10, 10, 0, "/a", "", "", {"1": "P", "2": "Q"})
    island = Zone("P", "F.Cu", ((0.0, 0.0), (4.0, 0.0), (4.0, 4.0)))
    text = write_dsn(
        "b",
        [part],
        [(0, 0), (30, 0), (30, 20), (0, 20)],
        cast("Any", _OneFootprint(footprint)),
        layers=("F.Cu", "B.Cu"),
        classes=[NetClass("default", 0.25, 0.2, ("P", "Q"))],
        areas=[island],
    )
    assert "(wire (polygon F.Cu 0 " in text and '(net "P") (type protect))' in text
    assert "(plane " not in text


def test_unrouted_poured_nets_point_at_the_pours() -> None:
    from kairo.ir import Design
    from kairo.pcb.make import next_step

    design = Design.model_validate(
        {
            "design": "d",
            "nets": {
                "+BATT": {"role": "power", "voltage": "48V"},
                "SDA": {"role": "signal"},
            },
            "components": {},
            "connections": {},
            "board": {"width": "10mm", "height": "10mm", "pours": [{"net": "+BATT"}]},
        }
    )
    summary = {
        "ready": False,
        "blocking": {},
        "unconnected": {"total": 4, "by_net": {"+BATT": 3, "/SDA": 1}},
    }
    assert "board.pours" in next_step(summary, design)
    summary["unconnected"] = {"total": 4, "by_net": {"+BATT": 1, "/SDA": 3}}
    assert "board.routes" in next_step(summary, design)
    many = {f"/S{n}": 2 for n in range(8)}
    summary["unconnected"] = {"total": 16, "by_net": many}
    assert "board.sides" in next_step(summary, design)


def test_a_custom_pad_is_as_large_as_its_drawn_copper(tmp_path: Path) -> None:
    from kairo.pcb.place import read_outline

    footprint = tmp_path / "TOLL.kicad_mod"
    footprint.write_text(
        '(footprint "TOLL" (layer "F.Cu")'
        ' (pad "3" smd custom (at 3.8 0) (size 2.9 8.7) (layers "B.Cu")'
        "  (primitives"
        "   (gr_poly (pts (xy 2.85 5.05) (xy 1.45 5.05)"
        "    (xy 1.45 -5.05) (xy 2.85 -5.05)) (width 0))"
        "   (gr_poly (pts (xy -1.45 4.85) (xy -5.15 4.85)"
        "    (xy -5.15 -4.85) (xy -1.45 -4.85)) (width 0)))))",
        encoding="utf-8",
    )
    outline = read_outline(cast("Any", _OneFootprint(footprint)), "L:TOLL")
    (number, x, y), size = outline.pads[0], outline.sizes[0]
    # Its copper runs from 3.8 - 5.15 to 3.8 + 2.85, not the anchor's 2.9 mm.
    assert number == "3" and x == pytest.approx(2.65) and y == 0.0
    assert size == pytest.approx((8.0, 10.1))
    assert outline.layers == ("B.Cu",)


def test_a_paste_aperture_is_not_copper(tmp_path: Path) -> None:
    from kairo.pcb.place import read_outline

    # A power package draws paste apertures as pads with no number, on
    # F.Paste only, over its copper pads.
    footprint = tmp_path / "Q.kicad_mod"
    footprint.write_text(
        '(footprint "Q" (layer "F.Cu")'
        ' (pad "1" smd rect (at 0 0) (size 0.8 0.8) (layers "F.Cu" "F.Mask"))'
        ' (pad "" smd rect (at 0 0) (size 0.7 0.7) (layers "F.Paste")))',
        encoding="utf-8",
    )
    outline = read_outline(cast("Any", _OneFootprint(footprint)), "L:Q")
    assert outline.layers == ("F.Cu", "")


def test_a_receptacle_drawn_mouth_down_faces_out_with_its_mouth() -> None:
    from kairo.pcb.place import facing

    # Pads at the top, the body (and its mouth) reaching 8 mm below them,
    # as KiCad draws a USB-C receptacle.
    usb = Outline(
        (-4.0, -5.0, 4.0, 5.0),
        (("1", -1.0, -4.0), ("2", 1.0, -4.0)),
        ((0.5, 1.0), (0.5, 1.0)),
        ("F.Cu", "F.Cu"),
        body=(-4.0, -4.0, 4.0, 5.0),
        copper=(-1.25, -4.5, 1.25, -3.5),
    )
    assert facing(usb) == "south"
    places = place(
        [Item("J5", usb, "usb", "south", "south", {"1": "A", "2": "B"})],
        40,
        30,
        clearance=0.5,
        effort=0.05,
    )
    _, y, turn = places["J5"]
    # Not turned: its mouth already faces the south edge, flush with it.
    assert turn == 0.0 and y == pytest.approx(30 - 5.0)
    # A part that overhangs alike all round keeps its top outward.
    assert facing(SQUARE) == "north"


def test_a_ground_via_keeps_out_of_a_neighbouring_pins_way_out() -> None:
    from kairo.pcb.fanout import _corridor, fan_out
    from kairo.pcb.geometry import point_box

    square = Outline(
        (-0.5, -0.5, 0.5, 0.5), (("1", 0.0, 0.0),), ((0.6, 0.6),), ("F.Cu",)
    )
    tall = Outline((-0.2, -0.7, 0.2, 0.7), (("1", 0.0, 0.0),), ((0.3, 1.2),), ("F.Cu",))
    # The ground pad's part lies below it, so its via would go straight
    # up, into the way out of the signal pin above.
    fanned = fan_out(
        {"G": square, "S": tall, "P": square},
        {"G": (10.0, 10.0, 0.0), "S": (10.0, 7.4, 0.0), "P": (10.0, 12.0, 0.0)},
        {"G": {"1": "GND"}, "S": {"1": "SIG"}, "P": {"1": "X"}},
        ["GND"],
        (0.0, 0.0, 20.0, 20.0),
    )
    (via,) = [v for v in fanned.vias if v.net == "GND"]
    lane = _corridor((9.85, 6.8, 10.15, 8.0))
    assert point_box(via.at, lane) >= 0.3


def _ic(pitch: float, nets: dict[str, str]) -> Outline:
    """An IC with eight pins, four down each side at ``pitch``."""
    pads: list[tuple[str, float, float]] = []
    for k in range(4):
        pads.append((str(k + 1), -1.5, (k - 1.5) * pitch))
        pads.append((str(8 - k), 1.5, (k - 1.5) * pitch))
    return Outline(
        (-2.0, -2.0, 2.0, 2.0),
        tuple(pads),
        tuple((0.6, pitch * 0.5) for _ in pads),
        tuple("F.Cu" for _ in pads),
    )


def test_a_fine_pitch_pin_keeps_its_front_free() -> None:
    from kairo.pcb.place import _fronts

    nets = {str(n): f"P{n}" for n in range(1, 9)}
    ic = Item("U1", _ic(0.5, nets), "mcu", "center", None, nets)
    zones = _fronts(ic)
    assert len(zones) == 8
    assert all(abs(z[2] - z[0]) == pytest.approx(2.0) for z, _ in zones)
    # Coarse pitch: no fronts.
    assert _fronts(Item("U2", _ic(1.27, nets), "mcu", "center", None, nets)) == []
    others = [
        Item(f"R{n}", SQUARE, "mcu", "center", None, {"1": "X", "2": "Y"})
        for n in range(6)
    ]
    places = place([ic, *others], 20, 20, clearance=0.5, effort=0.5)
    ux, uy, ut = places["U1"]
    for ref, (x, y, _) in places.items():
        if ref == "U1":
            continue
        box = (x - 1.25, y - 1.25, x + 1.25, y + 1.25)
        for zone, _ in zones:
            left, top, right, bottom = turned(zone, ut)
            z = (ux + left, uy + top, ux + right, uy + bottom)
            assert not (
                box[0] < z[2] and z[0] < box[2] and box[1] < z[3] and z[1] < box[3]
            )


def test_support_parts_are_those_of_the_ics_own_block() -> None:
    from kairo.pcb.place import satellites

    nets = {str(n): f"S{n}" for n in range(1, 9)} | {"8": "GND"}
    ic = Item("U1", _ic(0.5, nets), "monitor", "center", None, nets)
    other = Item("U2", _ic(0.5, {"1": "S2"}), "monitor", "center", None, {"1": "S2"})
    cap = Item("C1", SQUARE, "monitor", "center", None, {"1": "S1", "2": "GND"})
    # Wired only to U1, but its block (the FETs it measures) says otherwise.
    sensor = Item("TH1", SQUARE, "fets", "east", None, {"1": "S3", "2": "GND"})
    # Tied as much to U2 as to U1: it serves neither.
    shared = Item("R1", SQUARE, "monitor", "center", None, {"1": "S2", "2": "GND"})
    found = satellites([ic, other, cap, sensor, shared], {"GND": 0.05})
    assert set(found) == {"C1"}
    assert found["C1"].ic == "U1" and found["C1"].pins == ("1",)


def test_a_support_part_is_seated_at_its_pin_not_pulled_away() -> None:
    nets = {str(n): f"S{n}" for n in range(1, 9)}
    ic = Item("U1", _ic(0.5, nets), "monitor", "west", None, nets)
    # The filter capacitor's other net runs to a connector on the far
    # edge, weighted heavily: wirelength alone drags it to the connector.
    cap = Item("C1", SQUARE, "monitor", "west", None, {"1": "S1", "2": "TAP"})
    conn = Item("J1", SQUARE, "conn", "east", "east", {"1": "TAP"})
    # The IC's other pins hold it to a header on the near edge.
    header = Outline(
        (-1.0, -4.0, 1.0, 4.0), tuple((str(n), 0.0, n - 3.5) for n in range(2, 9))
    )
    pins = {str(n): f"S{n}" for n in range(2, 9)}
    held = Item("J2", header, "conn", "west", "west", pins)
    weights = {"TAP": 4.0} | {f"S{n}": 4.0 for n in range(2, 9)}
    parts = [ic, cap, conn, held]
    seated = place(parts, 60, 20, clearance=0.5, weights=weights, effort=0.5)
    loose = place(parts, 60, 20, clearance=0.5, weights=weights, effort=0.5, seat=False)

    def gap(places: dict[str, tuple[float, float, float]]) -> float:
        from kairo.pcb.geometry import rotate

        ux, uy, ut = places["U1"]
        dx, dy = rotate(-1.5, -0.75, ut)
        cx, cy, _ = places["C1"]
        return ((ux + dx - cx) ** 2 + (uy + dy - cy) ** 2) ** 0.5

    assert gap(seated) < 5.0
    assert gap(loose) > 10.0


def test_the_placement_report_measures_each_local_part_to_its_pin() -> None:
    from kairo.pcb.place import placement_report

    nets = {str(n): f"S{n}" for n in range(1, 9)} | {"1": "VDD", "8": "GND"}
    ic = Item("U1", _ic(1.0, nets), "mcu", "center", None, nets)
    cap = Item("C1", SQUARE, "mcu", "center", None, {"1": "VDD", "2": "GND"})
    # U1's pin 1 at (8.5, 8.5); C1's pad 1 at (5.5, 8.5): 3 mm.
    report = placement_report(
        [ic, cap], {"U1": (10.0, 10.0, 0.0), "C1": (6.0, 8.5, 0.0)}, {"GND": 0.05}
    )
    assert report["pairs"] == 1 and report["max_mm"] == pytest.approx(3.0)
    assert report["worst"] == ["C1.1 to U1: 3.0 mm"]


def test_a_wide_net_drawn_narrower_than_its_class_is_reported() -> None:
    from kairo.pcb.board_writer import Track
    from kairo.pcb.layout import narrowed
    from kairo.pcb.specctra import NetClass

    classes = [
        NetClass("supply", 0.5, 0.2, ("+BATT",)),
        NetClass("default", 0.25, 0.2, ("SIG",)),
    ]
    tracks = [
        Track((0.0, 0.0), (10.0, 0.0), 0.5, "F.Cu", "+BATT"),
        Track((10.0, 0.0), (11.0, 0.0), 0.3, "F.Cu", "+BATT"),
        Track((11.0, 0.0), (11.5, 0.0), 0.2, "F.Cu", "+BATT"),
        # A default net necked is the narrowest class's business: not listed.
        Track((0.0, 5.0), (1.0, 5.0), 0.15, "F.Cu", "SIG"),
    ]
    assert narrowed(tracks, classes) == {
        "+BATT": {"width": 0.5, "least": 0.2, "mm": 1.5, "longest": 1.0, "outer": 1.0}
    }


def test_pour_stitching_keeps_the_class_clearance_from_other_nets_pads() -> None:
    from kairo.pcb.board_writer import Zone
    from kairo.pcb.zones import stitching

    square = ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0))
    zones = [Zone("PACK_P", "F.Cu", square), Zone("PACK_P", "B.Cu", square)]
    # Another net's pad 0.3 mm from the via the (5, 5) grid point would get:
    # outside any courtyard, but nearer than the pour's 0.64 mm.
    pad = ("SENSE", (4.0, 5.6, 6.0, 6.6))
    loose = stitching(zones, [])
    assert (5.0, 5.0) in [v.at for v in loose]
    kept = stitching(zones, [], pads=[pad], gap=lambda a, b: 0.64)
    # (5, 5) at 0.3 mm and (5, 7.5) at 0.6 mm go; the rest stay.
    assert {v.at for v in loose} - {v.at for v in kept} == {(5.0, 5.0), (5.0, 7.5)}


def test_a_fanout_stub_runs_straight_or_at_45_degrees() -> None:
    import math

    from kairo.pcb.fanout import fan_out

    # The ground pad sits off the part's axis, 22 degrees round from its
    # centre: the stub still leaves at a multiple of 45 degrees.
    outline = Outline(
        (-1.5, -1.0, 1.5, 1.0),
        (("1", -0.75, 0.0), ("2", 0.75, 0.3)),
        ((0.6, 0.8), (0.6, 0.8)),
        ("F.Cu", "F.Cu"),
    )
    fanned = fan_out(
        {"C1": outline},
        {"C1": (10.0, 10.0, 0.0)},
        {"C1": {"1": "+3V3", "2": "GND"}},
        ["GND"],
        (0.0, 0.0, 20.0, 20.0),
    )
    (track,) = fanned.tracks
    (x0, y0), (x1, y1) = track.start, track.end
    angle = math.degrees(math.atan2(y1 - y0, x1 - x0)) % 45
    assert min(angle, 45 - angle) < 0.5


def test_classes_split_by_voltage_keep_ipc_spacing_between_far_apart_taps() -> None:
    from kairo.ir import Board
    from kairo.pcb.rules import pair_rules, routing_classes
    from kairo.pcb.specctra import between

    board = Board.model_validate(
        {
            "width": "50mm",
            "height": "40mm",
            "spacing": "B2",
            "net_classes": [
                {"name": "Default", "track": "0.25mm", "clearance": "0.2mm"}
            ],
        }
    )
    # Cell taps at full charge, 4.2 V a cell.
    volts = {"GND": (0.0, 0.0), "VC4": (16.8, 16.8), "VC8": (33.6, 33.6)}
    classes = {c.name: c for c in routing_classes(board, ["GND", "VC4", "VC8"], volts)}
    assert set(classes) == {"Default@0-15V", "Default@15-30V", "Default@30-45V"}
    low, mid, high = (
        classes["Default@0-15V"],
        classes["Default@15-30V"],
        classes["Default@30-45V"],
    )
    # 33.6 V apart: IPC-2221 B2 (bare outer layer) asks 0.64 mm.
    assert between(low, high) == 0.64
    # 16.8 V apart: B2 asks 0.1 mm, less than the class's own 0.2.
    assert between(low, mid) == 0.2 and between(mid, high) == 0.2
    rules = pair_rules(list(classes.values()))
    assert "A.NetClass == 'Default@0-15V' && B.NetClass == 'Default@30-45V'" in rules
    assert "A.NetClass == 'Default@30-45V' && B.NetClass == 'Default@0-15V'" in rules
    assert "(constraint clearance (min 0.64mm))" in rules
    # Without a spacing column, one class as before.
    plain = board.model_copy(update={"spacing": None})
    assert [c.name for c in routing_classes(plain, ["GND", "VC8"], volts)] == [
        "default"
    ]


def test_narrowed_copper_is_checked_against_its_nets_stated_current() -> None:
    from kairo.ir import Design
    from kairo.pcb.layout import current_checks

    design = Design.model_validate(
        {
            "design": "neck",
            "nets": {
                "LOW": {"role": "signal", "current": "0.5A"},
                "HIGH": {"role": "signal", "current": "1A"},
                "OPEN": {"role": "signal"},
            },
            "components": {},
            "connections": {},
            "board": {"width": "20mm", "height": "20mm", "outer_oz": 1},
        }
    )
    # 0.15 mm of 1 oz outer copper at 10 degC of rise: IPC-2221 gives
    # 0.048 * 10^0.44 * (5.9 mil * 1.38 mil)^0.725, about 0.6 A.
    neck = {"width": 0.5, "least": 0.15, "mm": 2.0, "longest": 2.0, "outer": 1.0}
    found = current_checks({"LOW": neck, "HIGH": neck, "OPEN": neck}, design)
    assert found["LOW"]["verdict"] == "pass"
    assert found["HIGH"]["verdict"] == "fail"
    assert found["OPEN"]["verdict"] == "unverified"
    assert found["LOW"]["carries"] == pytest.approx(0.6, abs=0.05)


def test_reference_labels_keep_clear_of_pads_each_other_and_the_edge() -> None:
    from kairo.pcb.silk import CHAR_WIDTH, LINE_HEIGHT, labels

    outline = Outline(
        (-1.0, -0.5, 1.0, 0.5),
        (("1", -0.75, 0.0), ("2", 0.75, 0.0)),
        ((0.6, 0.8), (0.6, 0.8)),
        ("F.Cu", "F.Cu"),
    )
    # Three resistors in a row, 3 mm apart, near the top edge.
    places = {f"R{k}": (5.0 + 3.0 * k, 2.0, 0.0) for k in range(1, 4)}
    found = labels(dict.fromkeys(places, outline), places, (0.0, 0.0, 20.0, 20.0))
    boxes: list[tuple[float, float, float, float]] = []
    for ref, mark in found.items():
        assert mark.printed
        w, h = len(ref) * mark.size * CHAR_WIDTH, mark.size * LINE_HEIGHT
        if mark.angle == 90.0:
            w, h = h, w
        boxes.append((mark.x - w / 2, mark.y - h / 2, mark.x + w / 2, mark.y + h / 2))
    pads = [
        (x + dx - 0.3, y - 0.4, x + dx + 0.3, y + 0.4)
        for x, y, _ in places.values()
        for dx in (-0.75, 0.75)
    ]

    def hit(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
        return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]

    assert not any(hit(b, p) for b in boxes for p in pads)
    assert not any(hit(a, b) for k, a in enumerate(boxes) for b in boxes[k + 1 :])
    assert all(b[1] >= 0.5 and b[0] >= 0.5 for b in boxes)


def test_routes_to_take_first_are_found_by_kicads_names_and_layers_checked() -> None:
    from kairo.ir import Design
    from kairo.pcb.layout import (  # pyright: ignore[reportPrivateUsage]
        LayoutError,
        _guided,
    )

    def design(routes: list[dict[str, object]]) -> Design:
        return Design.model_validate(
            {
                "design": "d",
                "nets": {"VC9": {"role": "signal"}},
                "components": {},
                "connections": {},
                "board": {"width": "10mm", "height": "10mm", "routes": routes},
            }
        )

    copper = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    ok = design([{"net": "VC9", "layers": ["In2.Cu"]}])
    assert _guided(ok, ["GND", "/VC9"], copper) == [("/VC9", ["In2.Cu"])]
    with pytest.raises(LayoutError, match="lacks"):
        _guided(design([{"net": "VC8"}]), ["/VC9"], copper)
    with pytest.raises(LayoutError, match=r"In3.Cu"):
        _guided(design([{"net": "VC9", "layers": ["In3.Cu"]}]), ["/VC9"], copper)


def test_a_net_routed_the_long_way_round_is_listed_as_a_detour() -> None:
    from kairo.pcb.board_writer import Track, Via
    from kairo.pcb.layout import detours

    pads = [
        ("A", (0.0, 0.0, 1.0, 1.0)),
        ("A", (5.0, 0.0, 6.0, 1.0)),
        ("B", (0.0, 10.0, 1.0, 11.0)),
        ("B", (5.0, 10.0, 6.0, 11.0)),
    ]
    # A goes 5 mm straight; B, the same span, goes 20 mm round with vias.
    tracks = [
        Track((0.5, 0.5), (5.5, 0.5), 0.25, "F.Cu", "A"),
        Track((0.5, 10.5), (0.5, 18.0), 0.25, "F.Cu", "B"),
        Track((0.5, 18.0), (5.5, 18.0), 0.25, "B.Cu", "B"),
        Track((5.5, 18.0), (5.5, 10.5), 0.25, "F.Cu", "B"),
    ]
    vias = [Via((0.5, 18.0), 0.6, 0.3, "B"), Via((5.5, 18.0), 0.6, 0.3, "B")]
    assert detours(tracks, vias, pads) == {
        "B": {"mm": 20.0, "span_mm": 5.0, "vias": 2.0}
    }


def test_two_vias_of_one_net_at_one_spot_are_drilled_once() -> None:
    from kairo.pcb.board_writer import Via
    from kairo.pcb.layout import unique_vias

    vias = [
        Via((140.0, 100.0), 0.6, 0.3, "+BATT"),
        Via((140.0, 100.00000000001), 0.6, 0.3, "+BATT"),
        Via((142.5, 100.0), 0.6, 0.3, "+BATT"),
    ]
    assert unique_vias(vias) == vias[1:]


def test_a_bank_of_fets_stays_a_row_when_each_has_a_gate_resistor() -> None:
    def fet(k: int) -> Item:
        # Pins 1-3 the source, 4 the gate, 5-8 the drain: a power FET.
        nets = {"1": "S", "2": "S", "3": "S", "4": f"G{k}"} | {
            str(n): "D" for n in range(5, 9)
        }
        return Item(f"Q{k}", _ic(1.27, nets), "fets", "center", None, nets)

    fets = [fet(k) for k in range(1, 5)]
    resistors = [
        Item(f"R{k}", SQUARE, "fets", "center", None, {"1": f"G{k}", "2": "DRV"})
        for k in range(1, 5)
    ]
    places = place([*fets, *resistors], 40, 40, clearance=0.5, effort=0.3)
    rows = {places[f"Q{k}"][1] for k in range(1, 5)}
    cols = {places[f"Q{k}"][0] for k in range(1, 5)}
    # One row (or column): the members share a line, at one pitch.
    assert len(rows) == 1 or len(cols) == 1


def test_a_missing_connection_is_read_from_kicads_report_with_its_ends() -> None:
    from kairo.pcb.layout import gaps

    report = {
        "unconnected_items": [
            {
                "items": [
                    {
                        "description": "Track [/PDSG] on F.Cu, length 3.7500 mm",
                        "pos": {"x": 93.0, "y": 44.5},
                    },
                    {
                        "description": "Pad 39 [/PDSG] of U1 on F.Cu",
                        "pos": {"x": 92.25, "y": 46.8375},
                    },
                ]
            }
        ]
    }
    assert gaps(report) == [("/PDSG", (93.0, 44.5), (92.25, 46.8375))]


def test_only_other_nets_tracks_near_a_missing_connection_are_taken_up() -> None:
    from kairo.pcb.board_writer import Track, Via
    from kairo.pcb.layout import take_up

    across = Track((5.0, -3.0), (5.0, 3.0), 0.25, "F.Cu", "B")  # crosses the way
    far = Track((5.0, 20.0), (9.0, 20.0), 0.25, "F.Cu", "C")
    own = Track((0.0, 0.0), (2.0, 0.0), 0.25, "F.Cu", "A")
    via = Via((5.0, 1.0), 0.6, 0.3, "B")
    away = Via((5.0, 15.0), 0.6, 0.3, "B")
    kept, ripped, kept_vias, gone = take_up(
        [across, far, own], [via, away], "A", (0.0, 0.0), (10.0, 0.0), 2.0
    )
    assert ripped == {id(across)} and kept == [far, own]
    assert gone == {id(via)} and kept_vias == [away]


def test_a_part_serving_only_an_ic_of_another_block_is_flagged() -> None:
    from kairo.pcb.place import Stray, strays

    nets = {str(n): f"VC{n}" for n in range(1, 8)} | {"8": "GND"}
    ic = Item("U1", _ic(0.5, nets), "monitor", "center", None, nets)
    fet = Item("Q1", _ic(1.27, {"1": "G"}), "cells", "west", None, {"1": "G"})
    # Both its nets are U1's, but it sits with the cells: flagged.
    filt = Item("C1", SQUARE, "cells", "west", None, {"1": "VC1", "2": "VC2"})
    # In U1's own block: seated there, nothing to flag.
    dec = Item("C2", SQUARE, "monitor", "center", None, {"1": "VC3", "2": "GND"})
    # A net that is not U1's (a cell tap): it serves more than U1.
    series = Item("R1", SQUARE, "cells", "west", None, {"1": "VC4", "2": "TAP"})
    # Wired to two ICs: it serves neither alone.
    both = Item("R2", SQUARE, "cells", "west", None, {"1": "VC5", "2": "G"})
    # A rail shared with a module on the edge: it serves both.
    module = Item("U5", _ic(1.27, {"1": "VC6"}), "mcu", "south", "south", {"1": "VC6"})
    rail = Item("C3", SQUARE, "supply", "center", None, {"1": "VC6", "2": "GND"})
    found = strays([ic, fet, filt, dec, series, both, module, rail], {"GND": 0.05})
    assert found == [Stray("C1", "cells", "U1", "monitor")]
    # An output capacitor on a supply rail only one large IC uses: the rail.
    out = Item("C4", SQUARE, "supply", "center", None, {"1": "VC7", "2": "GND"})
    assert [s.ref for s in strays([ic, out], {"GND": 0.05})] == ["C4"]
    assert strays([ic, out], {"GND": 0.05}, rails={"VC7", "GND"}) == []


def test_parts_in_the_wrong_block_come_first_when_connections_are_left() -> None:
    from kairo.ir import Design
    from kairo.pcb.make import next_step

    design = Design.model_validate(
        {
            "design": "d",
            "nets": {"VC1": {"role": "signal"}},
            "components": {},
            "connections": {},
            "board": {"width": "10mm", "height": "10mm"},
        }
    )
    summary = {
        "ready": False,
        "blocking": {},
        "unconnected": {"total": 2, "by_net": {"/VC1": 2}},
        "blocks": [{"part": "C1", "block": "cells", "serves": "U1", "fix": "..."}],
    }
    said = next_step(summary, design)
    assert "C1" in said and "blocks" in said
