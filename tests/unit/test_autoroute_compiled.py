"""The compiled router loops find exactly what the Python ones find."""

# The router's copper shapes are built directly here.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import random
from typing import Any

import pytest

from kairo.pcb.autoroute import Settings, route_board
from kairo.pcb.autoroute import board as board_module
from kairo.pcb.autoroute import grid as grid_module
from kairo.pcb.autoroute.grid import _Shape
from kairo.pcb.specctra import NetClass

pytest.importorskip("kairo._fast", reason="the compiled search is not built here")

BOARD = [(0.0, 0.0), (24.0, 0.0), (24.0, 24.0), (0.0, 24.0)]
TUNE = Settings(pitch=0.25, iterations=6, margin=4.0)


def board(seed: int) -> tuple[list[_Shape], list[NetClass]]:
    """Random pads on two layers, with every branch of the search in use.

    A wide class (its own ring of cells), a pair that must keep a voltage's
    spacing (the far claims), through pads (vias' hole spacing) and a row
    of fine-pitch pads (grazing diagonals).
    """
    rng = random.Random(seed)
    shapes: list[_Shape] = []
    names = [f"N{k}" for k in range(10)]
    for name in names:
        for _ in range(rng.choice((2, 2, 3))):
            x, y = rng.uniform(2, 22), rng.uniform(2, 22)
            layer = rng.choice(("F.Cu", "B.Cu", "*"))
            h = rng.choice((0.3, 0.5))
            shapes.append(_Shape(name, layer, box=(x - h, y - h, x + h, y + h)))
    for k in range(6):
        x = 8.0 + 0.5 * k
        net = names[k % len(names)]
        shapes.append(_Shape(net, "F.Cu", box=(x - 0.12, 11.6, x + 0.12, 12.4)))
    classes = [
        NetClass("power", 0.6, 0.3, tuple(names[:2]), (("signal", 0.64),)),
        NetClass("signal", 0.25, 0.2, tuple(names[2:7])),
        NetClass("default", 0.25, 0.2, tuple(names[7:])),
    ]
    return shapes, classes


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_the_compiled_search_routes_a_board_as_the_python_search_does(
    seed: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    shapes, classes = board(seed)
    layers = ["F.Cu", "B.Cu"]
    # The grazing steps barred are compared too: a step barred wrongly
    # need not change a route on a small board.
    tables: list[list[dict[int, tuple[int, ...]]]] = []
    real = board_module._grazes_all

    def kept(*args: Any) -> list[dict[int, tuple[int, ...]]]:
        found = real(*args)
        tables.append(found)
        return found

    monkeypatch.setattr(board_module, "_grazes_all", kept)
    fast_stats: dict[str, object] = {}
    fast = route_board(shapes, BOARD, layers, classes, TUNE, stats=fast_stats)
    monkeypatch.setattr(grid_module, "_FAST", None)
    slow_stats: dict[str, object] = {}
    slow = route_board(shapes, BOARD, layers, classes, TUNE, stats=slow_stats)
    assert fast_stats["routed"], "the board must route something to compare"
    assert sum(len(t) for t in tables[0]) > 1000, "the board must graze to compare"
    assert tables[0] == tables[1]
    assert fast == slow
    times = {"setup_s", "negotiate_s", "legalize_s"}
    assert {k: v for k, v in fast_stats.items() if k not in times} == {
        k: v for k, v in slow_stats.items() if k not in times
    }
