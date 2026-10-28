"""The router that draws wires for a person to read.

Distances are in grid steps of 1.27 mm; a pin at (10, 10) is at
(12.7 mm, 12.7 mm).
"""

from __future__ import annotations

from kairo.kicad.geometry import GRID, Box
from kairo.kicad.routing import Board, Group, Terminal, board_for, route_groups


def at(i: int, j: int) -> tuple[float, float]:
    return (round(i * GRID, 4), round(j * GRID, 4))


def board() -> Board:
    return board_for(Box(0.0, 0.0, 60 * GRID, 60 * GRID))


def pin(key: str, i: int, j: int, out: tuple[int, int]) -> Terminal:
    return Terminal(key, at(i, j), out)


def placed(b: Board, net: str, *terminals: Terminal) -> None:
    for t in terminals:
        b.keep(net, [t.at])


def points_of(
    segment: tuple[tuple[float, float], tuple[float, float]],
) -> set[tuple[int, int]]:
    (x0, y0), (x1, y1) = segment
    a = (round(x0 / GRID), round(y0 / GRID))
    b = (round(x1 / GRID), round(y1 / GRID))
    if a[1] == b[1]:
        return {(i, a[1]) for i in range(min(a[0], b[0]), max(a[0], b[0]) + 1)}
    return {(a[0], j) for j in range(min(a[1], b[1]), max(a[1], b[1]) + 1)}


def cover(
    segments: tuple[tuple[tuple[float, float], tuple[float, float]], ...],
) -> set[tuple[int, int]]:
    found: set[tuple[int, int]] = set()
    for segment in segments:
        found |= points_of(segment)
    return found


def test_two_pins_facing_each_other_are_joined_by_one_wire() -> None:
    b = board()
    left, right = pin("R1.2", 10, 20, (1, 0)), pin("R2.1", 20, 20, (-1, 0))
    placed(b, "A", left, right)
    (route,) = route_groups(b, [Group("A", (left, right))]).values()
    assert route.segments == ((at(20, 20), at(10, 20)),)
    assert route.junctions == ()


def test_three_pins_make_a_tree_with_one_junction() -> None:
    b = board()
    pins = (
        pin("U1.1", 10, 20, (1, 0)),
        pin("R1.1", 30, 20, (-1, 0)),
        pin("C1.1", 20, 30, (0, -1)),
    )
    placed(b, "A", *pins)
    (route,) = route_groups(b, [Group("A", pins)]).values()
    covered = cover(route.segments)
    assert {(10, 20), (30, 20), (20, 30)} <= covered
    assert len(route.junctions) == 1


def test_a_wire_goes_round_a_body_and_never_through_it() -> None:
    b = board()
    left, right = pin("R1.2", 10, 20, (1, 0)), pin("R2.1", 30, 20, (-1, 0))
    placed(b, "A", left, right)
    b.block(Box(15 * GRID, 15 * GRID, 25 * GRID, 25 * GRID))
    (route,) = route_groups(b, [Group("A", (left, right))]).values()
    covered = cover(route.segments)
    assert not covered & b.blocked
    assert len(route.segments) > 1


def test_another_nets_wire_is_crossed_straight_and_never_touched() -> None:
    b = board()
    # Net B runs down column 20 from row 5 to row 35.
    b.keep("B", [at(20, 5), at(20, 35)])
    left, right = pin("R1.2", 10, 20, (1, 0)), pin("R2.1", 30, 20, (-1, 0))
    placed(b, "A", left, right)
    (route,) = route_groups(b, [Group("A", (left, right))]).values()
    corners = {
        (round(x / GRID), round(y / GRID)) for s in route.segments for (x, y) in s
    }
    # No end or bend of A on B's wire; only a straight crossing.
    assert not any(c[0] == 20 and 5 <= c[1] <= 35 for c in corners)
    covered = cover(route.segments)
    assert (20, 20) in covered


def test_a_pin_no_wire_can_reach_is_left_to_labels() -> None:
    b = board()
    left, right = pin("R1.2", 10, 20, (1, 0)), pin("R2.1", 30, 20, (-1, 0))
    placed(b, "A", left, right)
    # Wall the second pin in on every side but its body's.
    b.block(Box(26 * GRID, 15 * GRID, 29 * GRID, 25 * GRID))
    routes = route_groups(b, [Group("A", (left, right))])
    assert routes == {}


def test_a_routed_wire_is_kept_out_of_by_the_next_net() -> None:
    b = board()
    a1, a2 = pin("R1.2", 10, 20, (1, 0)), pin("R2.1", 30, 20, (-1, 0))
    c1, c2 = pin("R3.2", 10, 25, (1, 0)), pin("R4.1", 30, 25, (-1, 0))
    placed(b, "A", a1, a2)
    placed(b, "C", c1, c2)
    routes = route_groups(b, [Group("A", (a1, a2)), Group("C", (c1, c2))])
    covered = [cover(r.segments) for r in routes.values()]
    assert not covered[0] & covered[1]


def test_freeing_a_stub_frees_every_point_along_it() -> None:
    # A pin's stub runs two grid steps out of its part's box; freeing only
    # its ends left the middle point blocked, and the pin boxed in.
    b = board()
    b.block(Box(5 * GRID, 5 * GRID, 12 * GRID, 12 * GRID))
    b.free([at(10, 8), at(12, 8)])
    assert (11, 8) not in b.blocked
    assert (10, 8) not in b.blocked and (12, 8) not in b.blocked
