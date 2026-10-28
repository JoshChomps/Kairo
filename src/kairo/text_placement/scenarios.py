"""Synthetic boards and sheets for testing and benchmarking the text placer.

Each family imitates a layout that is hard for labels in a different way:
tight rows of small passives, a ring of parts around a large chip, parts
crowded against an edge, a random packed board, and a schematic sheet where
every symbol carries both a reference and a value.

Text size here is a stand-in: width is taken as 0.6 times the font height
per character. That is good enough to make the problems realistic for
comparing strategies. Kairo itself must not use it for real drawings; there,
text extents come from calibration against KiCad's own output.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable

from kairo.text_placement.geometry import Box
from kairo.text_placement.problem import Label, Part, Problem, Rules, Side

PCB_RULES = Rules(
    gap=0.20, clearance=0.15, step=0.25, pushes=(0.0, 0.5, 1.0), max_distance=2.0
)
"""Rules for silkscreen-style problems, in mm."""

SCHEMATIC_RULES = Rules(
    gap=0.50, clearance=0.50, step=1.27, pushes=(0.0, 1.27, 2.54), max_distance=5.08
)
"""Rules for schematic-style problems, on KiCad's 1.27 mm grid."""

_CHAR_WIDTH = 0.6
"""Stand-in text width per character, as a fraction of the font height."""


def _label(owner: str, text: str, font: float, preferred: Side = Side.N) -> Label:
    return Label(
        owner=owner,
        text=text,
        width=_CHAR_WIDTH * font * len(text),
        height=font,
        preferred=preferred,
    )


def _place_without_overlap(
    rng: random.Random,
    area: Box,
    sizes: list[tuple[float, float]],
    spacing: float,
    existing: list[Box],
    tries: int = 400,
) -> list[Box]:
    """Drop boxes at random spots that keep ``spacing`` from everything placed."""
    placed: list[Box] = []
    for w, h in sizes:
        for _ in range(tries):
            cx = rng.uniform(area.x0 + w / 2 + spacing, area.x1 - w / 2 - spacing)
            cy = rng.uniform(area.y0 + h / 2 + spacing, area.y1 - h / 2 - spacing)
            box = Box.centred(cx, cy, w, h)
            if all(box.gap(b) >= spacing for b in (*existing, *placed)):
                placed.append(box)
                break
    return placed


def passive_rows(seed: int) -> Problem:
    """Rows of 0603-sized resistors and capacitors at tight pitch.

    Args:
        seed: Makes the jitter in pitch reproducible.

    Returns:
        The problem.
    """
    rng = random.Random(seed)
    area = Box(0.0, 0.0, 30.0, 20.0)
    parts: list[Part] = []
    labels: list[Label] = []
    y = 2.0
    n = 0
    while y < area.y1 - 2.0:
        x = 2.0
        while x < area.x1 - 2.0:
            n += 1
            ref = f"{'R' if n % 2 else 'C'}{n}"
            parts.append(Part(ref, Box.centred(x, y, 1.6, 0.8)))
            labels.append(_label(ref, ref, 0.8))
            x += rng.uniform(2.0, 2.5)
        y += rng.uniform(1.6, 2.1)
    return Problem(tuple(parts), tuple(labels), area, PCB_RULES)


def mcu_module(seed: int) -> Problem:
    """A 7 mm chip in the middle with decoupling and support parts around it.

    Args:
        seed: Makes the placement of the surrounding parts reproducible.

    Returns:
        The problem.
    """
    rng = random.Random(seed)
    area = Box(0.0, 0.0, 24.0, 24.0)
    chip = Box.centred(12.0, 12.0, 7.0, 7.0)
    parts = [Part("U1", chip)]
    sizes = [(1.6, 0.8)] * 34 + [(2.0, 1.25)] * 8 + [(3.2, 1.6)] * 4
    ring = Box(chip.x0 - 4.0, chip.y0 - 4.0, chip.x1 + 4.0, chip.y1 + 4.0)
    boxes = _place_without_overlap(rng, ring, sizes, 0.45, [chip])
    for k, box in enumerate(boxes, start=1):
        ref = f"{'C' if k % 3 else 'R'}{k}"
        parts.append(Part(ref, box))
    labels = [_label(p.ref, p.ref, 0.8) for p in parts]
    return Problem(tuple(parts), tuple(labels), area, PCB_RULES)


def connector_edge(seed: int) -> Problem:
    """A long connector on the board edge with protection parts crowded above it.

    Args:
        seed: Makes the row of parts reproducible.

    Returns:
        The problem.
    """
    rng = random.Random(seed)
    area = Box(0.0, 0.0, 30.0, 12.0)
    connector = Box(4.0, 0.3, 26.0, 3.3)
    parts = [Part("J1", connector)]
    # Walk a left edge along the row so neighbours of any width keep their gap.
    left = 4.0
    k = 0
    while True:
        w, h = rng.choice([(1.6, 0.8), (1.0, 0.5), (2.9, 1.3)])
        if left + w > connector.x1:
            break
        k += 1
        box = Box(left, 4.6 - h / 2, left + w, 4.6 + h / 2)
        parts.append(Part(f"D{k}" if k % 2 else f"R{k}", box))
        left += w + rng.uniform(0.3, 0.6)
    upper = Box(2.0, 6.0, 28.0, 11.5)
    for k2, box in enumerate(
        _place_without_overlap(rng, upper, [(1.6, 0.8)] * 22, 0.45, []), 1
    ):
        parts.append(Part(f"C{k2}", box))
    labels = [_label(p.ref, p.ref, 0.8) for p in parts]
    return Problem(tuple(parts), tuple(labels), area, PCB_RULES)


def random_packed(seed: int) -> Problem:
    """Mixed part sizes dropped at random with little room between them.

    Args:
        seed: Makes the board reproducible.

    Returns:
        The problem.
    """
    rng = random.Random(seed)
    area = Box(0.0, 0.0, 30.0, 30.0)
    kinds = [(1.0, 0.5), (1.6, 0.8), (2.0, 1.25), (3.0, 3.0), (5.0, 5.0)]
    sizes = [rng.choice(kinds) for _ in range(90)]
    boxes = _place_without_overlap(rng, area, sizes, 0.35, [])
    parts = [
        Part(f"{'U' if b.width >= 3 else 'R'}{k}", b) for k, b in enumerate(boxes, 1)
    ]
    labels = [_label(p.ref, p.ref, 0.8) for p in parts]
    return Problem(tuple(parts), tuple(labels), area, PCB_RULES)


def schematic_sheet(seed: int) -> Problem:
    """Schematic symbols on a grid, each with a reference above and a value below.

    Args:
        seed: Makes the symbol sizes, values and spacing reproducible.

    Returns:
        The problem.
    """
    rng = random.Random(seed)
    area = Box(0.0, 0.0, 180.0, 120.0)
    kinds = [
        ("R", (2.54, 7.62), ["10k", "4k7", "100R", "1M"]),
        ("C", (2.54, 5.08), ["100n", "10u 16V X5R", "22u 10V X5R", "1n"]),
        ("U", (12.7, 15.24), ["AMS1117-3.3", "STM32F103C8T6", "TPS62160"]),
        ("D", (5.08, 2.54), ["LED", "1N4148", "SMBJ5.0A"]),
    ]
    parts: list[Part] = []
    labels: list[Label] = []
    counts: dict[str, int] = {}
    grid = 1.27
    margin = 10.16
    bottom = margin
    while True:
        # Choose a row of symbols, then centre them all on one line. Edges
        # are walked (not centres) so symbols of any size keep their gap,
        # and positions only ever round up to the grid, which cannot close a
        # gap.
        row: list[tuple[str, tuple[float, float], list[str], float]] = []
        left = margin
        while True:
            prefix, (w, h), values = rng.choice(kinds)
            left = math.ceil(left / grid) * grid
            if left + w > area.x1 - margin:
                break
            row.append((prefix, (w, h), values, left))
            left += w + rng.choice([2.54, 5.08, 7.62])
        row_height = max(h for _, (_, h), _, _ in row)
        centre = math.ceil((bottom + row_height / 2) / grid) * grid
        if centre + row_height / 2 > area.y1 - margin:
            break
        for prefix, (w, h), values, x0 in row:
            counts[prefix] = counts.get(prefix, 0) + 1
            ref = f"{prefix}{counts[prefix]}"
            parts.append(Part(ref, Box(x0, centre - h / 2, x0 + w, centre + h / 2)))
            labels.append(_label(ref, ref, 1.27, Side.N))
            labels.append(_label(ref, rng.choice(values), 1.27, Side.S))
        bottom = centre + row_height / 2 + rng.choice([3.81, 5.08, 7.62])
    return Problem(tuple(parts), tuple(labels), area, SCHEMATIC_RULES)


FAMILIES: dict[str, Callable[[int], Problem]] = {
    "passive_rows": passive_rows,
    "mcu_module": mcu_module,
    "connector_edge": connector_edge,
    "random_packed": random_packed,
    "schematic_sheet": schematic_sheet,
}
"""Every scenario family, by name."""
