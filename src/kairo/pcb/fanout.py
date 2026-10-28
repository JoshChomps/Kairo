"""A via beside each surface pad of a plane's net, as a designer fans out ground.

A net carried by a whole-layer plane (ground on In1) reaches each surface
pad through a via: a short track out of the pad to a via that drops to the
plane. Designers place these first, before routing anything else, so that
every ground pad is one short hop from the plane and the router is left
only the signals. This module does the same, deterministically:

- for each surface pad of a plane's net, the via goes just beyond the
  pad's edge, on the side facing away from its part's centre where that
  is clear, else on the next clear side of eight;
- clear means the via, and the track to it, each grown by the clearance
  the two nets' classes ask, touch no pad of another net and no other via,
  stay on the board, and stay out of every footprint keep-out (a radio
  module's antenna area);
- the track runs straight from the pad's centre to the via;
- where a side allows, the via and its track also stay out of every other
  net's escape corridor: the strip off each end of a surface pad, along
  its long axis, `ESCAPE` long, by which a track leaves a fine-pitch pin.
  A ground via there walls the neighbouring pin in (a USB-C receptacle's
  VBUS pin, between its ground pins). Only where no side is clear of
  every corridor does the fanout fall back to ignoring them.

A pad with no clear side is left to the router, and counted.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from kairo.pcb.board_writer import Track, Via
from kairo.pcb.geometry import point_box, segment_box
from kairo.pcb.place import Outline, placed_pads

Rect = tuple[float, float, float, float]
#: How far past a pad's end its escape corridor runs, in mm.
ESCAPE = 1.0
#: The sides tried for a via, from straight away from the part's centre.
_TRIES = (0, 45, -45, 90, -90, 135, -135, 180)


@dataclass(frozen=True, slots=True)
class Fanout:
    """The fanout of a board's plane nets.

    Attributes:
        tracks: The short tracks from pads to vias.
        vias: The vias.
        left: Pads that found no clear side, as ``REF.PAD``.
    """

    tracks: list[Track]
    vias: list[Via]
    left: list[str]


def fan_out(
    outlines: Mapping[str, Outline],
    places: Mapping[str, tuple[float, float, float]],
    pad_nets: Mapping[str, Mapping[str, str]],
    planes: Sequence[str],
    bounds: Rect,
    *,
    via: tuple[float, float] = (0.6, 0.3),
    width: float = 0.3,
    clearance: float = 0.25,
    clearances: Mapping[str, float] | None = None,
    fixed: Sequence[Via] = (),
    keepouts: Sequence[Rect] = (),
    gap: Callable[[str, str], float] | None = None,
) -> Fanout:
    """Vias and tracks that join each surface pad of a plane's net to the plane.

    Args:
        outlines: Each part's footprint outline, by reference.
        places: Each part's x, y and turn on the board.
        pad_nets: Each part's pads' nets, by reference then pad number.
        planes: The nets carried by whole-layer planes, by KiCad's names.
        bounds: The board's edge, as left, top, right and bottom, in mm.
        via: The via's diameter and drill, in mm.
        width: The track's width, in mm.
        clearance: Copper's least distance to another net, in mm, for a
            net not in ``clearances``.
        clearances: Each net's clearance by KiCad's name, from its class;
            two nets keep the larger of their two.
        fixed: Vias already on the board, which a new via keeps clear of.
        keepouts: Boxes no via may touch, such as footprint keep-outs.
        gap: The clearance between two nets by KiCad's names, in place of
            ``clearances`` (`kairo.pcb.specctra.net_gaps`: a pair's own,
            such as the voltage between them).

    Returns:
        The tracks and vias, and the pads left to the router.
    """
    pads: list[tuple[str, str, str, Rect, str]] = []
    for ref, outline in outlines.items():
        if ref not in places:
            continue
        for number, box, layer in placed_pads(outline, *places[ref]):
            pads.append(
                (ref, number, pad_nets.get(ref, {}).get(number, ""), box, layer)
            )
    radius = via[0] / 2
    own = dict(clearances or {})
    pair = gap

    def between(a: str, b: str) -> float:
        if pair is not None:
            return pair(a, b)
        return max(own.get(a, clearance), own.get(b, clearance))

    taken: list[tuple[float, float, str]] = [(v.at[0], v.at[1], v.net) for v in fixed]
    tracks: list[Track] = []
    vias: list[Via] = []
    left: list[str] = []
    done: set[tuple[str, str]] = set()
    for ref, number, net, rect, layer in pads:
        if net not in planes or layer == "all" or (ref, number) in done:
            continue
        done.add((ref, number))
        cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
        px, py, _ = places[ref]
        away = math.atan2(cy - py, cx - px) if (cx, cy) != (px, py) else 0.0
        # A stub runs straight or at 45 degrees, as a designer draws it.
        away = round(away / (math.pi / 4)) * (math.pi / 4)
        spot = None
        lanes = [
            box
            for other_ref, other_number, other_net, other_box, other_layer in pads
            if other_net != net
            and other_layer == layer
            and (other_ref, other_number) != (ref, number)
            for box in [_corridor(other_box)]
        ]
        for respect, turn_by in [(True, k) for k in _TRIES] + [
            (False, k) for k in _TRIES
        ]:
            angle = away + math.radians(turn_by)
            vx, vy = _beside(rect, angle, radius + clearance)
            if (
                _clear(vx, vy, radius, net, pads, taken, bounds, between)
                and not any(point_box((vx, vy), box) < radius for box in keepouts)
                and _track_clear(
                    (cx, cy), (vx, vy), width / 2, net, ref, number, pads, between
                )
                and not (
                    respect
                    and any(
                        point_box((vx, vy), lane) < radius
                        or segment_box((cx, cy), (vx, vy), lane) < width / 2
                        for lane in lanes
                    )
                )
            ):
                spot = (round(vx, 3), round(vy, 3))
                break
        if spot is None:
            left.append(f"{ref}.{number}")
            continue
        vias.append(Via(spot, via[0], via[1], net))
        taken.append((spot[0], spot[1], net))
        tracks.append(Track((round(cx, 3), round(cy, 3)), spot, width, layer, net))
    return Fanout(tracks, vias, left)


def _beside(rect: Rect, angle: float, room: float) -> tuple[float, float]:
    """The nearest point from a pad's centre along ``angle`` that is ``room`` off it.

    Measured exactly from the pad's copper (`point_box`), so the stub keeps
    its direction: a via straight or diagonally off the pad, never skewed.
    """
    cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
    ux, uy = math.cos(angle), math.sin(angle)
    if abs(ux) < 1e-9:
        ux = 0.0
    if abs(uy) < 1e-9:
        uy = 0.0
    hw, hh = (rect[2] - rect[0]) / 2, (rect[3] - rect[1]) / 2
    # Along the direction the distance to the box grows monotonically;
    # bisect for where it reaches the room.
    low, high = 0.0, math.hypot(hw, hh) + room + 1.0
    for _ in range(40):
        mid = (low + high) / 2
        if point_box((cx + mid * ux, cy + mid * uy), rect) < room:
            low = mid
        else:
            high = mid
    return cx + high * ux, cy + high * uy


def _corridor(box: Rect) -> Rect:
    """The strip off both ends of a pad, along its long axis, ESCAPE long."""
    left, top, right, bottom = box
    if right - left >= bottom - top:
        return (left - ESCAPE, top, right + ESCAPE, bottom)
    return (left, top - ESCAPE, right, bottom + ESCAPE)


def _track_clear(
    start: tuple[float, float],
    end: tuple[float, float],
    half: float,
    net: str,
    ref: str,
    number: str,
    pads: Sequence[tuple[str, str, str, Rect, str]],
    between: Callable[[str, str], float],
) -> bool:
    """Whether a straight track keeps its clearance from other nets' pads.

    Measured exactly, segment to box.
    """
    for other_ref, other_number, other, box, _ in pads:
        if other == net or (other_ref, other_number) == (ref, number):
            continue
        if segment_box(start, end, box) < half + between(net, other):
            return False
    return True


def _clear(
    x: float,
    y: float,
    radius: float,
    net: str,
    pads: Sequence[tuple[str, str, str, Rect, str]],
    taken: Sequence[tuple[float, float, str]],
    bounds: Rect,
    between: Callable[[str, str], float],
) -> bool:
    """Whether a via there keeps its clearance from everything of another net."""
    left, top, right, bottom = bounds
    if (
        x - radius < left
        or x + radius > right
        or y - radius < top
        or y + radius > bottom
    ):
        return False
    for _, _, other, (pl, pt, pr, pb), _ in pads:
        if other == net:
            continue
        # The nearest point of the pad's box to the via's centre.
        nx = min(max(x, pl), pr)
        ny = min(max(y, pt), pb)
        if math.hypot(x - nx, y - ny) < radius + between(net, other):
            return False
    return all(
        math.hypot(x - ox, y - oy)
        >= 2 * radius + (between(net, other) if other != net else 0.0)
        for ox, oy, other in taken
    )
