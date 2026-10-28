"""Kairo's own router: negotiated congestion over an octilinear grid.

Designed in docs/router-design.md from published methods only (the record
is in docs/research.md):

- A* (Hart, Nilsson and Raphael, IEEE Trans. SSC 4(2), 1968) over cells
  of each routing layer, eight directions at octilinear cost, with a via
  to change layer; a turn costs extra, 45 degrees a little and 90 degrees
  more, and an acute turn is never taken.
- Negotiated congestion (McMurchie and Ebeling, "PathFinder", ACM FPGA
  1995): every net is routed, sharing allowed; a cell's cost grows with
  the nets using it now and with how often it was contested before; the
  nets that share are ripped up and routed again until none do.
- A net with many pins grows as a tree (Prim's order): each search starts
  from all the copper the net already has (its pads, its routed tree and,
  in a second stage, its filled pour islands) and ends at the nearest
  piece not yet joined. A net whose copper is already one piece is left
  alone.
- Clean-up: each routed run is straightened to the fewest octilinear
  segments the obstacles allow.

Clearance is held on the grid by growing every other net's copper by the
pair's clearance (the larger of the two classes', as KiCad's DRC keeps
it) plus half this class's track width; a cell is open to a net when its
centre is outside every other net's grown copper. KiCad's DRC remains the
judge of the result.

The package is in layers, each using only those before it: `grid`
(what is in the way), `search` (A*), `negotiate` (congestion and the
passes after), `copper` (tracks, made legal) and `board` (a board's
routing, end to end).
"""

from kairo.pcb.autoroute.board import (
    RouteCase,
    audit,
    case_for,
    case_from_json,
    case_to_json,
    focus,
    kairo_router,
    route_board,
    route_case,
)
from kairo.pcb.autoroute.grid import AutorouteError, Settings

__all__ = [
    "AutorouteError",
    "RouteCase",
    "Settings",
    "audit",
    "case_for",
    "case_from_json",
    "case_to_json",
    "focus",
    "kairo_router",
    "route_board",
    "route_case",
]
