# Making a board

`make_board` (and `kairo.pcb.make.make_board` under it) turns a built
schematic into a placed, poured, routed and KiCad-checked board in one
call. This page says what it does and why. Every rule here was learned by
hand on the 13S BMS board (2026-10-23), then written into the code, so the
next board does not have to be driven step by step.

## The one call

1. Write the design's `board`: `design_schema` with part `board` gives its
   shape. A design's own schema leaves the board out, so writing a
   schematic does not cost the board's 5,000 characters of schema.
2. Build the schematic with `apply_design`.
3. Call `make_board` once and read its summary. Call it again only to act
   on what the summary names. The places are kept in the project
   (`kairo/board_places.json`), so a re-route does not re-place;
   `replace_places: true` places afresh.

The summary is short on purpose. A raw KiCad DRC report on a 250-part
board runs to thousands of lines. The summary gives:

- `ready`: nothing blocks fabrication.
- `blocking`: copper, hole and outline findings by kind, three examples
  each.
- `land_pattern`: findings between two pads of one footprint. These are
  the maker's own geometry, kept as drawn (a USB-C receptacle's pads sit
  0.175 mm from its pegs), listed but not blocking.
- `cosmetic`: silkscreen findings, counted.
- `unconnected`: unrouted connections by net.
- `router_said`: Freerouting's own last word (passes, unrouted, whether
  its optimiser ran).
- `copper`: length, vias and bends by angle.

## What the design states

| Field | What it decides | Lesson |
|---|---|---|
| `sides` | Each block's compass cell | Cells are sized by the area their parts need, not fixed thirds: on the BMS the power column took 72 of 160 mm. |
| `edges` | Connectors, bolt terminals, a radio module's antenna end | The body (F.Fab) stands flush with the edge, or the copper stands the clearance in, whichever is further in. A keep-out beyond the body (an ESP32's antenna area) hangs off the board, as Espressif asks. |
| `pours` | High-current nets in copper, over the blocks they serve | A pour over the whole board floods it. Anchor each pour to its blocks. |
| `net_classes` | Track width and clearance per net | Clearances come from `calc` (IPC-2221 B2, the larger of revisions B and C: 0.64 mm at 96.8 V). The same figures reach the router, the plane fanout and KiCad's DRC. |
| `rules` | The maker's least figures | Read them from the maker's capabilities page. They have no defaults, because a wrong one passes a board the maker cannot build. JLCPCB multilayer, read 2026-10-23: 0.15 mm via drill, 0.25 mm via pad, 0.15/0.15 mm trace and space on 2 oz, 0.2 mm hole to copper, 0.2 mm edge. |

## What Kairo does without being told

- **Banks.** Parts of one footprint in one block whose pads share nets
  stand in a row. Back-to-back FETs split by source net into two rows
  that share their drain strip.
- **Power margins.** Parts in a pour's blocks keep 1.5 mm around them, so
  the pour and its stitching vias have room.
- **Stitching.** Every shared pour gets vias on a 2.5 mm pitch between its
  layers.
- **Plane fanout.** Every surface pad of a whole-layer plane's net (ground
  on In1) gets a short track to a via before routing, as a designer fans
  out ground first. The via and its track keep each neighbour's class
  clearance and stay out of footprint keep-outs. Without the fanout,
  Freerouting left 141 ground connections unrouted.
- **Router.** The newest `freerouting-*.jar` in the tools folder, run on a
  JDK 25 or newer from the same folder. It runs headless with analytics,
  the API server and the GUI off, so no design data leaves the machine.
  2.5.0 routed the BMS in 16 minutes. 2.1.0 hung or threw on it.
  Freerouting's optimiser, which straightens tracks and turns bends to
  45°, runs only on a fully routed board, so unrouted connections are the
  first thing to clear.
- **Pours and the router: three steps.**
  1. The router is given the pours as planes, so it does not thread 1 mm
     power tracks through dense FET pads. Routing them as tracks left 184
     to 464 unrouted and did not finish.
  2. KiCad then fills the pours around other nets at their clearance,
     which can break them into islands. Islands that overlap from layer
     to layer are joined with vias, as a designer stitches a pour.
  3. A short second routing stage gets the islands that are still apart.
     They go in as copper areas (fixed polygon wires), not as planes: a
     router treats every plane of one net as one joined plane, however
     far apart its pieces lie, so given as planes it never routed between
     them.

  Each step's result is kept only if it leaves fewer connections
  unrouted.
- **Pour only where the current flows.** Pour a high-current net over the
  blocks that carry the current (FETs, shunt, bolts), not over the clamps,
  snubbers and precharge beside them. Their scattered pads split each net
  into pieces, and pours that tile every layer leave no room for a track
  to join them. On the BMS, pouring all five power blocks left 28
  connections that no router could make.
- **Time.** A run is given 240 s per pass plus 10 minutes. A pass took up
  to 150 s on the BMS, and a run stopped at its time limit writes
  nothing.
- **Class pairs.** Between two classes, KiCad keeps the larger clearance.
  The router is told so pair by pair (`class_class`), or it falls back to
  its default between them.
- **Padstacks.** A pad shape on F.Cu and the same shape on B.Cu are two
  padstacks. One name for both lost the top pads and let the router
  short across a power FET.

## Reading bends

Fewer bends, and 45° rather than 90°, read cleaner and route current more
evenly. Acute bends are faults. `copper.bends` counts bends by angle, and
`bends_per_m` gives a figure to compare runs.

## Placement by job

The last gaps on the BMS came from where parts sit, not from routing.
Ideas from the KiStack skills (see [research](research.md)), measured on the BMS
(router-log.md, finding 24):

1. **Keep pin fronts free (built).** The area in front of a fine-pitch
   row (pins 0.65 mm apart or closer), 2 mm deep, is kept free of any
   part not on that pin's net, for its escaping tracks.
2. **Leave the fanout a way out (built).** A ground pad's via goes
   beside it but not into a fine-pitch part's escape corridor, unless
   no other side has room.
3. **Order by tolerance (tried, removed).** Pulling each decoupler to
   the IC pin it serves seated it there, but at 2 per mm the pull bent
   the whole placement round the ICs: 8 connections unmade against 1
   without it. How near each local part sits to its pin is reported
   (`placement` in the summary) instead.
3a. **Seat support parts after the rest (built 2026-10-25).** As a
   designer places an IC's capacitors and resistors at its pins first,
   then the rest round it: a local part (four pads or fewer) of an IC's
   own block that shares a net with it (`satellites`, most specific
   net first) is left out of the anneal. The IC keeps room round it for
   them (twice their area), and their other nets pull on the IC from
   the pin they serve. Once the rest is placed each is seated at the
   free spot nearest its pins that makes its nets shortest. The block
   is the designer's word: a thermistor wired only to the monitor IC
   belongs on the FET it measures, and its block says so where its
   nets cannot. A bank's members get none, so the row still forms.
4. **Radial small parts (not built).** Turn a two-pin part to point at
   its pin, not across the row, so the neighbouring pins keep their
   straight exits.
5. **Check reachability per IC (not built)** with the router itself,
   after each group of parts, and move a part that tolerates distance
   when it blocks one that does not.
6. **Pin swaps (not built).** Suggest swapping interchangeable pins where it untangles
   routing, never changing function, and show the swap for approval.

## Voltage spacing (fixed 2026-10-25)

Net classes give each net one clearance, whoever its neighbour is. A
battery stack needs spacing by the voltage between two nets: two cell
taps next to each other differ by 4.2 V, but a bundle of tap traces puts
taps far apart in the stack side by side, up to the whole pack.

Measured on the BMS (2026-10-25), with each tap's voltage from the
netlist (VCk through its filter resistor to CELLk, VC16 to CELL13) at
4.2 V a cell, and the other nets grouped as the low-voltage domain, the
pack's top, its gate drives and its switching node:

- 127 pairs of nets come closer than IPC-2221's B2 (outer layer,
  uncoated) asks for their voltage: above 30 V it asks 0.64 mm (the
  larger of revisions B and C, `kairo.calc.spacing`), where the board
  keeps 0.2. The closest is 0.226 mm between CELL10 and VC0 (42 V).
- Conformal coating does not settle it: B4 (outer, coated) asks 0.30 mm
  above 30 V, and 25 pairs are closer than that.

Fixed by spacing by pair (router-log rules 37 and 38): the design
states each net's range (`min` and `max`), the board its column
(`spacing: B2`), and Kairo derives each pair's clearance for the router,
the fanout, the stitching and KiCad's DRC (`<project>.kicad_dru`). A net
with no stated voltage is listed in the summary (`spacing_unchecked`),
never passed. On the BMS: 0 tracks or vias under B2. What remains is
inside single parts (a TOLL FET's gate beside its drain, a SOT-23 clamp
across the pack), each the part's own land pattern, judged under A6
(component terminations) rather than B2.

A bare board (B2) is the choice for a hobby build: coating costs at
assembly, hides rework, and a board safe uncoated does not depend on it.

## Parts in the wrong block

Blocks decide which IC a small part is seated at, so a part put in the
wrong block is placed away from the pins it serves and its tracks cross
the board: on the BMS, the 14 cell filter capacitors sat with the cells
while both their pins were the monitor IC's. The summary's `blocks`
lists every local part whose nets (supply rails and ground aside) all
reach one IC of another block and no other IC, each with its fix: move
it into the IC's block, unless it must sit where it is (a thermistor on
the FET it measures). Kairo finds them; the designer decides. When
connections are left, the next step names these first.

## Routes taken first

A designer routes the hard or critical nets by hand, then lets the
autorouter fill in the rest. `board.routes` says which, in order, with
the layers each may use: Kairo routes each alone, the rest of the board
in its way, then holds it fixed while routing everything else. Kairo
still chooses every coordinate; the design names only the order and
the layers. A net routed alone takes its own shortest way, and the rest
go round it: on the BMS, naming the last gap's net made five others
(router-log.md, finding 44). Name nets for what the router cannot know,
such as a sense pair to keep on one layer.

## Mending the last gaps

A connection still missing at the end is mended as a designer would:
the router's tracks of other nets within 2 mm of the line between its
ends (then 4 mm) are taken up, its net is routed, and the nets taken
up are joined again. KiCad judges the result, and it is kept only if
fewer connections are left.

## Detours

A connection forced through is joined but not good. The summary's
`detours` lists nets of two or three pads whose tracks add up to 2.5
times their pads' span or more (and 10 mm or more), with their vias.

## Silkscreen labels

Each part's reference is placed by `kairo.pcb.silk`, largest parts
first: over the part's body where it has no pads, else above, below,
left or right of its courtyard, at 1.0 mm and then 0.8 mm text, clear of
every pad, every other part's print, labels already placed and the
board's edge. One that fits nowhere moves to the fabrication layer. On
the BMS about 300 silkscreen findings became 18, all edge connectors'
own outlines overhanging the board edge, which the maker trims.

## Current in narrowed copper

A net may state the most current any of its tracks carries (`current`;
a pour's own current is the pour's). Where the router narrows a wide
net's track, the summary checks the narrowest copper with IPC-2221's
chart (`kairo.calc`) at the board's allowed rise (`trace_temp_rise`, 10
degC unless stated): ``pass``, ``fail`` (listed as ``current_fails``),
or ``unverified`` where no current is stated. A short neck sheds heat
into the wider track either side, so a pass is cautious.
