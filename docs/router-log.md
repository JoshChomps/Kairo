# Router log: what makes it effective

Every change to Kairo's router is measured on the same frozen case, and
the result decides whether it stays. This page holds the method, the
findings so far as rules, and the ledger.

## Method

- **The cases.** `evals/routing/freeze.py` freezes a built project's
  routing input; `crop.py` cuts a window out of one. Use, by need:
  - `bms_u1_window` (the BMS around its monitor IC, about 15 s) for
    iterating;
  - `mixed_signal` (a second, different board, about 40 s) before
    keeping a change;
  - `bms_13s11p_v3` (the whole BMS on its best placement, about 15
    minutes) to confirm;
  - boards other people drew, read by `evals/routing/import_board.py`
    (their pads and own net classes, their tracks and pours taken up):
    `mikoto` (an nRF52840 module, 4 layers), `microphone-board` and
    `baseboard_2slots` (2 layers), each in seconds;
  - `evals/routing/corpus.py`, every demo board of the KiCad install,
    routed and audited in turn, for crashes and faults on boards of
    every kind (nothing of the demos is kept in the repository). On
    KiCad 10's 16 demos of up to 1500 pads: no crash, 0 shorts and 0
    clearance faults on each, 3104 of 3401 connections routed, five
    boards whole; the misses are mostly where a board's own pours carried
    the nets, which the import takes up.

  A refactor must leave the fast cases' output byte-identical (`--keep`
  and compare); routing is deterministic.
- **The first case.** `evals/routing/cases/bms_13s11p_signals.json` freezes the
  router's input for the drafted BMS:
  - 251 parts, 443 connections to make, 3 routing layers;
  - the signal stage, with the pours left for after.

  It replays without KiCad.
- **The run.**

  ```
  uv run python evals/routing/bench.py "what changed" [--set name=value]
  ```

  It routes the case, audits the result exactly (shorts, clearance, edge,
  keep-out, with examples), measures the copper and appends one line to
  `evals/results/routing.jsonl`. `--set` tries any `Settings` value
  without a code change.
- **The table.** `uv run python evals/routing/report.py` prints the
  ledger below.
- **Order of merit**, as a designer judges a board:
  1. no shorts or clearance faults;
  2. then the fewest unrouted connections;
  3. then the fewest 90° bends and vias;
  4. then time.

  Times are comparable only between runs made alone on the machine.
- **A change stays** only if it improves the order of merit and does not
  break a unit test.

## Findings, as rules

Each rule is the change that measured best, and why it works.

1. **Measure claims by true distance, never in whole cells.** A
   neighbour's centreline must stand a track's width and a clearance
   off: 1.8 cells at 0.25 mm. Rounded down to 1 cell, every diagonal
   neighbour went unseen, giving 424 clearance faults. Measured exactly:
   32.
2. **A via is wider than a track and passes through every layer.** It
   must check other nets' routing over its whole radius on every layer.
   Checking only its own cell gave 193 to 197 shorts once the completion
   pass leaned on it. With the check: 0.
3. **Treat a power package's drain pad as its drawn copper.** A custom
   pad's anchor understates it: the TOLL's is 2.9 mm wide against 8.0 mm
   of copper. Routes ran across it.
4. **Drop crossing connections, not whole nets.** Dropping a 14-pin
   supply net for one crossing threw away 13 good connections.
5. **A completion pass after negotiation recovers most of what
   negotiation drops.** With other nets' routes as walls and vias at half
   price, it made 125 to 132 of the 133 dropped connections. It is only
   legal once rule 2 holds.
6. **Count unrouted from the copper, not the runs.** A net is joined when
   its pieces and runs form one piece. Counting runs against connections
   went negative once rip-up became granular.
7. **Escape strips stop a clearance plus half a track short of other
   copper**, measured from the pad's true axis. Stopping at half a cell
   left tracks 0.1 to 0.19 mm from neighbouring pads.
8. **Size claims for the narrowest neighbour; let a wide net check the
   extra itself.** A claim assumed a neighbour as wide as the claimant,
   so a 0.5 mm supply track routed beside a 0.25 mm one kept too little
   room: 97 clearance faults. Claims now leave room for the narrowest
   class, and each wider class checks a small ring round every step: 14
   to 18 faults.
9. **Rip up connections, not nets (granular negotiation).** Rerouting
   only a net's contested connections keeps its settled tree, which
   settles the board faster: 6 minutes against 15 for the same result,
   or better.
10. **Prefer a direction per layer (across, down, across).** A step
    against a layer's direction costs 1.5 times as much. It cut unrouted
    connections from 24 to 19 and bends per metre from 260 to 242, for
    more vias (374 against 323).
11. **Neck a track down where it leaves a fine-pitch pin.** On the
    BQ76952's 0.5 mm pitch with 0.36 mm pads, a 0.25 mm track along a
    pin's axis leaves 0.195 mm to the next pin, under the 0.2 mm
    clearance. So no full-width track can legally leave the pin. The
    escape is drawn as narrow as the neighbours allow, measured exactly,
    down to the maker's least track (0.15 mm).
12. **A pad on no copper layer is not copper.** Power packages draw
    paste apertures as numberless pads on F.Paste over their copper pads.
    Read as F.Cu, they walled in every FET gate (9 of the 11 unreachable
    pins) and blocked the fanout and the placer too. The case was
    re-frozen without them (150 KB, now 124 KB); rows before that are
    on the flawed case.
13. **A diagonal grazes a corner its grid check cannot see.** Obstacles
    are measured at cell centres, and a 45° step passes between two side
    cells, up to p/(2√2) (0.09 mm at 0.25 mm) nearer their copper. Raising
    the slack everywhere to cover it sealed the fine-pitch passages: 46
    unreachable. Forbidding any diagonal past a blocked side cell removed
    the corner faults but left 5 more unrouted. Neither is the default.
14. **Legalise last, exactly: neck each track to the room it has.** After
    routing, each track is measured against all other copper by true
    distance and drawn as wide as fits, down to the maker's least track.
    This removed every remaining clearance fault (22 to 0, 30 tracks
    necked) at no routing cost. The grid finds the way; exact geometry
    makes it legal.
15. **The measured best settings are the defaults:** granular rip-up,
    1.5 times for the wrong way, completion and legalising on, strict
    corners off. On the BMS signal stage that is 10 of 443 connections
    unrouted (2 of them blocked by fixed copper), no faults, 4.6 minutes.
16. **Drill holes keep apart whatever their nets.** KiCad's hole-to-hole
    rule applies within a net too: two vias of one net, or a via beside
    its own net's through pin. Through copper now bars every via within
    a drill's spacing, and a net's new vias keep clear of the ones it
    already has, including those in connections a rejoin kept. The
    signal stage is then clean of every fault: 0 shorts, 0 clearance,
    0 holes.
17. **Joining pours after they are filled is the wrong order.** Measured
    on the frozen join case (`bms_13s11p_join`, 51 connections still
    apart after stage one and stitching), 36 were unreachable. Every bit
    of the power area is some net's filled copper, so a 1 mm
    high-voltage join has nowhere to run. Route the power nets as tracks
    first, then pour around them, so the pours only add copper to what
    is already joined.
18. **Route everything as tracks, then pour.** Measured on the signal
    case with the poured nets included (711 connections): 7 unrouted, 2
    of them blocked by fixed copper, and no faults, in 7.5 minutes. On
    the full board, KiCad's DRC found nothing blocking and 4 connections
    unmade (judged without the net classes, as rule 34 found: every net
    at the Default 0.2 mm, for both routers), against Freerouting's 14
    clearance faults and 24 unmade in 27
    minutes. This is the default stage now (`all`).
19. **Read which way a part faces from its shape, not its library
    drawing.** KiCad draws the ESP32 module antenna-up but the USB-C
    receptacle mouth-down. Assuming "top faces out" put the receptacle
    backwards on the board edge, its VBUS pins boxed in between its own
    pegs and the edge, unreachable by any router. The outward side is
    where the courtyard reaches furthest past the pads.
20. **More rounds do not finish the last few.** On the re-placed board,
    30 negotiation rounds left 5 connections unmade against 4 for 15
    rounds: the negotiation had settled. The last few need a targeted
    step, not more of the same.

21. **Lift what blocks a dropped connection, and keep the trade only if
    it is clean.** The rescue pass routes a missing connection with
    other routes passable, lifts only the connections whose claims that
    path enters, lays it, and reroutes the lifted ones with every route
    a wall. It keeps the trade only if no net ends up missing more and
    nothing it touched crosses anything. On the full case: 8 unrouted
    against 10, no faults.
22. **Check a clash from both sides.** A via claims a via's width and a
    clearance round it (3.2 cells); a track claims less (1.8 cells). So
    a via 2.12 cells from another net's track sits inside its own claim
    but outside the track's: the clash is visible only from the track's
    side. Negotiation checks every net each round and sees it; the
    rescue checked only the nets whose claims its own path entered, and
    let 3 clearance faults through on the re-placed board
    (`bms_13s11p_placed`: a VC8 diagonal 0.105 mm from a CELL10 via).
    The rescue now finds touched nets both ways (`_touching`): 0 faults,
    13 unrouted against 12, the twelfth having been the illegal one.
23. **A stitch's drill keeps a hole's spacing from through pads too.**
    Stitching the pours after routing put vias inside the filled copper,
    clear of other nets, but 130 of them too near a through pin's hole
    of their own net (KiCad's hole-to-hole rule holds within a net).
    Stitches now keep clear of every through pad, its hole taken as its
    copper, as the router takes it.

24. **Keep fine-pitch pins' fronts and fanout corridors free; do not
    pull decouplers to their pins.** Three placement changes, measured
    on the whole board (`make_board`, KiCad's DRC the judge), each
    against the board before:

    | Placement | Unmade | Blocking | Track |
    |---|---|---|---|
    | before (shape-aware, banks, edges) | 4 to 5 | none | 8,000 to 8,136 mm |
    | fronts, corridors and a 2 per mm decoupler pull | 8 | 3 clearance (rule 22) | 8,827 mm |
    | fronts and corridors, no pull | **1** | none | 8,155 mm |

    The pull seated each decoupler at its pin (local parts 36.4 mm from
    their ICs on average, against 33.9 with it and 35.8 without) but bent
    the whole placement round the ICs. It was removed; the placement
    report still measures it. Rerun on the same placement, the board came
    out identical: placement and routing are deterministic.
25. **Stitch the pours before routing.** Stitching after the fill puts
    vias only where the copper is, so it walls off no channel; measured
    on the same placements, it left 7 unmade against 8, and 2 against 1.
    No consistent gain, so the option was removed.
26. **Measure a stage before calling it dead.** Routing everything as
    tracks first (rule 18) seemed to leave the rejoin stage nothing to
    do. Measured on the best placement: KiCad found 7 connections apart
    after the fill and island stitching, and the rejoin joined 6 of them
    (7 to 1). It stays.
27. **Keep each leg octilinear when its end moves onto a pad's axis.**
    Straightening works on cell centres; the end is then moved up to
    half a cell onto the pad's true axis, and a 45 degree leg became 46
    or 44. On the U1 window that was 70 odd turns and 55 runs off 45
    degrees, nearly all on F.Cu. Now the leg keeps its heading and a
    short 45 degree jog (under 0.2 mm) at its other end takes up the
    offset, unless the straight line is within half a degree anyway: 0
    and 0, at no cost in faults or unrouted.
28. **A second board finds what one board hides.** Every number before
    came from the BMS. The second case (`mixed_signal`: the 13 sound
    reference designs on a dense 2-layer board with no pours, 75 parts)
    showed 12 clearance faults at once, round a QFN's exposed pad, from
    two holes in the rules that the BMS never exercised (rules 29, 30).
29. **A step from one pad straight into another is not a pad's own.**
    A pad's cells are open to its net past other copper, or no router
    could reach a fine-pitch pin. But a step from an exposed pad's cell
    straight into a pin's cell crossed the 0.2 mm gap between them,
    0.125 mm from the next pin. Entering a pad's cell that other copper
    blocks is now allowed only from that same pad (or a routed or escape
    cell): 12 faults to 2.
30. **Measure the diagonals that pass fixed copper, once, exactly.** A
    45 degree step can graze a pad's corner closer than either cell's
    centre shows (rule 13). Before the search, each diagonal step past a
    blocked side cell is measured against the fixed copper near it; if
    even a track necked to the maker's least would be too near a net's
    copper, the step is barred to every other net. The search and the
    straightening read the map for free. A step no net can stand at
    both ends of is not measured, and each step is measured once for all
    classes: the board's setup went from 203 s to 22 s with the map
    unchanged. The last 2 faults on the second board went to 0, with no
    passage sealed (rule 13's strict corners had cost 5 connections).
31. **Pull the finished routes tight, and keep off the edge where there
    is room.** Two passes after negotiation: a soft band 1.5 mm in from
    the edge's keep-off costs twice as much (a track runs along the
    edge only where nothing else is free), and a tidy pass lifts each
    run 1.25 times longer than the octilinear way between its ends and
    keeps a shorter route found with every other route a wall. On the
    whole board: track 8,021 mm to 7,794 (3 % less), vias 494 to 472,
    unrouted 5 to 4, 21 runs tidied.
32. **Every pass that moves copper checks clashes from both sides.**
    The tidy pass first checked only its own new route's centreline,
    as the rescue once did (rule 22), and let a re-routed PACK_P via
    land 0.46 mm from an untouched LD_SNS track (7 faults). It now uses
    the rescue's two-sided check.
33. **The last word is exact geometry: drop a run still at fault.**
    After legalising, the router audits its own copper exactly, drops
    the shorter run of each clashing pair, and draws again. A rule the
    grid gets wrong then costs a connection, reported, never a fault in
    the copper: router-log principle since rule 4 (a gap is reported, a
    short is not made), now enforced at the end rather than hoped for.
    On the whole board: 0 faults (2 before), 6 unrouted (4 with the
    faults kept, 7 when a dropped run was not joined again).

    Times on 2026-10-25 are not comparable with earlier rows: another
    program loaded the machine (the 15 s case took 44 s).
34. **Check the judge.** KiCad 10 ignores the class patterns in a
    project file whose `net_settings` block is version 0, as Kairo's new
    projects were written: every net was filled and checked at the
    Default class's 0.2 mm. Measured on one board and project, changing
    only the version: 0 gives 0 clearance findings, 3, 4 and 5 give 499
    (its pours had been filled at 0.2 mm). Every `make_board` verdict
    before 2026-10-25 was made this way; the router's own audit always
    used the classes. `apply_rules` now writes version 4. A test that
    goes red against a judge which sees nothing is worth more than a
    verdict.
35. **A stitching via keeps the class clearance, not a courtyard's.**
    With the classes applied, the pre-route stitching put PACK_P vias
    0.30 to 0.61 mm from other nets' pads, where HV asks 0.64: clear of
    courtyards, whose margin is 0.25. They now keep the larger of the two
    nets' clearances from every other net's pad.
36. **A fanout stub runs straight or at 45 degrees.** The ground fanout
    put each via straight away from its part's centre, at any angle: the
    last 36 runs off 45 degrees on F.Cu. The side is now rounded to an
    eighth of a turn and the via stepped out along it, exactly clear of
    the pad. On the whole board, judged with the classes: 0 blocking
    findings, 3 connections unmade, 0 runs off 45 degrees on any layer.
37. **Space nets by the voltage between them, not by class alone.** A
    battery stack's taps run side by side, up to the whole pack apart.
    The design states each net's range and the board an IPC-2221 column
    (B2, bare); classes split into 15 V bands, and a pair of bands keeps
    the column's spacing for its worst pair of nets, in the router, the
    fanout, the stitching and KiCad's DRC (a `.kicad_dru` file, checked
    with KiCad to apply). On the BMS: 153 pairs of tracks or vias under
    B2 before, 0 after.
38. **Hold a pair's extra spacing where it applies, not everywhere.**
    First the extra went into each class's ring, so every tap track kept
    0.64 mm from every other routed track, its neighbour 4.2 V away
    included: 15 connections unmade against 3. Now a class that others
    must keep far from also claims a wider set of cells (its widest pair
    and the widest mover's half), and a mover reads only the claims of
    the classes it is far from in voltage, at its own centre: 6 unmade,
    still 0 under B2, and the plain cases byte-identical.
39. **The last check drops a run for any fault it measures.** It dropped
    runs for shorts and clearance but not hole spacing, and a pair of
    CELL6 vias 0.25 mm apart reached KiCad. Holes count now too.
40. **Do not spread parts for voltage; the router holds the spacing.**
    Keeping 1 mm more between parts far apart in voltage spread the
    whole placement (local parts 42.6 mm from their ICs on average,
    against 33.9) and left 15 connections unmade against 6. Removed, as
    the decoupler pull was (rule 24): what costs wirelength costs more
    than the room it gives.
41. **The 6 left were congestion round the monitor IC, not access.** On
    the frozen voltage case the main stage leaves 29 apart, all dropped,
    none unreachable; the rejoin after the fill brings it to 6. Thirty
    negotiation rounds, `make_board`'s default, made 5. Wider room in
    front of pins (3 mm, not 2) made 8: it moves the parts apart round
    U1 rather than opening its channels, so the fronts stay at 2 mm.
42. **The last gaps were placement's: seat each IC's own parts at its
    pins.** Measured on the saved board, the part nearest each U1 pin
    stood 11 to 40 mm away (C24 on NTC1 26 mm, C19 on REGIN 27 mm): the
    anneal shortens wirelength, and a filter capacitor costs the same
    anywhere along the line to its tap. Every one of those nets then
    crossed the crowd round U1. Now each IC's local parts of its own
    block are left out of the anneal, the IC keeps room round it for
    them, and they are seated nearest their pins afterwards. Unlike the
    pull tried in finding 24, the anneal never feels them, so nothing
    bends round the ICs. With the cell filters moved into the monitor's
    block, all five gaps at U1 were joined (6 others left: the FET banks
    had broken, see 43).
43. **A bank keeps its row.** Seating a gate resistor at each FET made
    each FET unlike its neighbours, so the bank did not form and the
    pours split: 3 islands. A bank's members get no seated parts. Then 1
    connection was left (PDSG), against 5; 8,128 mm of track against
    8,785, 801 vias against 811, no detours.
44. **A net routed alone first is no fix by itself.** Named in
    `board.routes`, a net is routed with the rest of the board unrouted,
    so it takes its own shortest way and the others then go round it.
    Three nets taken first on the broken-bank board were all joined, and
    two others at the same IC came apart instead (5 left against 6);
    PDSG taken first on the good board left 5 against 1, and two pours
    then failed their current check where they narrowed. The guide is
    for what a designer knows the router does not (a sense pair kept on
    one layer, a gate drive kept short), not for closing gaps.
45. **Compile the loops, not the router.** The A* was 91 % of a route's
    time, the Python interpreter itself most of that. Ported to Rust bit
    for bit, over the same arrays read in place, the whole BMS (v4) went
    from 1,509 s to 114 s with the same copper; then the grazing steps'
    setup, 21 classes measured cell by cell, from 51 s to 14 s: 66 s in
    all. A port is kept only if the frozen cases give byte-identical
    results and a deliberate break of it fails the comparison test.

## Ledger

Printed by `evals/routing/report.py`; the newest run is last.

One table per frozen case. Rows on the signal case before rule 12 are on the flawed case (paste apertures read as copper).

### bms_13s11p_signals

| When | Commit | Change | Settings | Unrouted | Shorts | Clearance | Edge | Keep-out | Completed | Vias | 90° | Bends/m | Seconds |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-24T19:47Z | 4997cae+ | baseline: router as of 2026-10-24 (escapes, true-distance claims, per-connection drops) |  | 133 | 0 | 46 | 0 | 0 |  | 223 | 25 | 269.9 | 926.0 |
| 2026-10-24T20:08Z | 4997cae+ | exact stubs + completion + layer directions | wrong_way=1.5 | 2 | 197 | 721 | 0 | 0 | 125 | 520 | 9 | 295.0 | 1243.5 |
| 2026-10-24T20:08Z | 4997cae+ | exact pad-axis stubs | complete=0.0 | 133 | 0 | 45 | 0 | 0 | 0 | 223 | 14 | 270.5 | 1257.1 |
| 2026-10-24T20:08Z | 4997cae+ | exact stubs + completion pass |  | 1 | 193 | 834 | 0 | 0 | 132 | 502 | 13 | 322.2 | 1260.0 |
| 2026-10-24T20:14Z | 4997cae+ | via ring + completion + granular + directions | granular=1.0, wrong_way=1.5 | -35 | 0 | 68 | 0 | 0 | 6 | 367 | 10 | 246.5 | 259.7 |
| 2026-10-24T20:15Z | 4997cae+ | via ring + completion + granular rip-up | granular=1.0 | -41 | 0 | 76 | 0 | 0 | 13 | 354 | 9 | 269.0 | 334.2 |
| 2026-10-24T20:24Z | 4997cae+ | via ring check + completion |  | 11 | 0 | 97 | 0 | 0 | 16 | 335 | 20 | 281.2 | 863.2 |
| 2026-10-24T20:32Z | 4997cae+ | width-aware + exact escapes + completion + granular | granular=1.0 | 24 | 0 | 17 | 0 | 0 | 2 | 323 | 9 | 260.0 | 353.1 |
| 2026-10-24T20:32Z | 4997cae+ | width-aware + exact escapes + completion + granular + directions | granular=1.0, wrong_way=1.5 | 19 | 0 | 14 | 0 | 0 | 0 | 374 | 11 | 242.1 | 353.9 |
| 2026-10-24T20:40Z | 4997cae+ | width-aware claims + exact escapes + completion |  | 23 | 0 | 18 | 0 | 0 | 4 | 339 | 14 | 269.7 | 878.4 |
| 2026-10-24T20:47Z | 4997cae+ | neckdown at fine-pitch escapes (alone, clean timing) | granular=1.0, wrong_way=1.5 | 17 | 0 | 47 | 0 | 0 | 6 | 381 | 9 | 245.9 | 243.5 |
| 2026-10-24T20:53Z | 4997cae+ | case re-frozen without paste apertures; neck chunks fixed | granular=1.0, wrong_way=1.5 | 10 | 0 | 22 | 0 | 0 | 5 | 394 | 7 | 247.3 | 238.9 |
| 2026-10-24T20:58Z | 4997cae+ | same settings, copper kept for inspection | granular=1.0, wrong_way=1.5 | 10 | 0 | 22 | 0 | 0 | 5 | 394 | 7 | 247.3 | 278.5 |
| 2026-10-24T21:00Z | 4997cae+ | slack 0.09 mm (diagonal rounding) | granular=1.0, wrong_way=1.5, slack=0.09 | 46 | 0 | 16 | 0 | 0 | 0 | 307 | 2 | 228.4 | 418.3 |
| 2026-10-24T21:06Z | 4997cae+ | strict corners: no diagonal past a blocked side cell | granular=1.0, wrong_way=1.5, strict_corners=1.0 | 15 | 0 | 16 | 0 | 0 | 6 | 389 | 22 | 252.6 | 256.3 |
| 2026-10-24T21:07Z | 4997cae+ | straightening keeps the search's corner rule | granular=1.0, wrong_way=1.5 | 10 | 0 | 22 | 0 | 0 | 5 | 394 | 7 | 247.3 | 286.6 |
| 2026-10-24T21:12Z | 4997cae+ | legalising neckdown + strict corners | granular=1.0, wrong_way=1.5, strict_corners=1.0 | 15 | 0 | 0 | 0 | 0 | 6 | 389 | 22 | 252.6 | 260.9 |
| 2026-10-24T21:12Z | 4997cae+ | exact legalising neckdown pass | granular=1.0, wrong_way=1.5 | 10 | 0 | 0 | 0 | 0 | 5 | 394 | 7 | 247.3 | 275.2 |
| 2026-10-24T21:26Z | 4997cae+ | drill spacing: through copper and a net's own vias |  | 7 | 0 | 0 | 0 | 0 | 4 | 406 | 9 | 251.5 | 337.2 |
| 2026-10-24T21:32Z | 4997cae+ | a net's rejoin keeps its own holes' spacing |  | 10 | 0 | 0 | 0 | 0 | 5 | 394 | 8 | 251.9 | 295.5 |
| 2026-10-24T21:41Z | 4997cae+ | route the pour nets as tracks first (one stage, everything) |  | 7 | 0 | 0 | 0 | 0 | 3 | 476 | 8 | 238.4 | 447.4 |

### bms_13s11p_join

| When | Commit | Change | Settings | Unrouted | Shorts | Clearance | Edge | Keep-out | Completed | Vias | 90° | Bends/m | Seconds |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-24T21:28Z | 4997cae+ | join stage: first measure |  | 36 | 0 | 0 | 0 | 0 | 0 | 11 | 0 | 161.6 | 42.4 |
| 2026-10-24T21:33Z | 4997cae+ | join stage: first measure |  | 36 | 0 | 0 | 0 | 0 | 0 | 11 | 0 | 161.6 | 39.6 |

### bms_13s11p_all

| When | Commit | Change | Settings | Unrouted | Shorts | Clearance | Edge | Keep-out | Completed | Vias | 90° | Bends/m | Seconds |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-24T22:17Z | 4997cae+ | re-placed board, every net (case bms_13s11p_all) |  | 10 | 0 | 0 | 0 | 0 | 9 | 438 | 13 | 234.9 | 458.5 |
| 2026-10-24T22:27Z | 4997cae+ | rescue: lift what blocks a dropped connection |  | 4 | 40 | 37 | 0 | 0 | 9 | 454 | 13 | 238.0 | 459.7 |
| 2026-10-24T22:35Z | 4997cae+ | rescue: lift by claim, keep only if nothing crosses |  | 8 | 0 | 0 | 0 | 0 | 9 | 440 | 13 | 236.6 | 485.5 |

### bms_13s11p_placed

| When | Commit | Change | Settings | Unrouted | Shorts | Clearance | Edge | Keep-out | Completed | Vias | 90° | Bends/m | Seconds |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-24T23:01Z | 4997cae+ | job-aware placement (kairo12), after refactor |  | 12 | 0 | 3 | 0 | 0 | 8 | 501 | 35 | 246.6 | 694.1 |
| 2026-10-24T23:15Z | 4997cae+ | rescue: touched nets from both sides (via claim > track claim) |  | 13 | 0 | 0 | 0 | 0 | 8 | 493 | 35 | 245.9 | 699.1 |

### bms_13s11p_v3

| When | Commit | Change | Settings | Unrouted | Shorts | Clearance | Edge | Keep-out | Completed | Vias | 90° | Bends/m | Seconds |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-25T00:38Z | ba1d926+ | v3 baseline: dogleg only (tidy, edge band off) | tidy=0.0, edge_band=0.0 | 5 | 0 | 2 | 0 | 0 | 4 | 494 | 150 | 294.2 | 688.9 |
| 2026-10-25T01:07Z | ba1d926+ | v3: grazes + pad bridges + dogleg; tidy and edge band off | tidy=0.0, edge_band=0.0 | 5 | 0 | 2 | 0 | 0 | 4 | 494 | 150 | 294.2 | 1008.2 |
| 2026-10-25T01:08Z | ba1d926+ | v3: grazes + pad bridges + dogleg + edge band + tidy (all on) |  | 4 | 0 | 7 | 0 | 0 | 7 | 467 | 131 | 299.7 | 1082.7 |
| 2026-10-25T01:25Z | ba1d926+ | v3 all on: fast grazes, pad bridges, dogleg, edge band, two-sided tidy |  | 4 | 0 | 2 | 0 | 0 | 7 | 472 | 133 | 299.3 | 870.3 |
| 2026-10-25T02:02Z | ba1d926+ | v3 final: + exact audit drops a run still at fault |  | 7 | 0 | 0 | 0 | 0 | 7 | 469 | 131 | 298.5 | 2021.8 |
| 2026-10-25T02:40Z | ba1d926+ | v3 final: dropped runs joined again, walled |  | 6 | 0 | 0 | 0 | 0 | 7 | 469 | 133 | 298.8 | 1994.4 |

### bms_u1_window

| When | Commit | Change | Settings | Unrouted | Shorts | Clearance | Edge | Keep-out | Completed | Vias | 90° | Bends/m | Seconds |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-25T00:22Z | ba1d926 | fast case baseline |  | 5 | 0 | 0 | 12 | 0 | 0 | 65 | 3 | 287.1 | 18.9 |
| 2026-10-25T00:22Z | ba1d926 | fast case: copper wholly inside the window |  | 0 | 0 | 0 | 0 | 0 | 0 | 55 | 3 | 296.4 | 6.4 |
| 2026-10-25T00:23Z | ba1d926+ | dogleg: legs stay octilinear onto exact pad points |  | 0 | 0 | 0 | 0 | 0 | 0 | 55 | 25 | 364.2 | 6.4 |
| 2026-10-25T00:23Z | ba1d926+ | dogleg, skipped within half a degree |  | 0 | 0 | 0 | 0 | 0 | 0 | 55 | 24 | 357.8 | 5.9 |
| 2026-10-25T00:26Z | ba1d926+ | edge band 1.5 mm + tidy pass |  | 0 | 0 | 0 | 0 | 0 | 1 | 60 | 27 | 355.2 | 11.6 |
| 2026-10-25T00:40Z | ba1d926+ | diagonals measured past fixed copper (grazes) |  | 0 | 0 | 0 | 0 | 0 | 1 | 60 | 27 | 355.2 | 20.0 |
| 2026-10-25T00:45Z | ba1d926+ | grazes measured from every cell, own pads included |  | 0 | 0 | 0 | 0 | 0 | 1 | 60 | 27 | 355.2 | 25.1 |
| 2026-10-25T00:49Z | ba1d926+ | grazes: offenders as sets (own copper never bars) |  | 0 | 0 | 0 | 0 | 0 | 1 | 60 | 27 | 355.2 | 25.6 |
| 2026-10-25T02:05Z | ba1d926+ | final |  | 0 | 0 | 0 | 0 | 0 | 1 | 60 | 27 | 355.2 | 43.8 |

### mixed_signal

| When | Commit | Change | Settings | Unrouted | Shorts | Clearance | Edge | Keep-out | Completed | Vias | 90° | Bends/m | Seconds |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-25T00:36Z | ba1d926+ | second board: 13 reference designs, 2 layers |  | 2 | 0 | 12 | 0 | 0 | 1 | 33 | 37 | 426.2 | 33.3 |
| 2026-10-25T00:40Z | ba1d926+ | diagonals measured past fixed copper (grazes) |  | 2 | 0 | 12 | 0 | 0 | 1 | 33 | 37 | 426.2 | 35.0 |
| 2026-10-25T00:43Z | ba1d926+ | no step from one pad straight into another past other copper |  | 3 | 0 | 2 | 0 | 0 | 1 | 36 | 35 | 436.2 | 52.7 |
| 2026-10-25T00:45Z | ba1d926+ | grazes measured from every cell, own pads included |  | 3 | 0 | 0 | 0 | 0 | 1 | 36 | 35 | 436.2 | 45.2 |
| 2026-10-25T00:49Z | ba1d926+ | grazes: offenders as sets (own copper never bars) |  | 3 | 0 | 0 | 0 | 0 | 1 | 36 | 35 | 436.2 | 46.2 |
| 2026-10-25T02:04Z | ba1d926+ | final |  | 3 | 0 | 0 | 0 | 0 | 1 | 36 | 35 | 436.2 | 109.3 |
