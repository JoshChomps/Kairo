"""The compiled pricing arranges a block exactly as the Python pricing does."""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

from kairo.kicad import arrange
from kairo.kicad.arrange import Piece, PinEnd, Shape, arrange_block, wire_cost

pytest.importorskip("kairo._fast", reason="the compiled pricing is not built here")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_arrange import two_pin


def chip(ref: str, nets: list[str]) -> Piece:
    """A chip with pins on its left and right, in one shape."""
    pins: list[PinEnd] = []
    for n, net in enumerate(nets):
        left = n % 2 == 0
        pins.append(
            PinEnd(net, (-3 if left else 3, 2 * (n // 2) - 4), (-1 if left else 1, 0))
        )
    return Piece(ref, (Shape(0, (-2, -5, 2, 5), tuple(pins)),))


def block(seed: int) -> list[Piece]:
    """Random parts on random nets: chains, a shared net, grounds, supplies."""
    rng = random.Random(seed)
    nets = [f"N{k}" for k in range(8)]
    pieces = [chip("U1", rng.sample(nets, 8))]
    for n in range(14):
        a, b = rng.sample([*nets, "GND", "VCC"], 2)
        pieces.append(two_pin(f"R{n}", a, b, ground=b == "GND", supply=a == "VCC"))
    return pieces


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_the_compiled_pricing_arranges_a_block_as_the_python_does(
    seed: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    pieces = block(seed)
    fast = arrange_block(pieces, effort=0.2)
    fast_cost = wire_cost(pieces, fast.spots)
    monkeypatch.setattr(arrange, "_FAST", None)
    slow = arrange_block(pieces, effort=0.2)
    assert fast == slow
    assert fast_cost == wire_cost(pieces, slow.spots)
