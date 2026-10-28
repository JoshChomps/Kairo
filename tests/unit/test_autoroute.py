"""Kairo's own router on small boards whose answers are known."""

# The router's copper shapes are built directly here.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import math

import pytest

from kairo.pcb.autoroute import Settings, route_board
from kairo.pcb.autoroute.grid import _Shape
from kairo.pcb.specctra import NetClass

BOARD = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]
TUNE = Settings(pitch=0.25, iterations=10, margin=5.0)


def pad(net: str, x: float, y: float, layer: str = "F.Cu", size: float = 1.0) -> _Shape:
    h = size / 2
    return _Shape(net, layer, box=(x - h, y - h, x + h, y + h))


def default(*nets: str) -> list[NetClass]:
    return [NetClass("default", 0.25, 0.2, nets)]


def test_two_pads_in_a_line_are_one_straight_track() -> None:
    tracks, vias = route_board(
        [pad("A", 3, 10), pad("A", 17, 10)], BOARD, ["F.Cu"], default("A"), TUNE
    )
    assert vias == []
    assert len(tracks) == 1
    (track,) = tracks
    assert track.start[1] == track.end[1] == 10.0 and track.layer == "F.Cu"


def test_a_diagonal_run_is_one_45_degree_track() -> None:
    tracks, _ = route_board(
        [pad("A", 3, 3), pad("A", 13, 13)], BOARD, ["F.Cu"], default("A"), TUNE
    )
    assert len(tracks) == 1
    (t,) = tracks
    assert abs(t.end[0] - t.start[0]) == abs(t.end[1] - t.start[1])


def test_copper_already_joined_is_left_alone() -> None:
    joined = _Shape("A", "F.Cu", box=(3, 9.5, 17, 10.5))
    tracks, vias = route_board(
        [pad("A", 3, 10), pad("A", 17, 10), joined], BOARD, ["F.Cu"], default("A"), TUNE
    )
    assert tracks == [] and vias == []


def crossing() -> list[_Shape]:
    # A runs left to right, B top to bottom, through the middle; walls
    # close every way round on the one layer.
    # Every pad stands against its edge, so neither net can pass round the
    # other's pads.
    shapes = [pad("A", 1, 10), pad("A", 19, 10), pad("B", 10, 1), pad("B", 10, 19)]
    for x0, y0, x1, y1 in (
        (0, 0, 8, 8),
        (12, 0, 20, 8),
        (0, 12, 8, 20),
        (12, 12, 20, 20),
    ):
        # Copper of no net, such as a mounting hole's: in everyone's way.
        shapes.append(_Shape("", "*", box=(x0 + 0.5, y0 + 0.5, x1 - 0.5, y1 - 0.5)))
    return shapes


def test_two_nets_that_must_cross_take_a_via_on_two_layers() -> None:
    tracks, vias = route_board(
        crossing(), BOARD, ["F.Cu", "B.Cu"], default("A", "B"), TUNE
    )
    assert {t.net for t in tracks} == {"A", "B"}
    assert len(vias) >= 2
    # Nothing of A comes within a track and a clearance of B on one layer.
    for a in (t for t in tracks if t.net == "A"):
        for b in (t for t in tracks if t.net == "B" and t.layer == a.layer):
            assert _apart(a, b) >= 0.25 + 0.2 - 1e-6
    # Nor any via near another net's copper, on any layer.
    from kairo.pcb.autoroute import RouteCase, audit

    case = RouteCase(
        tuple(crossing()),
        tuple(BOARD),
        ("F.Cu", "B.Cu"),
        tuple(default("A", "B")),
        frozenset(),
        (0.6, 0.3),
    )
    assert audit(case, tracks, vias) == {
        "shorts": 0,
        "clearance": 0,
        "edge": 0,
        "keepout": 0,
        "holes": 0,
    }


def test_on_one_layer_one_of_two_crossing_nets_is_left_unrouted() -> None:
    tracks, vias = route_board(crossing(), BOARD, ["F.Cu"], default("A", "B"), TUNE)
    # A short is never made: one net is routed, the other is a gap.
    assert len({t.net for t in tracks}) == 1 and vias == []


def test_a_high_voltage_pad_is_passed_at_its_class_clearance() -> None:
    hv = pad("HV", 10, 10.9, size=0.6)
    shapes = [pad("A", 3, 10), pad("A", 17, 10), hv]
    classes = [
        NetClass("hv", 1.0, 0.64, ("HV",)),
        NetClass("default", 0.25, 0.2, ("A",)),
    ]
    tracks, _ = route_board(shapes, BOARD, ["F.Cu"], classes, TUNE)
    assert tracks
    edge = 10.9 - 0.3
    for t in tracks:
        nearest = min(
            _point_to_box(p, (10 - 0.3, edge, 10 + 0.3, 10.9 + 0.3))
            for p in _samples(t)
        )
        assert nearest >= 0.64 + 0.125 - 0.13  # within half a grid cell


def _samples(t: object) -> list[tuple[float, float]]:
    start, end = t.start, t.end  # type: ignore[attr-defined]
    n = 50
    return [
        (start[0] + (end[0] - start[0]) * k / n, start[1] + (end[1] - start[1]) * k / n)
        for k in range(n + 1)
    ]


def _point_to_box(
    p: tuple[float, float], box: tuple[float, float, float, float]
) -> float:
    dx = max(box[0] - p[0], 0.0, p[0] - box[2])
    dy = max(box[1] - p[1], 0.0, p[1] - box[3])
    return math.hypot(dx, dy)


def _apart(a: object, b: object) -> float:
    return min(
        math.hypot(p[0] - q[0], p[1] - q[1]) for p in _samples(a) for q in _samples(b)
    )


def test_a_polygon_fills_by_rows_and_grows_by_a_disc() -> None:
    from kairo.pcb.autoroute.grid import Grid, _cells

    grid = Grid(0.0, 0.0, 1.0, 20, 20, ("F.Cu",))
    square = _Shape(
        "P", "F.Cu", polygon=((2.5, 2.5), (7.5, 2.5), (7.5, 7.5), (2.5, 7.5))
    )
    # Centres 3 to 7 in each direction: 25 cells.
    assert len(_cells(grid, square, 0.0)) == 25
    grown = set(_cells(grid, square, 1.0))
    assert (2, 5) in grown and (8, 5) in grown and (2, 2) not in grown


def test_tracks_keep_off_the_board_edge() -> None:
    # Two pads near the top edge: the track may not run along it.
    tracks, _ = route_board(
        [pad("A", 3, 1.2, size=0.6), pad("A", 17, 1.2, size=0.6)],
        BOARD,
        ["F.Cu"],
        default("A"),
        TUNE,
    )
    assert tracks
    for t in tracks:
        for _, y in _samples(t):
            assert y >= 0.5 + 0.125 - 0.13 or abs(y - 1.2) < 0.3  # within its pads


def test_a_pad_escapes_along_its_axis_necking_down_near_other_copper() -> None:
    from kairo.pcb.autoroute.grid import Grid, _escape

    grid = Grid(0.0, 0.0, 0.25, 80, 80, ("F.Cu",))

    # Another net's copper from x = 11.3, 0.2 mm of clearance: a half
    # track at x may be 11.3 - 0.2 - x wide, never more than 0.125.
    def room(x: float, y: float) -> float:
        return min(0.125, 11.3 - 0.2 - x)

    strip = _escape(grid, (9.0, 9.9, 11.0, 10.1), 1.0, room, 0.075)
    by_column = {i: half for i, _, half in strip}
    # From 8 mm (1 mm past the left end) to 11 mm; at 11.25 not even the
    # narrowest track fits.
    assert min(by_column) == 32 and max(by_column) == 44
    assert by_column[40] == 0.125
    assert by_column[44] == pytest.approx(0.1)  # necked down
    assert {j for _, j, _ in strip} == {40}


def test_a_routed_net_claims_its_diagonal_neighbours_and_its_vias_on_every_layer() -> (
    None
):
    from kairo.pcb.autoroute.grid import Grid, _footprint

    grid = Grid(0.0, 0.0, 0.25, 40, 40, ("F.Cu", "In2.Cu", "B.Cu"))
    klass = NetClass("default", 0.25, 0.2, ("A",))
    # A track along row 10 on F.Cu that drops by a via at column 20 to In2.
    run = [grid.index(i, 10, 0) for i in range(5, 21)] + [grid.index(20, 10, 1)]
    cells = _footprint(grid, [run], klass, 0.25, 0.6)
    # A diagonal neighbour 0.35 mm away is nearer than 0.45 mm: claimed.
    assert grid.index(6, 11, 0) in cells
    # Two rows away, 0.5 mm, is far enough.
    assert grid.index(6, 12, 0) not in cells
    # The via passes B.Cu too, and claims round it there.
    assert grid.index(20, 10, 2) in cells and grid.index(22, 10, 2) in cells


def test_a_new_via_beside_a_track_touches_its_net_though_the_track_claim_misses() -> (
    None
):
    from kairo.pcb.autoroute.grid import Grid, _footprint, _Net, _run_cells
    from kairo.pcb.autoroute.negotiate import _touching

    grid = Grid(0.0, 0.0, 0.25, 40, 40, ("F.Cu", "B.Cu"))
    klass = NetClass("default", 0.25, 0.2, ("A", "C"))
    # C: a 45 degree track on B.Cu along i + j = 30.
    track = [grid.index(i, 30 - i, 1) for i in range(5, 26)]
    # A: a track on F.Cu that drops by a via at (17, 16), on i + j = 33:
    # 2.12 cells (0.53 mm) from C's centreline, as on the BMS.
    trial = [grid.index(i, 16, 0) for i in range(30, 16, -1)] + [grid.index(17, 16, 1)]
    cells = _run_cells(grid, trial)
    c_claim = _footprint(grid, [track], klass, 0.25, 0.6)
    # C's track claims 1.8 cells: A's via centre lies outside it.
    assert not c_claim & cells
    claim = _footprint(grid, [trial], klass, 0.25, 0.6)
    c = _Net("C", 0)
    assert _touching(grid, cells, claim, [c], {"C": [track]}, {"C": c_claim}) == [c]


def test_a_track_leaves_a_pad_on_its_true_axis_not_the_grid() -> None:
    # Pads centred 0.1 mm off the 0.25 mm grid, as fine-pitch pins are.
    tracks, _ = route_board(
        [pad("A", 3, 10.1, size=0.4), pad("A", 17, 10.1, size=0.4)],
        BOARD,
        ["F.Cu"],
        default("A"),
        TUNE,
    )
    assert tracks
    ends = [p for t in tracks for p in (t.start, t.end)]
    assert (3.0, 10.1) in ends and (17.0, 10.1) in ends


def test_completion_finds_the_way_round_that_negotiation_dropped() -> None:
    from kairo.pcb.autoroute import RouteCase, audit

    # As the crossing, but B's pads stand clear of the edges: A can pass
    # above B's top pad. Negotiation drops one of the two; completion,
    # with B's route a wall, finds A's way round.
    shapes = [pad("A", 2, 10), pad("A", 18, 10), pad("B", 10, 2), pad("B", 10, 18)]
    for x0, y0, x1, y1 in (
        (0, 0, 8, 8),
        (12, 0, 20, 8),
        (0, 12, 8, 20),
        (12, 12, 20, 20),
    ):
        shapes.append(_Shape("", "*", box=(x0 + 0.5, y0 + 0.5, x1 - 0.5, y1 - 0.5)))
    stats: dict[str, float] = {}
    tracks, vias = route_board(
        shapes, BOARD, ["F.Cu"], default("A", "B"), TUNE, stats=stats
    )
    assert {t.net for t in tracks} == {"A", "B"} and stats["completed"] >= 1
    case = RouteCase(
        tuple(shapes),
        tuple(BOARD),
        ("F.Cu",),
        tuple(default("A", "B")),
        frozenset(),
        (0.6, 0.3),
    )
    assert audit(case, tracks, vias) == {
        "shorts": 0,
        "clearance": 0,
        "edge": 0,
        "keepout": 0,
        "holes": 0,
    }


def test_a_track_too_near_a_pad_is_necked_down_to_fit() -> None:
    from kairo.pcb.autoroute.copper import _legalize
    from kairo.pcb.board_writer import Track

    # A 0.25 mm track 0.3 mm from another net's pad edge at its centre:
    # 0.175 mm of copper gap, 0.2 mm needed. 0.2 mm wide fits: 0.2 mm gap.
    other = pad("B", 10, 10.8, size=1.0)  # its edge at y = 10.3
    track = Track((5.0, 10.0), (15.0, 10.0), 0.25, "F.Cu", "A")
    out, necked = _legalize([track], [], [other], lambda a, b: 0.2, 0.0, 0.15)
    assert necked == 1 and out[0].width == pytest.approx(0.2)
    # One that has room is left as it is, and one that cannot fit even
    # the maker's least track is left for the audit.
    clear = Track((5.0, 5.0), (15.0, 5.0), 0.25, "F.Cu", "A")
    tight = Track((5.0, 10.25), (15.0, 10.25), 0.25, "F.Cu", "A")
    out, necked = _legalize([clear, tight], [], [other], lambda a, b: 0.2, 0.0, 0.15)
    assert necked == 0 and [t.width for t in out] == [0.25, 0.25]


def test_rescue_moves_a_route_aside_for_one_that_was_dropped() -> None:
    from kairo.pcb.autoroute import RouteCase, audit

    # One layer. B's pads stand against the edges; A's leave a way round
    # their ends. Negotiation and completion leave B dropped behind A's
    # straight route; the rescue lays B round A's left pad and keeps A.
    shapes = [pad("A", 2, 10), pad("A", 18, 10), pad("B", 10, 1), pad("B", 10, 19)]
    for x0, y0, x1, y1 in (
        (0, 0, 8, 8),
        (12, 0, 20, 8),
        (0, 12, 8, 20),
        (12, 12, 20, 20),
    ):
        shapes.append(_Shape("", "*", box=(x0 + 0.5, y0 + 0.5, x1 - 0.5, y1 - 0.5)))
    stats: dict[str, object] = {}
    tracks, vias = route_board(
        shapes, BOARD, ["F.Cu"], default("A", "B"), TUNE, stats=stats
    )
    assert {t.net for t in tracks} == {"A", "B"} and stats["rescued"] == 1
    case = RouteCase(
        tuple(shapes),
        tuple(BOARD),
        ("F.Cu",),
        tuple(default("A", "B")),
        frozenset(),
        (0.6, 0.3),
    )
    assert audit(case, tracks, vias) == {
        "shorts": 0,
        "clearance": 0,
        "edge": 0,
        "keepout": 0,
        "holes": 0,
    }


def _qfn_corner() -> list[_Shape]:
    """The BQ24075's corner on the mixed-signal board, moved by (-62, -34).

    A ground exposed pad, a ground pin 0.2 mm below it, another net's pin
    beside the gap between them (no track fits through that gap) and a
    third net's pin on the ground pin's other side.
    """
    return [
        _Shape("GND", "F.Cu", box=(10.2, 10.7, 11.8, 12.3)),
        _Shape("GND", "F.Cu", box=(11.625, 12.5, 11.875, 13.375)),
        _Shape("X", "F.Cu", box=(12.0, 12.125, 12.875, 12.375)),
        _Shape("X", "F.Cu", box=(16.0, 12.125, 16.875, 12.375)),
        _Shape("Y", "F.Cu", box=(11.125, 12.5, 11.375, 13.375)),
    ]


def test_a_track_never_bridges_two_pads_past_another_nets_pin() -> None:
    from kairo.pcb.autoroute import RouteCase, audit

    shapes = _qfn_corner()
    classes = default("GND", "X", "Y")
    tracks, vias = route_board(shapes, BOARD, ["F.Cu", "B.Cu"], classes, TUNE)
    case = RouteCase(
        tuple(shapes),
        tuple(BOARD),
        ("F.Cu", "B.Cu"),
        tuple(classes),
        frozenset(),
        (0.6, 0.3),
    )
    found = audit(case, tracks, vias)
    assert found["clearance"] == 0 and found["shorts"] == 0


def test_a_diagonal_that_grazes_a_pins_corner_is_barred_to_other_nets() -> None:
    from kairo.pcb.autoroute.grid import Grid, _foes, _grazes, _Near

    # The BQ24075's top row, moved as `_qfn_corner`: a ground pin above the
    # ground exposed pad, between two other nets' pins. The 45 degree step
    # from the pad's cell (11.0, 10.75) up into the pin's (10.75, 10.5)
    # passes T's corner at 0.265 mm from its centreline, though both cells
    # are clear at their centres: no track, even necked to 0.15 mm, fits.
    shapes = [
        _Shape("GND", "F.Cu", box=(10.2, 10.7, 11.8, 12.3)),
        _Shape("GND", "F.Cu", box=(10.625, 9.625, 10.875, 10.5)),
        _Shape("T", "F.Cu", box=(11.125, 9.625, 11.375, 10.5)),
        _Shape("I", "F.Cu", box=(10.125, 9.625, 10.375, 10.5)),
    ]
    grid = Grid(0.0, 0.0, 0.25, 81, 81, ("F.Cu", "B.Cu"))
    classes = default("GND", "T", "I")
    net_id = {"GND": 1, "T": 2, "I": 3}
    class_of = dict.fromkeys(net_id, 0)
    foes = _foes(grid, shapes, classes, 0, net_id, class_of, (0.6, 0.3))
    # Every cell open, as a pad's own cells are: the geometry alone decides.
    everywhere = set(range(81 * 81 * 2))
    barred = _grazes(
        grid, foes, classes, 0, _Near(shapes), net_id, class_of, 0.15, everywhere, {}
    )
    # From (11.0, 10.75), cell 43 * 81 + 44, the move (-1, -1) is move 5.
    near = barred[(43 * 81 + 44) * 8 + 5]
    # Barred to ground: T's copper is too near, whatever ground's own is.
    assert net_id["T"] in near and any(n != net_id["GND"] for n in near)
    # Inside the exposed pad, a step grazes only ground's own copper.
    assert barred[(45 * 81 + 44) * 8 + 5] == (net_id["GND"],)


def test_a_run_still_at_fault_after_legalising_is_dropped_not_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    import kairo.pcb.autoroute.board as board
    from kairo.pcb.autoroute import RouteCase, audit
    from kairo.pcb.board_writer import Track

    # Two nets side by side, 1.5 mm apart. Every track drawn 1.5 mm wide
    # stands for whatever a rule failed to keep apart: the exact audit
    # after legalising must drop a run rather than return the fault.
    def too_wide(tracks: list[Track], *_: object) -> tuple[list[Track], int]:
        return [replace(t, width=1.5) for t in tracks], 0

    monkeypatch.setattr(board, "_legalize", too_wide)
    shapes = [pad("A", 3, 9), pad("A", 17, 9), pad("B", 3, 10.5), pad("B", 17, 10.5)]
    classes = default("A", "B")
    stats: dict[str, object] = {}
    tracks, vias = route_board(shapes, BOARD, ["F.Cu"], classes, TUNE, stats=stats)
    case = RouteCase(
        tuple(shapes), tuple(BOARD), ("F.Cu",), tuple(classes), frozenset(), (0.6, 0.3)
    )
    found = audit(case, tracks, vias)
    assert found["shorts"] == 0 and found["clearance"] == 0
    # Joined again after each drop, it clashes again: dropped three times,
    # the last time for good.
    assert stats["faulted"] == 3 and stats["routed"] == 1


def test_a_track_keeps_a_pairs_voltage_spacing_from_another_nets_pad() -> None:
    from kairo.pcb.autoroute import RouteCase, audit
    from kairo.pcb.geometry import segment_box

    # A runs from 3 to 17 along y = 10; B's pad, 50 V from A, lies 0.4 mm
    # above that line, closer than the 0.64 mm their pair needs (though
    # farther than either class's own 0.2 mm): A must go round it.
    b_pad = (9.5, 10.4, 10.5, 11.4)
    shapes = [pad("A", 3, 10), pad("A", 17, 10), _Shape("B", "F.Cu", box=b_pad)]
    classes = [
        NetClass("low", 0.25, 0.2, ("A",), (("high", 0.64),)),
        NetClass("high", 0.25, 0.2, ("B",)),
    ]
    tracks, vias = route_board(shapes, BOARD, ["F.Cu"], classes, TUNE)
    assert tracks
    nearest = min(segment_box(t.start, t.end, b_pad) - t.width / 2 for t in tracks)
    assert nearest >= 0.64 - 1e-6
    case = RouteCase(
        tuple(shapes), tuple(BOARD), ("F.Cu",), tuple(classes), frozenset(), (0.6, 0.3)
    )
    assert audit(case, tracks, vias)["clearance"] == 0


def test_two_routed_nets_far_apart_in_voltage_never_share_a_narrow_channel() -> None:
    # A channel 1.3 mm wide (y 8.6 to 9.9) between walls from x = 4 to 16;
    # A and H must both cross it. Two tracks fit at 0.2 mm but not at the
    # 0.64 mm A and H need: the router itself routes one and leaves the
    # other, rather than laying both for the last check to drop.
    walls = [
        _Shape("", "*", box=(4.0, 0.5, 16.0, 8.6)),
        _Shape("", "*", box=(4.0, 9.9, 16.0, 19.5)),
    ]
    shapes = [
        pad("A", 2, 9, size=0.4),
        pad("A", 18, 9, size=0.4),
        pad("H", 2, 11, size=0.4),
        pad("H", 18, 11, size=0.4),
        *walls,
    ]
    near = [NetClass("low", 0.25, 0.2, ("A",)), NetClass("high", 0.25, 0.2, ("H",))]
    stats: dict[str, object] = {}
    route_board(shapes, BOARD, ["F.Cu"], near, TUNE, stats=stats)
    assert stats["routed"] == 2
    far = [
        NetClass("low", 0.25, 0.2, ("A",), (("high", 0.64),)),
        NetClass("high", 0.25, 0.2, ("H",)),
    ]
    stats = {}
    route_board(shapes, BOARD, ["F.Cu"], far, TUNE, stats=stats)
    assert stats["routed"] == 1 and stats["faulted"] == 0


def test_a_net_routed_first_keeps_its_layer_and_the_rest_route_round_it() -> None:
    from kairo.pcb.autoroute import RouteCase, focus, route_case

    case = RouteCase(
        tuple(crossing()),
        tuple(BOARD),
        ("F.Cu", "B.Cu"),
        tuple(default("A", "B")),
        frozenset(),
        (0.6, 0.3),
    )
    first, drilled = route_case(focus(case, {"B"}, ["F.Cu"]), TUNE)
    # B alone, on the one layer it was given: straight down the middle.
    assert {t.net for t in first} == {"B"} and drilled == []
    assert {t.layer for t in first} == {"F.Cu"}
    held = tuple(
        _Shape(t.net, t.layer, start=t.start, end=t.end, radius=t.width / 2)
        for t in first
    )
    rest, vias = route_case(
        RouteCase(
            case.shapes + held,
            case.outline,
            case.layers,
            case.classes,
            case.skip,
            case.via,
        ),
        TUNE,
    )
    # A crosses it by changing layer; B is not touched again.
    assert {t.net for t in rest} == {"A"} and len(vias) >= 2
