"""How well a routed board is drawn, beyond whether KiCad's DRC passes it.

A board can be electrically right and still poorly routed. Designers judge
copper by a few plain measures, and this module counts them from the board
file itself:

- each net's track length and via count: shorter, with fewer vias, is
  better, all else equal;
- every bend where two tracks of a net meet end to end, by the angle the
  track turns through: 45 degrees is how a designer bends a track; 90
  degrees concentrates current and field at the outer corner and is
  avoided where a 45 will do; a turn sharper than 90 (an acute angle)
  traps etchant and is a fault (IPC-2221B section 6.3 recommends avoiding
  sharp corners; it is common board practice for acid traps);
- a bend count per millimetre, so a zig-zag that a straight or diagonal
  run would replace shows up even where each bend is 45 degrees.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from kairo.kicad.sexpr import SList, parse


@dataclass(frozen=True, slots=True)
class NetRouting:
    """One net's routed copper.

    Attributes:
        length: Its track length, in mm.
        vias: Its vias.
        bends: Its bends, by the angle turned through: 45, 90, or ``acute``
            for sharper than 90; other angles are counted under their own
            rounded value.
    """

    length: float
    vias: int
    bends: dict[str, int] = field(default_factory=dict[str, int])


@dataclass(frozen=True, slots=True)
class Evaluation:
    """A routed board's measures.

    Attributes:
        nets: Each routed net's measures.
        length: All tracks' length, in mm.
        vias: All vias.
        bends: All bends, by angle.
        bends_per_m: Bends per metre of track: lower reads as cleaner.
    """

    nets: dict[str, NetRouting]
    length: float
    vias: int
    bends: dict[str, int]
    bends_per_m: float


def evaluate(board_text: str) -> Evaluation:
    """Measure a board file's routed copper."""
    root = parse(board_text).root
    tracks: list[tuple[str, str, tuple[float, float], tuple[float, float]]] = []
    for node in root.lists("segment"):
        start, end = node.first("start"), node.first("end")
        net, layer = node.first("net"), node.first("layer")
        if start is None or end is None or net is None or layer is None:
            continue
        a = (float(start.values()[0]), float(start.values()[1]))
        b = (float(end.values()[0]), float(end.values()[1]))
        tracks.append((_text(net), _text(layer), a, b))
    vias = [_text(n) for v in root.lists("via") if (n := v.first("net")) is not None]
    return measure(tracks, vias)


def measure(
    tracks: Sequence[tuple[str, str, tuple[float, float], tuple[float, float]]],
    vias: Sequence[str],
) -> Evaluation:
    """Measure routed copper given as tracks (net, layer, start, end) and vias' nets.

    The measure a board file gets from `evaluate`, for copper not yet
    written (a router's output in a benchmark).
    """
    segments: dict[str, list[tuple[tuple[float, float], tuple[float, float], str]]] = {}
    for net, layer, start, end in tracks:
        a = (round(start[0], 3), round(start[1], 3))
        b = (round(end[0], 3), round(end[1], 3))
        segments.setdefault(net, []).append((a, b, layer))
    vias_by: Counter[str] = Counter(vias)
    nets: dict[str, NetRouting] = {}
    for net, runs in segments.items():
        length = sum(math.dist(a, b) for a, b, _ in runs)
        nets[net] = NetRouting(round(length, 3), vias_by.get(net, 0), _bends(runs))
    for net, count in vias_by.items():
        if net not in nets:
            nets[net] = NetRouting(0.0, count)
    total = sum(n.length for n in nets.values())
    bends: Counter[str] = Counter()
    for n in nets.values():
        bends.update(n.bends)
    count = sum(bends.values())
    return Evaluation(
        nets,
        round(total, 1),
        sum(vias_by.values()),
        dict(bends),
        round(count / (total / 1000), 1) if total else 0.0,
    )


def _bends(
    runs: list[tuple[tuple[float, float], tuple[float, float], str]],
) -> dict[str, int]:
    """Each point where exactly two tracks of a net meet on one layer, by turn."""
    meeting: dict[tuple[tuple[float, float], str], list[tuple[float, float]]] = {}
    for a, b, layer in runs:
        meeting.setdefault((a, layer), []).append((b[0] - a[0], b[1] - a[1]))
        meeting.setdefault((b, layer), []).append((a[0] - b[0], a[1] - b[1]))
    found: Counter[str] = Counter()
    for directions in meeting.values():
        if len(directions) != 2:
            continue
        (x1, y1), (x2, y2) = directions
        # The angle between the two tracks leaving the point: 180 is straight
        # on; the turn is what is left of 180.
        between = math.degrees(
            math.acos(
                max(
                    -1.0,
                    min(
                        1.0,
                        (x1 * x2 + y1 * y2) / (math.hypot(x1, y1) * math.hypot(x2, y2)),
                    ),
                )
            )
        )
        turn = round(180 - between)
        if turn <= 1:
            continue
        if abs(turn - 45) <= 2:
            found["45"] += 1
        elif abs(turn - 90) <= 2:
            found["90"] += 1
        elif turn > 92:
            found["acute"] += 1
        else:
            found[str(turn)] += 1
    return dict(found)


def _text(node: SList) -> str:
    return str(node.values()[0])
