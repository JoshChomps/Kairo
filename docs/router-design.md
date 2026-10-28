# Kairo's router: design

Kairo routes boards with Freerouting today, run as a separate program
because it is GPL-3.0. This page designs Kairo's own router, built from
published methods. The survey behind it read papers, vendors' user
documentation and Freerouting's observable behaviour on our boards, and
no router's source (see [research](research.md)).

## What the survey found

| Router | Published method | What we take |
|---|---|---|
| Situs (Altium) | Maps the board topologically (a triangulation between neighbouring obstacles) to find a "human-like" path first, then realises it with push-and-shove. Routing is an ordered strategy of passes: fan-out, memory (aligned pins), layer patterns, a main pass, completion (cheaper vias, dearer wrong-way), and clean-up passes (straighten, recorner, hug, spread). Its globally optimised main pass ignores conflicts on its first iteration, then reroutes with rising conflict cost until none remain. | Routing as a strategy of passes; a global first pass that may overlap, then negotiation; separate clean-up passes for straightening and corners. |
| Allegro PCB Router (Cadence, formerly Specctra) | Shape-based (gridless) routing; the cost of a conflict rises each pass; per-layer costs. | Conflict cost that rises per pass; layer costs. |
| PathFinder (McMurchie and Ebeling, ACM FPGA 1995) | Negotiated congestion: every net routes by least cost, sharing allowed. A resource's cost grows with how many nets use it now (present) and how often it was contested before (history), until no resource is shared. | The core loop. It is published, small and proven, and it converges where sequential rip-up stalls. |
| Freerouting (behaviour, measured 2026-10-23 on the BMS) | Documented as A* over convex "expansion rooms" in the continuous plane, then rip-up passes and an optimiser that runs only on a fully routed board. It treats every Specctra plane of a net as one joined plane, however far apart the pieces lie. Passes levelled off at about 48 unrouted and took 100 to 300 s each. | What to do better: see real copper pieces, and give a usable result even when not everything routes. |
| KiCad push-and-shove (FOSDEM 2015 abstract) | Interactive shove and hug on octagonal primitives, with trace smoothing. | Octilinear geometry, and smoothing as a separate step. |
| TopoR (Eremex papers) | Any-angle topological routing; minimum-length wire shapes under the design rules. | Later work: any-angle smoothing of a routed topology. |
| Lee (1961), Hart, Nilsson and Raphael (A*, 1968) | Shortest paths on a grid; an admissible heuristic. | The search inside each net. |

## Kairo's design

**Model.** A grid of cells on each routing layer, with a pitch from the
board's least track and clearance (0.25 mm for the BMS). Each net class
gets an obstacle map per layer: every other net's copper is grown by
that pair's clearance (`class_class`, the larger of the two) plus half
this class's track width. A cell is open to a class when its centre is
off the grown copper. Vias get their own map, grown by the via's radius,
across every layer, since a via is through.

**Search.** A* over (x, y, layer), moving in eight directions at
octilinear costs, plus a layer change at a via cost. The heuristic is
the octile distance plus one via for each layer between, which never
overestimates. A turn costs extra: 45° a little, 90° more, acute never,
because bends are what a designer and our board evaluation count.

**A net with many pins** grows as a tree, Prim-style: each search runs
from all the copper the net already has (its routed tree, its pads, its
filled pour islands) to the nearest pin not yet joined.

**Negotiation (PathFinder).** Every net is routed. A cell's cost is

```
(base + history) * (1 + present_factor * (others using it))
```

`present_factor` rises each iteration and `history` grows where nets
still overlap. Nets that overlap are ripped up and rerouted. The loop
stops when nothing overlaps or after an iteration limit; the result is
the overlap-free set, with what is still contested reported unrouted.
There is no all-or-nothing: unlike Freerouting's optimiser, the routed
nets keep their clean-up even when some nets fail.

**Pours are copper, not promises.** The order is a designer's:

1. Route the nets that are not poured.
2. Have KiCad fill the pours around them.
3. Route again, given the filled islands as real copper of their net, to
   join pads and islands with tracks and vias.

A pour net is never "joined" by an outline the fill may break.

**Clean-up passes**, after negotiation:

- *straighten*: replace runs of cells with the fewest octilinear segments
  the obstacle map allows (line of sight at 0°, 45° and 90°);
- *recorner*: turn 90° corners into two 45° bends where the cut corner
  is open.

**Output** is the `Router` contract `kairo.pcb.layout` already uses:
tracks and vias in board millimetres, so KiCad's DRC remains the judge.

## Measures

Every run is compared with Freerouting on the same placed board by
`kairo.pcb.make.summarize`:

- unrouted count;
- blocking DRC findings;
- bends per metre, and 90° bends;
- vias;
- seconds.

The earlier figure is the record: a number taken after tuning is reported
beside the one taken before, never in its place.

## Where it stands (2026-10-24)

Measured on the drafted BMS: 251 parts, 4 layers, the same placement for
both routers, pours on the FETs, shunt and bolts only.

| | Freerouting 2.5 | Kairo's router |
|---|---|---|
| Unrouted connections | 24 | 103 |
| Blocking DRC findings | 14 clearance | 9 clearance, 3 keep-out (since fixed) |
| Shorts | 0 | 0 |
| Bends per metre (90°) | 231 (197) | 270 (37) |
| Run time | 27 min | 18 min |

How the prototype got here, each step measured:

| Run | Change | Time | Unrouted | Shorts | Clearance |
|---|---|---|---|---|---|
| First | | 65 min a round | 188 | | |
| Second | Search state is the cell alone; flat stamped arrays; weighted A* | 2.7 min a round | | | |
| Third | Pad escapes, edge band, 15 rounds | | 42 | 177 | 424 |
| Fourth | Claims by true distance; vias claimed on every layer | | 92 | 5 | 32 |
| Fifth | Custom pads drawn to their real extent; only crossing connections dropped | | 103 | 0 | 9 |

Next, in order of what they cost:

1. **Pin access at fine pitch.** The 0.25 mm grid sits up to half a cell
   off a 0.5 mm pitch pad's axis, which is the 9 clearance findings at
   U1. Fix: a grid offset per pad row, or exact pad-axis stubs written
   as tracks.
2. **Completion.** A pass after negotiation that tries each dropped
   connection alone, with vias cheaper and at a finer pitch locally, as
   a completion pass does.
3. **Speed.** The search is pure Python. The arrays already suit a
   compiled kernel (numpy or a small C extension) if the project takes
   one on.

Freerouting stays the default engine until Kairo's router matches it on
unrouted connections; `make_board(..., engine="kairo")` runs Kairo's.
