# Changelog

## Unreleased

### The router on boards other people drew (2026-10-28)

- `evals/routing/import_board.py` reads any KiCad board (KiCad 8 and 10)
  as a routing case: its pads and their nets, its own net classes from
  the project beside it, its tracks and pours taken up. Three fixture
  boards are cases now, routed with 0 shorts and 0 clearance faults:
  `baseboard_2slots` whole (139 of 139, 45 vias against its designer's
  304, in 8 s); `mikoto`, a dense nRF52840 module, 142 of 165 with its
  two inner planes taken away; `microphone-board` 20 of 26, its two
  edge findings from pads its designer put 0.29 mm from the edge.
- `evals/routing/corpus.py` routes and audits every demo board of the
  KiCad install in turn, so a change is tried on boards of every kind.
  On KiCad 10's demos (16 boards up to 1500 pads; three larger ones
  skipped) it crashed on none and left 0 shorts and 0 clearance faults
  on every one, routing 3104 of 3401 connections and five boards whole.
  Most of the rest are on boards whose designers rely on pours
  (`StickHub` 70 of 207 unrouted, `RoyalBlue54L-Feather` 35 of 224): the
  import takes the pours up and Kairo adds no planes to an imported board
  yet.

### Ready for JLCPCB's assembly (2026-10-27)

- A ready board's fab folder also holds JLCPCB's BOM and placement file
  (`kairo.pcb.jlcpcb`): KiCad's placement under JLCPCB's column names,
  and the BOM with each kind of part once and its LCSC number, looked up
  in the parts catalogue by the design's `params.mpn`. A part with no
  number is left empty and named in `make_board`'s result
  (`jlcpcb_unsourced`, five at most, with the count), never guessed. No
  rotation correction is applied: JLCPCB's preview shows each part (A260).

### A prototype's rails: lights and links (2026-10-27)

- With `policy.prototype`, the review also asks for an indicator LED on
  each rail made on the board (a regulator's output is on it) that lights
  none, directly or through a resistor (`S_RAIL_NO_INDICATOR`), and for a
  0 ohm link or solder jumper where a regulator's output feeds chips
  straight (`S_RAIL_NOT_ISOLATED`), so a rail can be proven before it
  powers anything, and for a chip's spare I/O left open to be brought to
  a header or test points (`S_SPARE_IO_UNREACHED`; a drawn schematic's
  open pins now keep their names and types for it). All are suggestions
  (A259). On the BMS as a prototype:
  +1V8, +3V3 and REG1 light nothing, and +3V3 feeds its chips directly.

### Whether a project travels (2026-10-27)

- `project_summary` says whether a project opens the same on another
  computer (`portability`): each footprint library the schematic names
  that is neither one of KiCad's own nor in the project's own folder, and
  each 3D model given by an absolute path or by a variable another
  computer may lack. A relative model path is reported as unverified, not
  judged (A258). Kairo's BMS is portable; of the real boards, C13 keeps
  two libraries beside its folder and mikoto names models by KiCad 5's
  `${KISYS3DMOD}`.

### Impedance and voltage drop (2026-10-27)

- **`microstrip_impedance`**: Hammerstad and Jensen's equations (IEEE
  MTT-S 1980), within 0.5 % of Pozar's worked 50 ohm designs; for a trace
  of no thickness, as its notes say.
- **`stripline_impedance`**: Cohn's exact solution (IRE MTT 1954), as a
  ratio of two arithmetic-geometric means, so it stays exact for any
  width; 49.92 ohm for Pozar's 50 ohm example.
- **`microstrip_width` and `stripline_width`**: the width for a target
  impedance, by bisection to a part in 10^12; each gives its target back.
- **`trace_voltage_drop`**: a run's drop at a current, from the copper
  resistance `trace_resistance` already gave.
- Each runs on intervals for its enclosure (new interval `exp` and `log`;
  the means' ends are widened past rounding) and declares its directions,
  which the grid test checks. The `go` board step names the width
  calculations for a net that needs a set impedance.

### Licensed, packaged for each platform, ready to publish (2026-10-27)

- **Apache-2.0** (`LICENSE`; NOTICE keeps third-party licences, and now
  says how the KiCad libraries' licence covers the files Kairo writes).
- **One package**, `kairo-eda` 0.1.0 ("kairo" is taken on PyPI), built by
  maturin with the compiled loops inside as `kairo._fast`; the import and
  the command are still `kairo`. Wheels for Windows x64 and Linux x86_64
  and arm64 are built and checked here; `.github/workflows/release.yml`
  builds those and macOS (Apple silicon and Intel) on each platform's own
  runner, installs each on a clean machine and runs it, builds a Claude
  Desktop extension carrying them all, and on a tag makes a draft release.
- **The extension runs anywhere uv does**: `packaging/mcpb/build.py
  --wheels` makes one whose launchers pick the bundled wheel for their
  platform by file name, so no package of the same name on an index can
  stand in for it.
- **Docs for a public reader**: the README and getting-started cover
  installing on each platform; `docs/research.md` says how Kairo was
  researched and credits what it took. No public file names a local
  path or an internal note.
- Tests that read a recorded evaluation run skip, saying so, where there
  is none.

### Fewer tokens to load and to read (2026-10-27)

- **The tool listing** every session pays for is half what it was: 18,355
  characters against 35,857 for the 33 write-tier tools. Each tool's
  description keeps what it does, needs, cannot do and which tool to use
  instead, said once and short; the Tier line is gone (a tool's tier
  decides whether it is offered). The listing drops the library's
  generated titles, the null alternative of optional arguments and the
  output schema that said only "an object"; calls are still validated
  against the full schemas, and a test holds the listing under 19,500.
- **The server instructions** are 602 characters, against 945, every
  rule kept.
- **Results:** a list of three or more records that share their fields is
  sent as `{columns, rows}`, the fields once: `trace_net` on a 75-pin net
  went from 4,803 characters to 2,308, a net list from 3,772 to 1,912.
  The structured result keeps the records.
- **The design schema** leaves out the null alternative and `default:
  null` of optional fields: 17,218 characters against 18,035.

### Review rules for prototype boards (2026-10-27)

- **UART wiring.** `R_UART_TX_TO_TX` faults a net joining two transmit
  outputs, and `R_UART_RX_TO_RX` one joining receive inputs with nothing
  sending. They go by the pins' types as well as their names, so a CAN
  transceiver's TXD (an input the controller's TX drives) is sound, and
  a connector's pins, which may be named from the cable's side, are not
  read. Silent on the 16 real boards (none has two UART-named chip pins
  on one net) and on the BMS, whose CAN transceiver they leave alone.
- **Prototype policy.** `policy.prototype` marks a first board to be
  brought up; the review then suggests a test point on each supply and
  ground rail that has none (`S_RAIL_NO_TEST_POINT`). Off by default, so
  production reviews say nothing more. On the BMS it names 10 rails.
- Both follow a talk on prototype boards (`docs/research.md`), whose other
  points are recorded there for later.

### The hot loops compiled, and multi-unit parts read (2026-10-27)

- **Compiled loops.** `rust/kairo_fast` (Rust, built by maturin as a uv
  workspace member in the default `fast` group) holds three ports, each
  bit for bit the Python function it replaces, which stays as the
  reference: the router's A* search, its grazing diagonal steps, and the
  drawing annealer's pricing of a move. The whole BMS routes in 66 s
  against 1,509 s, with the same copper; the BMS drawn on one wired sheet
  takes about 58 s against about 155 s, byte for byte the same file.
  `uv sync --no-group fast` leaves the module out, and the Python runs.
- The router's per-cell maps and the annealer's spots are typed arrays,
  which the compiled loops read in place.
- **Tests.** `test_autoroute_compiled.py` and `test_arrange_compiled.py`
  run random boards and blocks both ways and compare everything,
  grazing tables included; each was shown to fail on deliberate breaks
  of the Rust. NOTICE lists the Rust crates, and a test keeps it in step
  with `Cargo.lock`; packages installed editable (Kairo's own) are left
  out of the uv.lock check.
- **Multi-unit parts.** kicad-cli 10.0.7 writes a `tstamps` per unit for
  a part drawn as several (a dual transistor), which the netlist reader
  refused, so `check_schematic` failed on such a schematic. Each unit's
  uuid is kept (`Component.units`); a board is not made for such a part
  until a KiCad-saved board shows which uuid its footprint's path names.
- The design-loop test counts 33 write-tier tools, `make_board`
  included. CI checks the Rust's format and lints.

### Support parts at their pins, routes taken first (2026-10-25)

- **Block check.** The board summary lists parts that serve only an IC
  of another block (`blocks`), each with its fix; the next step names
  them first when connections are left. On the BMS it finds the cell
  filter capacitors placed with the cells.
- **The go steps' board section** now says that blocks decide
  placement, what Kairo does itself, which summary findings to act on
  and in what order, and what `board.routes` is for; a test holds it to
  naming each finding.

- **Placement.** Each IC's own local parts (its block's capacitors and
  resistors on its pins) are seated at the pins they serve after the
  rest is placed, round an IC that kept room for them; banks still form.
  On the BMS the gaps at the monitor IC closed: 1 connection left
  against 5, 8,128 mm of track against 8,785.
- **Routes taken first.** `board.routes` names nets to route before the
  rest, in order, with their layers, each then fixed: routing by hand,
  with Kairo still choosing the copper. Measured, it does not close
  gaps by itself (the others go round), so it is for what the router
  cannot know.
- **Mending.** Each connection still missing at the end has the other
  nets' tracks near it taken up, is routed, and they are joined again;
  kept only if KiCad finds fewer missing. Five at most, ten passes
  each; the summary's `mended` says which were tried and joined.
- **Detours.** The summary lists nets routed the long way round.
- **Fixes.** Same-net vias at one spot are written once.

### Labels placed, narrowed copper checked for current (2026-10-25)

- **Silkscreen labels.** Each reference is placed clear of pads, other
  print, other labels and the edge (`kairo.pcb.silk`), or moved to the
  fabrication layer where nothing fits. On the BMS, about 300 silkscreen
  findings became 18 (edge connectors' outlines, which the maker trims).
- **Current.** A net may state the most current its tracks carry;
  narrowed copper is checked with IPC-2221 and the summary says pass,
  fail or unverified for each narrowed net.
- **Tried and removed:** extra room in placement between parts far apart
  in voltage (15 connections unmade against 6).
- **CI.** The CHANGELOG's paths to docs pages are code, not links, so the
  docs build (which includes the CHANGELOG) passes again.

### The router checked on a second board, and its traces tidied (2026-10-25)

- **Cleaner traces.** A leg keeps its 45 or 90 degree heading when its
  end moves onto a pad's true axis (a short jog takes up the offset):
  the odd angles near pads are gone. Tracks keep 1.5 mm off the board
  edge where there is room, and a tidy pass pulls finished detours
  tight: 3 % less track and 22 fewer vias on the BMS.
- **Two clearance holes closed**, found by a second benchmark board
  (`mixed_signal`, 75 parts on two layers, built from the reference
  designs): a step from a QFN's exposed pad straight into its pin past
  the next pin, and 45 degree steps grazing pad corners. Both cost
  nothing on the BMS.
- **A gap, never a fault.** The router audits its own copper exactly
  after legalising; a run still at fault is dropped, its net joined
  again with every route a wall, and anything still at fault reported
  unrouted. The tidy pass checks clashes from both sides, as the rescue
  does.
- **Benchmarks.** `evals/routing/freeze.py` freezes any built project's
  routing input, `crop.py` cuts a window out of a case; `bms_u1_window`
  routes in about 15 seconds against 15 minutes for the whole board.
- **Removed:** the `stitch_after` option and the `signals` routing
  stage, both measured with no gain. The rejoin stage was measured and
  kept: it joins 6 of the 7 connections the fill leaves apart.
- **Structure.** `kairo.pcb.autoroute` is a package in layers (`grid`,
  `search`, `negotiate`, `copper`, `board`); the split was checked
  byte-identical.
- **KiCad was not applying the net classes.** A new project's
  `net_settings` block was version 0, and KiCad 10 ignores the class
  patterns there: boards were filled and checked at the Default 0.2 mm,
  so earlier "nothing blocking" results (and the Freerouting comparison)
  were judged without the classes. `apply_rules` now writes version 4.
  Judged properly, the pre-route stitching vias were too near other
  nets' pads (now they keep the class clearance) and fanout stubs ran at
  any angle (now straight or 45 degrees). The BMS: 0 blocking findings,
  3 connections unmade.
- **Spacing by voltage.** A board may name an IPC-2221 column
  (`spacing: B2` for bare); with each net's voltage range stated, every
  pair of nets keeps the spacing for the voltage between them, in the
  router, fanout, stitching and KiCad's DRC (a `.kicad_dru` rules file).
  Nets with no stated voltage are listed (`spacing_unchecked`), not
  passed. On the BMS, 153 pairs of tracks or vias closer than B2 asked
  became 0, with 6 connections unmade.

### Kairo's own router, now the default (2026-10-24)

`kairo.pcb.autoroute` routes a board with no outside program, and
`make_board` uses it by default (`engine="freerouting"` still works). It
was designed from published methods (`docs/router-design.md`,
sources in `docs/research.md`):

- A* over an octilinear grid, with bend costs that favour 45° and a
  preferred direction per layer;
- negotiated congestion (PathFinder), ripping up only the contested
  connections;
- a completion pass with other routes as walls, then a rescue pass that
  lifts what blocks a dropped connection and keeps the trade only if it
  is clean;
- pad escapes along each pad's true axis, necked down between
  fine-pitch pins;
- line-of-sight straightening, then an exact legalising pass that necks
  each track to the room it has.

Every change was measured on frozen cases (`evals/routing`, ledger in
`docs/router-log.md`, rules 1 to 25). On the BMS the
whole board now has no blocking DRC finding and 1 connection unmade, in
about 6 minutes; Freerouting left 14 clearance faults and 24 unmade in 27.
(Both judged without the net classes; see 2026-10-25.)

Placement and pours:

- A fine-pitch part keeps 2 mm in front of its pins free, and the
  ground fanout leaves those corridors open.
- A part on an edge faces out on the side its courtyard overhangs, so
  the USB-C receptacle is no longer placed backwards.
- A pour can be stitched after routing (`stitch_after`, off: no
  consistent gain). Stitches keep a hole's spacing from through pads.

The summary gains `placement` (how near each local part sits to its
IC pin) and `narrowed` (wide nets the router drew narrower than their
class, for a current check).

Fixes found on the way:

- The rescue pass checked a clash from one side only, and a via beside
  an untouched net's track got through (3 clearance faults on the
  re-placed BMS).
- A custom pad (a power package's drain) is read at the extent of its
  drawn copper, not its anchor's: the TOLL drain is 8.0 mm wide, not
  2.9 mm.
- A paste aperture (a pad on no copper layer) is no longer read as
  copper; it walled in 9 FET gates.
- Shared geometry (distances between points, segments, boxes and
  polygons) is one module, `kairo.pcb.geometry`, in place of five
  private copies.
- The `test_sheets` lambdas are now typed.

### A board in one call: make_board (2026-10-23)

New `kairo.pcb` package and `make_board` write-tier tool. A built
schematic becomes a placed, poured, routed and KiCad-checked board, and
the answer is a short summary rather than a raw DRC report. Every step
uses KiCad's own forms, taken from its 10.0.7 demos:

- The board file is written from KiCad's netlist, with schematic parity
  clean (A212).
- Placement works from constraints (A213). Regions are sized by need
  (A216). Edge parts stand on their body and a module's antenna keep-out
  hangs off the board (A217). Parallel parts form banks, and back-to-back
  FETs form two rows (A218).
- Pours are anchored to their blocks and stitched between layers. Ground
  pads are fanned out to the plane before routing, keeping each class's
  clearance and staying out of keep-outs.
- Routing uses Freerouting 2.5, run headless and offline on a Java 25
  from the tools folder (`--tools-dir`), in two stages. The first is
  given the pours as planes. The second is given the islands KiCad's fill
  left, with the first stage's wiring kept, and joins them. A run's time
  limit follows its passes.
- Net classes and the maker's rules are new design fields
  (`board.net_classes`, `board.rules`, A219). They reach the router
  (class-to-class clearances included), the fanout and the `.kicad_pro`,
  so KiCad's DRC checks against them.
- The summary separates blocking findings from a land pattern's own
  findings and from cosmetic ones, groups unrouted connections by net and
  counts bends (A220). Fab files (Gerber, drill, CSV placement, BOM) are
  written only when nothing blocks. The places are kept, so a re-route
  does not re-place.

`design_schema` now leaves the board out of part `design` and gives it as
part `board` (A221). Writing a schematic no longer carries the board's
5,225 characters. The design part is 18,875 characters, down from 20,708.
The `go` steps gain a board step. `docs/board-layout.md`
records the rules this board taught.

### Designs on several sheets, drawn as a drawing office would (2026-10-23)

A design may now say which blocks go on which sheet (`sheets`) and what
its title block says (`title_block`: company, revision, date, up to four
notes). Such a design is built as one hierarchical KiCad project (A210):
a root sheet that is a block diagram, one sheet symbol per child with its
ports as pins, neighbouring sheets joined by straight wires and the rest
by labels; and each child drawn wired for a person, its outgoing nets
ending in hierarchical labels. Power flags are chosen once for the whole
design and `#PWR` is numbered per sheet, so ERC sees one flag per net.
KiCad's netlist of the whole hierarchy is compared with the design (L3)
and its rule check run on it (L4), as for one sheet. Every form written
was taken from KiCad 10.0.7's own save of its cm5_minima and
complex_hierarchy demos. Wired blocks are also compacted after the
anneal (A211): a cluster held only by the pull to its block's middle
used to be left far out.

On the 13S11P BMS (C.5, 251 parts) this gives nine sheets: the root and
cells 1 to 6, cells 7 to 13, the monitor, the FET switch, terminals and
protection, the supply, the controller and CAN. All pass L0 to L4 in
44 s; 4 texts are hidden, where the one-sheet drawing hid 6. Not done:
the FET switch and controller sheets come out A2, not A3, and the root
A2 as well; sheets inside sheets, and one sheet used twice, are not
built.

### A buck whose switch pin is typed output, and a rail behind a diode (2026-10-23)

Found designing a carrier board for the LicheeRV Nano W through Kairo.
The power tree found a step-down stage only when the regulator's switch
pin was typed power output; KiCad types the TPS54360's SW pin output, so
its +5V rail had no source and every check of the stage was unverified.
The switch pin is now found among output pins too (A209); the inductor
is what marks the stage. A new calculation, `diode_output`, gives a rail
fed through a series diode (Vin less Vf), so such a rail's range comes
from Kairo and not from arithmetic by hand. Not done: the tree does not
yet carry a downstream rail's current back through a series diode to the
rail that feeds it.

### KiCad 10.0.7 measured (2026-10-22)

This machine now runs KiCad 10.0.7. The whole suite passes on it (1,099
passed, 9 skipped). Its ERC JSON report gives item positions at their
true size (KiCad issue 25582): on a copy of `artix-dc-scm` the label
`ETH_~{RESET}` is at (189.23, 121.92) mm where 10.0.6 gave (1.8923,
1.2192), and all 1,443 items found in both reports are exactly 100 times
their 10.0.6 values. No tool reports positions, so A44 needs no code; the
length note already stops at 10.0.7. File formats are unchanged:
`sch upgrade --force` still writes schematic version 20260306, and the
installed libraries use the versions Kairo already reads.

The first 10.0.7 install on this machine left KiCad 10.0.6's
`_eeschema.dll` in place, because a test run was calling kicad-cli while
the installer replaced files; every schematic command then failed with
"procedure not found". A clean reinstall fixed it. Never run the tests
during a KiCad install.

### Parts in parallel drawn as a bank (2026-10-20)

In a wired drawing, parts drawn alike that share their nets pin by pin,
each but for a net of its own, are now placed as one piece (A208): the
power FETs of a switch, clamps across one rail, capacitors in parallel.
Each member's follower, the one other part on its own net (a FET's gate
resistor), sits beside it in the same place, its pin facing the
member's on one line. The row runs across the way the shared pins face,
side by side when they face up and down and stacked when they face the
sides. Parts whose own nets all lead to one part, such as capacitors
each on a pin of one chip, are left beside their pins.

On the 13-cell BMS the eight power FETs, scattered by the annealing
before, now draw as two columns of four on the FET_MID bus with every
gate resistor beside its FET, and the eight TVS diodes on B+ as one row
where they were a column the height of their block. All 16 reference
designs and the BMS still come out netlist-equal and ERC clean under
KiCad; the BMS hides as many texts as before (6: Q15's value and a
second value of J6 in place of R56's and D30's references), and drew in
96 s against 117 s, since a bank is one piece to the annealing.

### Which build is serving, a call log, two review checks, and wires between blocks (2026-10-20)

At the owner's request, after Konnect (mixelpixx, AGPL-3.0) was read
and compared with Kairo: its best ideas taken without its code, then
the work carried on. Only Konnect's documentation was read; how each
idea became Kairo's own design is summarised in `docs/research.md`.

**`server_info`** (A203) says which build is serving: version, commit,
the package's folder, when it started, and `restart_needed` when any of
Kairo's source files changed on disk after the process started, which
a client cannot otherwise see. Started from the Claude Desktop
extension, whose launcher now passes its own folder, it also compares
the extension's manifest with this build's tools and says `rebuild`
when they differ. It reports where kicad-cli, ngspice and Xyce were
found and their versions. Anything it cannot observe is `unknown`.

**`recent_calls`** (A204): every call, refused or not, is logged with
its seconds, outcome, refusal code and the start of its arguments, in
`calls.jsonl` in the cache folder; the tool returns the latest and each
tool's counts and failures by code, so a model can see that it made
the same mistake before.

**Two review checks** (A205, A207), each tried on the twelve real boards
before it was kept. `S_SUPPLY_IN_NO_BULK` asks about a supply that comes
in through a connector and feeds chips with no capacitor of 10 µF or
more (Analog Devices MT-101); it names one rail on the real boards.
`S_CONTROL_INPUT_UNSET` asks about a reset or enable input with nothing
drawn to set its level. It was first written for any input with only
capacitors beside it, and all ten pins it named on the real boards were
sound (soft-start, AC-coupled and crystal inputs), so it was narrowed;
it names none now. A test point in KiCad's Connector library was first
taken as a way in for a supply; its test failed before that was fixed.
Konnect's regulator-output and test-point checks were not taken: the
first is already `regulator_stability`, the second has no source.

**Wires between neighbouring blocks** (A206). A net spread over the
sheet was joined block by block and its blocks tied only by labels.
Pins of two blocks within 100 mm of each other are now joined too, so
the BMS's cell chain draws as one ladder from cell 1 to cell 10 and the
cell connector is wired to its first cells; its labels fall from 211 to
198, and KiCad finds the netlist equal and ERC clean.

### Two review checks, Diode-library clamps, and wired drawings placed for wiring (2026-10-19)

From a second review of the 13-cell BMS designed through Kairo (its
design document), at the owner's request.

**Two checks the BMS review of 2026-10-16 had to make by hand.** The
review now faults a back-to-back MOSFET switch that something conducts
around while it is off (`R_SWITCH_BYPASSED`, A197): it groups FETs of
one channel into banks, finds pairs sharing their drains or sources,
and searches for a path between the outer nets through resistors,
diodes forward and other FETs' body diodes. Run on the BMS's revision
C.2 it names the fault that review found, with its path: `PACK_P -> R81
-> PRE_D -> Q25 (its body diode) -> +BATT`; on C.3 and later it is
silent. A path above 1 kohm is a leak and only suggested. It also asks
about a current-sense resistor with off-board wiring on both sides
(`S_SENSE_RESISTOR_SHARED`, A198), which is how C.2's CAN ground ran in
parallel with the shunt. Tests for each, with the sound case beside it.

**Clamps drawn with KiCad's Diode library count** (A199). The pin check
knew a clamp only by the Device library's symbols, so a BAT54 drawn as
`Diode:BAT54W` beside a pin was ignored and the pin bounded by a
Zener's 0.9 V instead of the Schottky's 0.32 V. Any two-terminal part
with pins A and K is now a diode, or a Zener if it states `v_clamp`.
Its test failed before the change.

**Wired drawings are placed for wiring** (A200 to A202). Until now the
wired style placed parts as the labelled one does, in columns, and
joined them afterwards, so wires wandered across the sheet. A new
module, `kairo.kicad.arrange`, places each block's parts by simulated
annealing for short, straight, uncrossed wires: parts of three pins or
fewer are turned so the pins that meet face each other, a capacitor to
ground stands with its ground below, and a move that sets a pin facing
a neighbour's on one line finds straight wires the other moves miss.
Blocks are seeded by their structure, so channels built alike are drawn
alike, and each structure is arranged once. Each block is framed under
its name, and the frames are packed in reading order onto the smallest
sheet that holds them. Supplies with no power symbol of their name are
now wired too, with a global label, instead of a label on every pin.
Found on the way, both connectivity faults KiCad's netlist caught: a
wire's label anchored where another net's wire passed joined the two
nets, and a connector's ground symbol fenced off its neighbour pin's
way out. Measured on the BMS (283 parts, A0): netlist equal and ERC
clean; arrangement takes about 10 s a try after overlap was checked by
rectangle rather than by grid cell (it was 40 s); 6 reference or value
texts find no clear spot and are hidden. All the reference designs
tried (MCU, USB-C sink, charger) come out netlist-equal and ERC clean.
Tests: `tests/unit/test_arrange.py`.

### Refusals that say what to do, progress, and a bounded project search (2026-10-16)

Three ideas taken from HeyPCB's MCP proxy (MIT; see `docs/research.md`), written
for Kairo rather than copied.

**A tool that refuses now says what clears it** (A191). It answers
`{ok: false, code, error, fix, retryable}`, as structured content and
the same JSON as text, instead of the MCP library's "Error executing
tool ...: E_CODE: message". `fix` tells the model what to do next: the
tool or argument to use, or to stop and ask the user. `retryable` is
true only where the same call may work again (a file changed under the
call, a download that failed). The fixes live in `kairo.refusal`, one
for each of the 74 codes a tool can refuse with; a test finds every
code raised anywhere in Kairo, and a grounding finding's, and fails if
one has no fix or a fix has no code. Arguments the MCP library itself
rejects before a tool runs still come back as its plain text.

**A long call tells the client it is still working** (A192). Every
tool runs on a worker thread while the server sends a progress
notification every 5 seconds to a client that asked for them, with the
seconds so far and the stage the work last named: the first library
index, each layer of a build, the simulators. A cancelled call is
not abandoned halfway; it finishes and its answer is dropped.

**`list_projects` searches a bounded number of entries** (A193). It no
longer enters hidden folders (`.git`, KiCad's `.history`),
`node_modules`, `__pycache__`, KiCad's `-backups` and autosave folders,
or links to folders, and it stops after 200,000 entries, saying so in
`searched_all` and a note. KiCad's own install (30,523 entries) still
lists the same 77 projects, in 0.1 s instead of 0.3 s.

Each of the four behaviours has a test that fails with its change
undone.

### A drawing a person can follow, beside every project (2026-10-16)

**Every built project now has a wired drawing too** (A194), in its
`wired` folder: the same design with its pins joined by wires, where the
project's own sheet, the one every check is built on, has a label on
every pin. The owner asked for both after reading the BMS board's
labelled sheets. Supplies and grounds keep their power symbols; a signal
net is joined whole when its pins lie close, block by block when they
are spread, and named by one label on its own wire; what the router
cannot join keeps its labels. The drawing is checked as the project is,
in a scratch folder first (layout, KiCad's netlist equal to the design,
ERC clean), and written with the project only if it passes; otherwise a
warning, `W_WIRED_NOT_WRITTEN`, and the project alone. `apply_design`
returns its path as `wired_drawing`. All sixteen reference designs come
out equal to their designs and ERC clean when wired, and so does the
259-part BMS on one A0 sheet (1,128 wires, 99 junctions; 12 of its
reference and value texts found no clear spot and are hidden).

**The router** (`kairo.kicad.routing`, A195) joins each group's pins by
A* search on the 1.27 mm grid, one pin at a time to the tree so far. A
wire never ends, bends or runs along another net's, nor touches another
net's pin or a symbol's box; it may cross another net's wire only
straight through, which KiCad does not join. Its first fault was found on
the charger reference design and has a test that fails without the fix:
freeing a pin's stub freed its ends and not the point between.

**Blocks wrapped into bands** (A196, slice 1 of the multi-sheet
proposal): a design too wide for one row on A0 is laid in bands on the
smallest sheet that holds them; anything that fit before is drawn as
before. A wider retry to place text that no longer fits a sheet now keeps
the closer drawing and reports the hidden texts, where it used to fail.

### Checks found missing by a battery management board (2026-10-16)

A 13-cell, 100 A BMS was designed through Kairo on 2026-10-15 and 16,
and a list of what Kairo could not check was kept. These close the
gaps that are about checking.

**Pins against their absolute maximum ratings** (A185, gap 19). A part
may state `pin_ratings`: for each pin by name a `max` and or `min`,
measured from another of its pins (`to`, such as `VSS` or a FET's `S`)
or from ground. Every rated pin is checked against the range its net
can reach (`kairo.verify.voltages`): a stated range and surges, a
rail's range over its tolerance, or, for a net with nothing stated,
the range of the nets feeding it through resistors and diodes, narrowed
by a Zener, TVS or diode with a stated `v_clamp` (a new parameter) or
`v_f`. A clamp straight between a pin and its reference pin bounds
their difference whatever drives the pin, so a gate Zener proves a
FET's gate-source rating. A net that leaves through a connector needs
its `surge_max` and `surge_min`; a net a chip's output or supply pin
can drive needs its range stated. A pin that its net is shown to take
past its rating fails; one that only might is unverified. On the
BMS this reproduces what was found by reading TI's and Libre Solar's
boards: with its 75 V Zener taken out, the BQ76952's PACK pin, fed
through 2 x 5.1 kohm from a pack terminal that surges to its TVS's
96.8 V, fails its 85 V rating; with the Zener in, it passes at 78.75 V.
Faults 1, 6 and 8 of the second agent's schematic are of this kind.
A net reached through one resistor with only chips' inputs on it
follows the far net at rest (`followers`), so a gate Zener in front of
a gate resistor holds the gate. A test point is not a way off the
board: KiCad keeps its own in the Connector library, which the first
run on the BMS read as a connector (found there, with a test that fails
without the fix). Tests: `tests/unit/test_pin_ratings.py`.

**A net's range** (A186, gap 12). A net takes `min` and `max` instead
of a tolerance (a pack of 32.5 V to 54.6 V had to be written 43.55 V
+/-25.4%), and `surge_max` and `surge_min` for transients.

**Programmed settings** (A187, the BMS's revision C double check). A
`setting` requirement states a value a chip is programmed with, its
step, offset and accepted range, and fails when the chip cannot hold
it, naming the nearest values it can (`kairo.calc.setting`). Revision
B of the BMS relied on a 4.22 V cell overvoltage threshold, which in
50.6 mV steps is 4.1998 V or 4.2504 V, and on overcurrent delays of 1
to 2 s where the chip allows 10 to 426 ms.

**A feedback pin straight on its rail** (A188). The review calls a
fault `R_FEEDBACK_NO_DIVIDER` where a pin named FB, VFB, ADJ or
FEEDBACK sits on a power net whose voltage is not its part's `v_ref`,
and suggests checking where no `v_ref` is given. The second agent's
LM5164 had FB on its 12 V output, which would regulate to 1.2 V.

**Power-stage calculations** (gap 7): `parallel_switch_loss` (the
worst of N switches sharing a current, with an imbalance),
`precharge_energy`, `precharge_peak_power` and `precharge_time`, from
TI's TIDUF73 section 2.2.1. Joules are a unit. The short-circuit
energy of a loop's inductance is not added: no source that states
it could be read.

**A rebuild from a new process is no longer refused** (A189). Each
write records the files' digests in the project's `kairo/written.json`,
and a later session may write over a file that still holds exactly
what Kairo wrote. A file someone changed is refused as before. Backups
are numbered on from the highest kept, where a later session's first
backup used to overwrite `0001`.

**Inputs in the shapes that were sent** (A190, gaps 14 and 15):
`calc` reads `10k 1%` and `3.3V +/-2%`, and its error shows both forms;
`find_part` takes a package as a number; `read_datasheet` takes pages
as a number or text such as `5-7`.

Not done from the same list: several sheets (gaps 1 and 2, then
awaiting a design decision); two
pins of one name to different nets (gap 17), which needs a pin number
and so the owner's word on the rule that a model never gives one; a
project's own footprints (gap 18); a chip with a current on each of two
supplies (gap 8); more than one output capacitor in ripple and
stability checks (gap 11); pull-up currents bounded by V / R (gap 9).

### Derived symbols carry their base's fields (2026-10-15)

**A symbol that extends another is embedded with every field it
inherits** (A184). A design with Diode:1N4148W or Transistor_BJT:BCX56
was written, but KiCad's rule check called each copy different from
the library (`lib_symbol_mismatch`). KiCad 10 flattens a derived symbol
from a copy of its base, so the base's fields the derived symbol does
not set come with it: the diode's and transistor's `Sim.Device` and
`Sim.Pins`, private fields, a `Manufacturer`, and the base's keywords
and footprint filters when the derived symbol has none. The earlier
`ki_locked` fix was one case of this. Measured with kicad-cli 10.0.6:
its rule check passes copies of 1N4148W, BCX56, ADS7867, W6100-L,
10M08SCE144, RP2354A, MAX15062B and LM358 made this way and failed all
but LM358 before; a scratch library showed the keywords rule. The
order (in the base's order, before the derived symbol's first `ki_`
entry) is the one KiCad 10 wrote for Diode:1N4148 on the pedalboard
board, which the embedding now reproduces: 76 of 89 real copies, 7 of
the 8 derived ones, up from 75 and 6. Private fields are now matched
by name too. Three new tests (a unit test, the real-board embedding
and a build of both parts with KiCad's rule check clean) fail without
the change, and so does the corrected unit test that said a derived
symbol's keywords stay empty.

### References kept off pin numbers (2026-10-14)

**The writer no longer places a reference or value over a pin number**
(A181). The hold-up and I2C sheets drew each connector's reference on
its pin 1 number: a symbol's box counts its pins but not their text,
and on a connector's top pin the number stands above the box, where
the reference goes. Where KiCad draws pin numbers and outside pin names
was measured, not taken from its code: from kicad-cli's SVG on KiCad
10.0.6, a number is centred along its pin above a horizontal pin (left
of a vertical one) unless the symbol draws its names outside, when it
goes below. `pin_text_boxes` gives those boxes; the placer avoids them
and layer L2 reports any text over one (`E_TEXT_OVERLAP`). A new test
draws every sheet of two real boards and finds each of more than 300
pin texts' ink inside its box; another fails on the reference designs
with the placer change reverted (J2 of `buck_refusal` first), and a
seeded L2 case fails without the check. The reference run
(2026-10-15T022537Z) is 16 of 16, and `rescore` reproduces it.
`drawn_edit` does not use this yet.

**The model harness passes on Ollama's reason when a call fails.** A
run with a 27b model stopped on "HTTP Error 500" alone, while Ollama's
reply said the model server had run out of memory loading it and
crashed. `evals/model_run.py` now raises with that reply; a test with a
stand-in for the network fails without it.

**A search finds a part by the lib_id Kairo gave, and by the parts of
an underscored name** (A182). A local model asked `find_footprint` for
`LED_THT:LED_D3.0mm`, which a search had just returned, and for
`LED_SMD_0603`, and got nothing both times: the library's nickname and
name are kept apart, and words were split only at spaces. A query word
written `<nickname>:<name>` is now its two halves, an exact lib_id
comes first, and a search that finds nothing tries the query split at
`_`, `-` and `:` before leaving words out, saying so in its notes. On
the real libraries every one of the model's empty searches now finds
footprints (`LED_SMD:LED_0603_1608Metric` for `LED_SMD_0603`). A new
test fails without the change.

**A design or a list of operations may be sent as JSON text** (A183).
The same model, once it had its footprints, sent `apply_design` its
design as a JSON string; the tool's own argument check refused it as
"Input should be a valid dictionary" before Kairo saw it, five times,
and the model gave up. `accuracy_report`, `analyze_power_tree`,
`plan_design`, `simulate` and `apply_design` now take a design as an
object or the text of one, and `edit_design` and `edit_schematic`
their operations as a list or the text of one. Text that is not JSON
is refused as `E_JSON_INVALID`, naming the fault, its character, line
and column, and the text either side (the model's had one closing
brace too many). Two new tests fail without the change: the tools read
text as the object it holds, and the server's tool list lets text
through.

### The hold-up reference design, on a maker's measured DC-bias curve (2026-10-14)

**`holdup_mlcc`, four 22 uF X5R capacitors holding a 5 V rail through a
brown-out, is the sixteenth reference design** (M1.9), and the first
to rest on a capacitor maker's own DC-bias curve: Murata's measured data
for GRM21BR61A226ME44, read from the part's page (SimSurfing data, 201
points from 0 to 10 V; the test conditions checked in Murata's
measuring-conditions document, page 8). The curve is kept in
`evals/parts/` (A180), which the reference run now reads, and in
Kairo's own parts folder. At the rail's highest 5.25 V each capacitor
is at least 6.6 uF (44% of its 0 V value, less 20% and X5R's 15%), so
the bank holds 50 mA above 4 V for at least 396 us: 300 us passes,
500 us is refused, and marked values would have claimed 1.32 ms.
Without the curve the check stays unverified. The points were checked
row by row against the page's own capacitance column. A unit test pins
all three outcomes to the kept curve.

### The charger reference design, and power symbols turned when crowded (2026-10-13)

**`charger_bq24075`, a one-cell charger built round TI's BQ24075 from
its datasheet (SLUS810N), is the fifteenth reference design** (M1.9).
Its charge current and input limit are checked by a new requirement,
`programmed_current` (A178): a current a chip sets as `I = K / R` from
one resistor to ground on a pin, with K the datasheet's factor stated
as `k_charge` or `k_input` with its spread. The design's 1.78 kilohm
and 1.62 kilohm resistors give 443 mA to 553 mA and 917 mA to 1072 mA
over KISET's 797 to 975 A*ohm, KILIM's 1500 to 1720 A*ohm and the
resistors' 1%. Anything else on the pin's net, a resistor to a rail, or
a factor with no spread leaves it unverified; there is no default
spread for a chip's factor. Every figure in the design was read back
from the fetched datasheet. Tests are worked by hand and fail without
the lone-resistor rule.

**The writer turns a power symbol to point along its stub when,
upright, it would face a connected neighbouring pin** (A179). The
charger found this: EN1 tied to ground and EN2 to the input sit one
pitch apart, and a ground hanging from one and a supply standing on the
other were drawn on top of each other, so the supply's name was hidden.
No stub length cures two symbols facing each other. The I2C sensor's
sheet changes too: the ground on A0, which hung over A1's stub, now
points left. How KiCad draws a
turned symbol's name was measured with kicad-cli's render of KiCad
10.0.6 and its re-save: a field is drawn at its own angle plus the
symbol's, centred on the point written. A new writer test fails with
either half reverted. The design schema grew by 1,061 characters for
the new requirement; its budget is now 14,600.

**A worst-case search whose simulator run fails now says why.** In one
full test run the filter ripple test came back `unverified` with "a
run confirming the worst case failed" and nothing more; it passed alone
and with its file, so the cause is not known. The verdict now ends with
the failed run's own reason (a timeout, the simulator's error). A unit
test with a stand-in run fails without it. Two integration tests still
counted the tools from before `save_facts` and `fetch_datasheet`, and
the text tests' libraries lacked the charger's; both are fixed.

### Kairo fetches datasheets itself (2026-10-13)

**`fetch_datasheet` (read tier) fetches a part's datasheet PDF from its
maker and keeps it** (A177), so nobody has to download one by hand:
the owner called having to fetch each sheet a flaw. The link comes from
the call, from the KiCad symbol's own Datasheet field (KiCad's libraries
carry one for most chips: `Battery_Management:BQ24075RGT` links TI's
`bq24075.pdf`), or from the parts catalogue's listing. The PDF goes to
the `datasheets` folder of Kairo's cache, which `read_datasheet` already
reads, with its link, where a redirect took it, its SHA-256 and the day
(`index.json`); a link fetched before is not fetched again unless the
file has changed. What is not a PDF (a sign-in page) is refused, and so
is anything over 50 MB. Tried on TI's BQ24075 (3.7 MB) and Microchip's
MCP73831 (0.8 MB) datasheets, both straight from the symbols' links.
Tests run against a stand-in for the network and fail without the
reuse and the PDF check. The protocol's step for a chip now says to
fetch, then read. 30 tools at the write tier.

### A drawn project's facts kept with it (2026-10-13)

**`save_facts` (write tier) keeps what was stated about a schematic a
person drew in the project's own `kairo/facts.json`** (M2.4, A176), and
`check_schematic` and `simulate_schematic` use it without being given
it again; facts given in a call are laid over the kept ones and say so
(`kept_facts` in the result). The owner left the place to Kairo: what
is one project's stays with it, beside the backups already in its
`kairo` folder, and what serves every project stays in Kairo's folder.
The file is written through the transaction module, and facts that do
not fit the drawing (a net it does not have) are refused with nothing
written. A new test on a real board saves the facts once, checks with
none given and with a load laid over them, and fails without the kept
file being read. The server has 29 tools at the write tier.

### KiCad's own simulation fields for a registered model (2026-10-13)

**A part with a registered model is written with `Sim.Library`,
`Sim.Name` and `Sim.Pins`** (M2.1, A175), so KiCad's simulator runs
the model Kairo ran. The form was measured, not assumed: KiCad's LM317
demo re-saved with `kicad-cli sch upgrade` shows the three fields as
KiCad 10 writes them, and kicad-cli's SPICE netlist of it shows how
`Sim.Pins` orders a subcircuit's nodes; a symbol pin listed twice feeds
both nodes, which a model with two ground nodes (TI's LM1117) needs.
`Sim.Library` names the registered file where it is, so KiCad reads the
bytes whose hash Kairo checked. A part whose model ties a node to ground
inside the registry, needs a pin the design leaves open, or whose file
has changed gets no fields and a warning, `W_NO_SIM_FIELDS`. A new test
builds the regulator example with a registered model: KiCad's SPICE
netlist includes the file and gives `XU1 VIN_5V +3V3 GND GND TESTREG`,
and KiCad's re-save leaves the fields byte for byte as written. It
fails without the fields. `apply_design`, `edit_design`, `plan_design`
and `verify_design` use the server's `--models-dir`.

### A maker's PSpice model in two simulators (2026-10-13)

**Xyce now runs TI's LM1117 model, and agrees with ngspice** (A123,
A173, A174). Recorded on 2026-10-09 as reading only after three syntax
edits and then not converging; measured today, Xyce 7.10 needs two
edits, both of spelling, and one change of how it chooses time steps.
`kairo.spice.translate` makes the edits: a `TABLE` written PSpice's way
gets Xyce's `=` and loses its outer brackets, and `IC=` on a capacitor
or inductor is dropped, because ngspice ignores it without `UIC` while
Xyce holds the capacitor there through the operating point (TI's
soft-start capacitor, held at 0 V, gave an output of 0.9 mV at rest).
The registry writes the rewritten file once, named by the original's
hash, into the models folder's `.xyce` folder, and only Xyce reads it;
the part's note lists each edit by line. In a transient, Xyce's error
estimate stopped at TI's under-voltage switch, an ideal step at 30 us,
on every setting tried; with a PSpice model in the circuit it now
chooses steps by iteration count (`ERROPTION=1`) with a Newton current
tolerance 1000 times the profile's, and every such run says so among
its convergence aids. Measured on the reference LM1117 design, ngspice
then Xyce: at rest 3.29122 V and 301.284 mA in both; supply rising in
100 us, output rise time 129.413 us and 129.415 us, settling 306.008
us and 306.211 us; plugged in live, the input's peak 8.87956 V and
8.87935 V. Every number still says `unverified`, as it must: the model
has not been compared with PSpice, which wrote it. What changes is that
a second engine now checks ngspice's arithmetic on it. A new test with
a model of Kairo's own in PSpice's spelling fails without each of the
three changes.

### Support parts beside the pins they serve, and text off no-connect crosses (2026-10-12)

**Support parts are drawn beside their pins** (A68, the plan's rule
for placing inside a block). In a block, the first part of more than
two pins is the main part; a two-pin part of the block that shares a
net other than ground with it (a decoupling capacitor, a pull-up, a
feedback resistor) is drawn beside the main part's pin on that net, on
the side the pin leaves and as level with it as it can be without
rising above the main part or falling below it. Others stay in the
block's column as before. Seen in KiCad's plots: the MCU's reset
pull-up now stands beside its RESET pin, the buck's boot capacitor
beside BOOT, the regulator's output capacitor beside its output.
Every reference design still has no overlapping text and builds
through KiCad's checks. The cost: a block's column is wider by its
side column, so the test's 80 extra capacitors need A1 where they
needed A2; 12 still fit A4.

**A fault found by looking at a plot, fixed:** text could be placed
over a no-connect cross. The cross sits on a pin's end and reaches past
the body's box, which was all the text placer kept clear, so the
ATtiny's value covered two of its pins' crosses, and the buck design's
U1 reference another. Each cross is now kept clear, and the layout
check reads them too. A new test reads the written files themselves,
since the writer's own record had left the crosses out; it fails
without the fix.

### A supply budget, current through a converter, and hold-up behind one (M1.7, 2026-10-12)

**`max_rail_current`** is a new requirement: a rail draws at most a
stated current over the tolerances, everything it feeds included, for
a supply's budget (500 mA from USB) or a fuse's or connector's rating.
It fails if the rail's nominal current is over it, or its greatest; it
passes only if the greatest is worked out and within it.

**A switching regulator's greatest input current** is now worked out:
`Iin = Vout * Iout / (efficiency * Vin)` with the output at its
highest, its rail's greatest current, the input at its lowest and the
efficiency at its least. The efficiency counts as a least only if the
design marks it `min` under `limits`; a typical one is not a bound. On
the test library's TPS54202 stage (12 V within 5% to 5 V, 1.5 A,
85%): 820.2 mA, worked by hand in the test.

**Hold-up behind a converter.** `min_holdup_time` no longer refuses a
rail that feeds a switching regulator: the converter is taken at the
floor, where it draws most over the discharge, so the constant current
the formula uses is still the greatest. The test stage's 10 uF input
holds 11.4 V above 8 V for 26.2 us (A171).

**A fault this found, fixed.** A rail's greatest current ignored draws
the power tree could not work out at all: a design whose linear
regulator states no quiescent current got a "greatest" input current
of 301.6 mA, the rest alone, as if it were a bound. Such a draw now
leaves the greatest unknown, named with everything else missing. Only
checks that rest on a rail's greatest current are affected: a linear
regulator's junction temperature, a converter's inductor peak current,
the hold-up, the new budget, and the simulation comparison at the worst
corner.

### A third check: exact nodal analysis of linear circuits (M2.2, 2026-10-12)

For a circuit of resistors, capacitors, inductors and independent
sources, `simulate` now also works out each number exactly from the
same circuit with no simulator (`kairo.spice.nodal`): the modified
nodal equations solved in rational numbers for the steady state, and
directly in complex numbers at each of the simulator's frequencies for
a sweep. The value is shown beside ngspice's and Xyce's as `exact nodal
analysis`; where it and the simulators part, the number is not
checked. A circuit with anything else in it (a diode, a regulator's
model, a maker's subcircuit) gets no third value.

The plan named lcapy on SymPy for this. Neither is needed: the
equations are linear, the standard library solves them exactly, and
the core install keeps its two dependencies (A170).

Measured: a divider's midpoint is exactly 2.5 V and its supply
current 250 uA; an RC filter's cutoff agrees with the simulators'
within one part in a million; a 1% error put into the exact solution
on purpose makes the number unverified. On the accuracy set, the
resistor ladder and the RC low-pass solved from their netlists give
the answers the set records, within each check's budget. Interpolating
the sweep's 50 points a decade costs 0.38 mdB of the low-pass's 2 mdB
budget at its corner, before any simulator is involved. The RC step
(a response in time) and the temperature-coefficient resistor (a model
parameter) are outside what is solved.

### Ripple in simulation, ESR at the worst case, and nets nothing drives (M2.3, 2026-10-12)

**Ripple.** `simulate` has a `ripple` metric: a net's peak-to-peak
voltage over the settled part of a run in time (the second half, or
from `analysis.ripple_from`). It is measured only on nets a limit names,
since a small spread of a large value is the first number to differ
between runs. A new `sine` stimulus puts a sine on a level (a supply's
ripple, `amplitude` and `frequency`), and a run with one takes at least
200 steps a period. With the run's own step, a 1 ms run gave 10 points
a period and read a filter's ripple 4% low; at 200 a sampled peak is
within about 0.01%.

Measured on an RC filter, 1k into 100 nF, fed 5 V with 100 mV at
100 kHz: 3.183 mV with an ideal capacitor and 3.721 mV with 10 ohm of
ESR, each within 0.2% of 2 * A * |H(jw)| on ngspice and Xyce.

**Ripple with no ESR is unverified, naming the capacitor** (M2.3's
acceptance): a capacitor with no stated ESR is modelled ideal, and ESR
often sets most of a ripple.

**A fault this found, fixed: the worst case held ESR exact.** The sweep
did not vary a capacitor's ESR, so a ripple limit passed at 3.98 mV
with the ESR left at its stated 10 ohm. ESR now varies over the part's
stated `esr_min` to `esr_max` (the least spread about `esr` that holds
the range); with no range stated, a limit the ESR moves is
`unverified` and says what to give. With 8 to 12 ohm stated the worst
case is 4.194 mV, within 0.01% of the formula at R -1%, C -5%, ESR
12 ohm and the source +2% (A168).

**Nets nothing drives.** A net whose only connection is a part left
open (no model) has nothing that sets its voltage, and the simulators
either failed with a singular matrix (the op-amp design) or settled it
by their own aids, each its own way (the RC filters at rest, which
were reported `checked`). Each such piece is now held to ground
through 1 teraohm, so every simulator solves the same circuit, and the
run is `unverified`, naming the nets and the parts left open on them
(A169).

**Calculation beside simulation on every reference design**
(`evals/simulation/run.py --reconcile`). Every design whose parts all
have models agrees: ldo_3v3, ldo_overloaded, led_indicator,
reverse_polarity and usb_c_sink are checked. ldo_lm1117 with TI's
model agrees within the 5% tolerance and is unverified as before
(A123); its 10 mA gap is the stated maximum quiescent current, which
the calculation counts and TI's model does not draw at all. Designs
with a part no model covers (the buck, the I2C sensor, the MCU, the op
amp, the adjustable regulator) are listed with it.

**CI fixed.** The simulator job, which has ngspice and no Xyce, had
failed since 2026-10-11 on a loop test that demanded the two-engine
verdict; the test now expects `unverified` with one engine.

### Parts of several units in a drawn schematic (M2.4, 2026-10-12)

`edit_schematic` now adds a part of several units, such as a dual op
amp or a quad gate, where it refused one before. The part is drawn
whole: each unit in the free spot nearest the one before, with a wire
and label or power symbol on each pin, so every pin of every unit
needs a net or a place under `no_connect`. A pin the symbol shares
between all its units (the 7400's supply pins) gets a wire on each
unit. A pin name found in two units (an op amp's `+`) is refused with
the units and numbers it could mean; give the number or write `+*`.
A pin is now found by its number when no pin has that name, for named
pins too, as the tool's description already said (A166). Removing and
reconnecting such parts already worked unit by unit.

Measured on the pedalboard fixture with KiCad 10.0.6: an LM358 (three
units) and a 7400 (four gates sharing two supply pins) added to the
LEDs sheet give exactly the connections asked for, and the rule check
nothing new. Adding then removing an LM358 leaves the sheet's drawing
as it was.

**A fault this found in the writer, fixed.** A symbol that extends
another was embedded without its base's `ki_locked`, the flag saying
its units cannot be swapped. KiCad's rule check then called the copy
different from the library: three `lib_symbol_mismatch` warnings for
one LM358, which extends LM2904. The flag now comes from the base. It
only matters for derived symbols with several units, and none was ever
placed before; on the real boards the embedding still reproduces 75 of
89 copies, the same as before.

Not done: a built design still places one unit per reference; units
on a crowded sheet can land far apart; units cannot be put on
different sheets. Seen in KiCad's plot of the LEDs sheet: the units
sit together and read well, but a ground symbol on one pin of a small
unit crowds the next pin's wire (pins 5 and 6 of the LM358), as it
would on a single-unit part with pins that close.

### Choosing a regulator: `find_part` knows chips of one kind (2026-10-11)

`find_part` now finds regulators as well as passives: positive linear
regulators and step-down and step-up converters, by the output voltage
wanted, and narrowed by topology (`ldo`, `buck`, `boost`), the input
voltage the part must take and the least output current.

    find_part kind=regulator value=5V topology=buck v_in=12V i_out=1.5A

A fixed part must have the output asked for; an adjustable one must
reach it. With `v_in`, a part is left out if its listed input range
does not hold that voltage, if it would have to step the wrong way, or,
for a linear part, if the input is not above the output by its listed
dropout. Each answer carries the part's datasheet link.

**Nothing from a regulator's listing goes into a design.** A listing
does not say which figure is typical and which a limit, so the figures
are shown with `_listed` in their names and are for choosing only. The
path for a chip is: choose it here, read its limits from its datasheet
(`read_datasheet`), keep them (`save_part`), name it in the design.

Measured on the snapshot of 2026-10-11: 16,721 regulators kept (14,507
linear, 1,628 step-down, 586 step-up), 2,513 of them in stock. Four
searches were run and the 15 parts they showed were compared with the
listings they came from: every figure agreed. That comparison found one
fault, fixed before this was committed: a dropout listed at two
currents (`300mV@(200mA), 100mV@(100mA)`) was read as its first figure;
it is now read as none. A figure listed twice for a part with two
outputs is likewise read as none. The same gap applies as for passives:
the snapshot holds no long-established part numbers.

The catalogue file's layout changed (`kairo-parts-2`), so one built
before today is refused with a message saying to build it again. The
one in this machine's cache folder was rebuilt (77 MB, 305,246 parts).

Not kept: negative regulators, other topologies (flyback, inverting),
controllers with outside switches are not told apart from converters,
and no other kind of chip.

### A hold-up requirement, and a step-down refusal (M1.9, partly; 2026-10-11)

**`min_holdup_time`** is a new requirement: a rail's capacitors must
keep it above a floor for a stated time once its supply is gone.

    {"type": "min_holdup_time", "net": "VBUS", "floor": "4V", "time": "1ms"}

It is judged at the worst case of `t = C * (Vstart - Vfloor) / I`
(`holdup_time`, the 36th calculation: the defining relation of
capacitance, integrated for a constant current): the capacitors to
ground at their least under tolerance and DC bias, the rail at its
lowest before the supply goes, and the most the rail draws. Worked by
hand in the tests: two 100 uF polymer capacitors of 20% on 5 V within 5%
with 100 mA hold above 4 V for 1.2 ms, where marked values would say
2 ms; asked for 1.5 ms, it fails.

What it will not pass:

- A class II ceramic with no DC-bias curve: its capacitance at the
  rail's voltage is not known, as for `min_capacitance`.
- A rail that feeds a switching regulator, which draws more as the rail
  falls: the formula is for a constant current, so the result is
  `unverified` and says to simulate.
- A rail with nothing stated to draw from it, or whose greatest draw is
  not established.

What it leaves out, and says so in every result: the step across the
capacitors' series resistance when the supply goes. The summing of a
bank's least capacitance is now one function shared with
`min_capacitance` (`_bank`), which is otherwise unchanged.

This is the check the plan's hold-up reference design needs. That
design is not written: the plan asks for a bank of ceramics only, and
no manufacturer's DC-bias curve is in the parts store yet (the owner's
open item under M1.6), so it could only come out `unverified`.

**`buck_refusal`** is the fourteenth reference design: the step-down
converter of `buck_tps54202`, part for part, asked for 5 V within 5%
where the other asks for 7%. The datasheet's own divider gives 4.863 V
to 5.298 V, 48 mV over the top, so it must be refused for
`rail_voltage` and is. The run of 2026-10-12T024145Z scores 14 of 14.

### Part numbers a drawing already holds, and a chip's figures kept by part number (2026-10-11)

Two things that finish the importing asked for.

**A drawn part's own part number is read.** KiCad's netlist carries each
part's fields, and many drawings give a part number in one. The netlist
reader now keeps them (`Component.fields`; the layout was read from four
real boards' netlists), and `check_schematic` and `simulate_schematic`
give a part the number in its fields as its `mpn` where the facts state
none: a maker's number from a field named like `MPN`, `Part_number` or
`Manufacturer Part`, or an LCSC number from a field with `lcsc` in its
name. Where a part has both, the one the catalogue holds is used. The
answer counts them (`part_numbers_from_the_drawing`).

Measured on the four real boards that could be exported that day, with
the catalogue built from the snapshot of the same day:

| Board | Parts | With a part number | Passives the catalogue held |
|---|---|---|---|
| rtb-c13 | 122 | 91 | 0 of 53 |
| clavier | 344 | 117 | 0 of 82 |
| mikoto | 67 | 51 | 0 of 41 |
| pedalboard | 169 | 0 (it names suppliers, not numbers) | none to look up |

**Not one of 176 passives was found**, and the reason is the snapshot,
not the reading: every number was looked up, and the catalogue holds
nothing below LCSC number C6374512. The snapshot's rows were fetched
between 2026-09-14 and 2026-10-11 at about 75,000 a day, and its format
is marked `source-db-v2`: the jlcparts project is crawling its source
afresh, and the long-established parts that real boards use have not
come round yet. So today the catalogue chooses parts for a new design
(`find_part`) and does nothing for a drawn board. Fetch it again in a
few weeks (`python -m kairo.parts.catalogue fetch`) and run the count
again before relying on it. The list of basic and preferred parts that
the jlcpcb-parts-database project publishes is made from the same
source and has the same gap: fetched the same day it held 999 parts and
not the common 10k resistor C25804, so it is no way round.

**`save_part` (propose tier) keeps a part's datasheet figures under its
part number.** The figures are the parameters a design states, each with
its page and, for a datasheet limit, its side: `{"i_limit": {"value":
"2.5A", "page": "5", "limit": "min"}}`. A part that then gives that
number as `mpn`, in a design or in a drawing's field, takes them where
it states none, with its `limits` marked and its source citing the
document and page (`kairo.parts.fill.fill_from_store`). So a chip's
limits are read from its datasheet once (`read_datasheet`), saved, and
used by every design after. The step-down stage of the tests, with its
regulator named and none of its figures stated, gives the same rails
and the same result for every requirement as the one that states them
all.

What guards it:

- A saved figure is `unreviewed` until a person checks it against the
  page and marks it in the file. The part's source and the answer's
  `parts_store` note say so wherever one was used.
- A figure a person marked `reviewed` is not changed by the tool
  (`E_PART_REVIEWED`).
- A figure that names no parameter of a design, has the wrong unit or
  gives no page is refused, and nothing is saved.
- What a design states is never replaced.

The order a part's figure comes from is now: the design, the parts
store (a datasheet, with its page), the catalogue (a distributor's
listing), Kairo's default margin.

The store is the folder given by `--parts-dir`, or `parts` in Kairo's
cache folder when none is given, so the tool works with no setting. A
part number finds its file in any letter case.

Tried end to end on TI's real TPS54202 datasheet by a script calling
the tools in the order a model would: `read_datasheet` gave the
feedback voltage (0.581, 0.596, 0.611 V), the high-side current limit
(2.5 A least) and the switching frequency (390 kHz least) from page 5;
those were saved with `save_part`; and the step-down reference design
with its regulator named and no figure stated gave the same rails and
the same result for all seven checks as the design that states them.

Not done: no model has driven these tools yet; the script chose what to
save. One value a parameter, so a datasheet's min, typ and max of one
quantity go in as a value and a tolerance, which the caller works out.
A part number must match whole: figures saved for `TPS54202` are not
used by a part named `TPS54202DDC`, since a suffix can be another
voltage or grade as easily as another package.

Tools: 28 at the write tier, 15 at read.

### A chip's limits, read from its datasheet PDF (2026-10-11)

`read_datasheet` (read tier) reads the limit tables of a datasheet PDF
into rows, each with the figures under MIN, TYP and MAX kept in their
own columns, its unit and its page. This is the half of importing the
catalogue cannot do: a regulator's reference range, current limit and
least switching frequency are in its datasheet's tables and nowhere
else.

Which column a figure is in matters more than the figure, since a
typical value is not a limit. The columns are found from where the
headings sit on the page, and each figure goes to the heading it is
nearest (`kairo.parts.datasheet`). A row where two figures would land
under one heading, or with words where the figures go (a page's
footer), is left out.

Tried on TI's TPS54202 datasheet (SLVSD26C), pages 4 and 5: 44 rows,
every figure under the right heading, among them the feedback voltage
`0.581 0.596 0.611`, a quiescent current given as typical only, an
enable threshold given as typical and maximum, and the absolute maximum
ratings with their negative minima. These are the figures typed by hand
into the step-down reference design earlier the same day.

How it is kept light, as the owner asked: the reader is pdfplumber
alone, the same library the owner's Scraper project reads PDFs with, and
no layout model. It is an optional extra (`kairo[datasheets]`), so a
plain install still has two dependencies. Its licences are in NOTICE.
Docling was not needed for this page.

Limits:

- **Four datasheets, not a survey.** Beside TI's, it was run on three
  more, fetched the same day. Microchip's MCP1700 (37 rows) and
  Advanced Monolithic's AMS1117 (57 rows): the rows looked at agree
  with the page's own text (quiescent current 1.6 typical and 4
  maximum, dropout 178 and 350, least output current 250; reference
  1.232, 1.250, 1.268). Diodes Incorporated's AP63205 (24 rows): its
  table stacks several parts in one cell, and whether each figure is
  paired with the right name there was not established. Rows that
  hold something other than a plain figure in a column, such as
  `VR-3.0%`, are left out. The tests use a page written by hand,
  since no maker's datasheet is in the repository.
- **A row's name is rough.** It is the words on the line as they lie:
  a subscript is lost (`VFB` reads `V`) and a name on two lines is in
  two pieces. The result says to check a row against the page.
- **Nothing is filled in from it.** The tool shows the rows; the model
  or the user writes a limit into the design, marks it min or max and
  cites the page. Kairo does not download datasheets.
- Thermal tables, which have no MIN and MAX headings, are not read. A
  whole datasheet takes about 14 s; a named page a fraction of that.

Tools: 27 at the write tier, 15 at read.

### A parts catalogue: choosing real parts, and their figures by part number (M2.9, partly; 2026-10-11)

The owner's aim, restated 2026-10-11: a model should find choosing
parts and checking work simple through these tools.

- **`find_part`** (read tier) gives a short list of real resistors,
  capacitors, inductors or LEDs of one value: maker's part number, LCSC
  number, tolerance, ratings, stock, with basic and preferred parts
  first. It narrows by package, widest tolerance, dielectric and least
  ratings.
- **A design that names a part number takes that part's figures.** Give
  a part `params.mpn` (or an LCSC number such as `C16194178`) and its
  tolerance, ratings, dielectric, temperature coefficient or saturation
  current are filled in where the design states none. A stated figure
  is never replaced. The part's source then says which figures came
  from the catalogue, from which entry, read when, and that they were
  not checked against the datasheet. So the order is now: what the
  design states, then the part's listing, then Kairo's default margin.
- **A part number that is another part is caught**: a resistor marked
  10k whose number is a 10.2k part, or a capacitor with a resistor's
  number, takes nothing and is reported.

The catalogue (`kairo.parts.catalogue`) is built from the jlcparts
project's snapshot of JLCPCB's and LCSC's parts. A person fetches it
(about 160 MB, 1.1 GB unpacked) and Kairo cuts it down to the kinds it
reckons with: 64 MB, built in 19 s. The split archive is unpacked with
the standard library alone, so nothing else needs installing. Nothing
is downloaded unless that command is run, and no test uses the network:
the tests build a snapshot of twelve rows by hand, in the real one's
layout, with listings copied from it.

Measured on the snapshot fetched 2026-10-11 (2,074,000 parts): 288,525
kept (111,387 resistors, 99,566 capacitors, 48,192 inductors, 29,380
LEDs); 36,464 listings of those kinds gave no value and were left out.

Limits, each seen in that snapshot:

- **It was not the whole catalogue.** Six parts had an LCSC number
  below a million and one was marked basic: the long-established parts
  (the common 10k resistor C25804, for one) were not in it, and only
  189,688 parts showed stock. Whether that is how the project now
  publishes or a crawl part-way through is not known.
- **A listing is not a datasheet.** It can be wrong, and it does not say
  whether a figure is typical or a limit. That is why an ESR is shown
  by `find_part` and never filled in, and why every filled figure says
  where it came from.
- **A figure that is not one plain number is left out**: a tolerance of
  -20% to +80% is not turned into one number.
- **No chips.** A regulator's limits are in its datasheet's tables,
  which this does not read.
- A schematic a person drew is not filled from the catalogue yet: only
  a design given to the design tools.

Tools: 26 at the write tier, 14 at read.

### Kairo's own default margins, in place of `unverified` for a missing tolerance (2026-10-11)

The owner's decision, 2026-10-11: "We have our own margin of error that
we default to and make it generous, but ultimately we use whatever the
spec sheets of certain components require", after the way LTspice runs
with generic parts and says that it did. Until now a tolerance the
design did not state was never filled in and the result was
`unverified`; that rule is changed.

Where a design states no tolerance, Kairo now reckons with a wide one of
its own (`kairo.defaults`), and a figure the design states always takes
its place:

| Kind | Default |
|---|---|
| resistor | 5% |
| capacitor | 20% |
| inductor | 30% |
| supply from outside the design | 10% |
| a diode's or LED's forward voltage | 20% |
| a regulator's output or reference | 5% |

These six figures are Kairo's choices (A159); the owner asked for
generous defaults and has not set the numbers. A project can change any
of them (`policy.default_margins`) or turn them off
(`policy.margins: "stated"`), which is the old behaviour and is meant
for the check before a board is ordered.

What does not change:

- **A default is always named.** Each requirement's result carries
  `basis` (`default margins` or `stated values`) and lists each default
  it rested on; its summary says so; the accuracy report counts passes
  on defaults apart and lists every default in use; a simulation's
  limits carry the same. A model is told to say so when it reports one.
- **A fail on defaults says the default may be why**, and asks for the
  real figures.
- **Only spreads have defaults.** A fact about one part (a dropout, a
  current limit, a thermal resistance, an ESR, a switching frequency, a
  capacitor's loss under DC bias) is never filled in, and still leaves
  its result `unverified`.

Measured on the thirteen reference designs, each judged both ways: with
defaults on, four results go from `unverified` to `pass` (an LED
resistor's power in four designs, each on the forward voltage's or the
supply's default); no result that passed or failed changes; everything
that waits on a fact stays `unverified`. The reference designs
themselves are pinned to `stated`, since their specs say which results
must stay unverified for want of a datasheet figure; they score 13 of
13 with records identical to the run before.

The tests of what a missing figure does now set `stated` in their
designs; `tests/unit/test_defaults.py` and one simulation test cover the
defaults. The design schema is 11,699 characters, where it was 10,851.

### A step-down converter: the thirteenth reference design, and the checks it needed (M1.9; 2026-10-11)

`evals/reference_designs/buck_tps54202`: 12 V to 5 V at 1.5 A round TI's
TPS54202, every regulator value from its datasheet (SLVSD26C, February
2026, downloaded that day), the divider the datasheet's own. KiCad
builds it with every layer passing and a clean rule check, and the run
scores 13 of 13 (`evals/results/2026-10-11T175138Z-scripted`).

What had to be built for it:

- **The power tree finds a step-down stage.** A switching regulator
  drives a switch node, not its rail; the rail is the power net on the
  far side of the one inductor from that node. The stage's inductor is
  the regulator's own path and is no longer listed as an unknown draw.
- **Two requirement types.** `max_inductor_current` compares the
  inductor's greatest peak current with its saturation current
  (`i_sat`). `max_ripple` bounds a stage's output ripple, peak to peak.
- **A check made for every such stage**, `switch_current_limit`: the
  peak current against the regulator's least current limit (`i_limit`,
  marked a minimum).
- **Three calculations**, each tested against the datasheet's own worked
  numbers: `buck_ripple_current`, `buck_output_ripple` (the
  capacitance's part and the ESR's part added, which is no less than
  the two together) and `series_resistors_current`.
- **A switching regulator's rail from its reference and divider**
  (`feedback_divider_output`), with the feedback pin's own current
  taken as nothing, as the datasheet's formula takes it; the result
  says so.
- **A divider's current is solved** where the net between its two
  resistors holds nothing else but input pins. It was listed as
  unknown, which left a regulator's load `unverified` for want of a few
  microamps.
- `regulator_stability` on a switching regulator now says what the
  matter is: its loop, not a capacitor's ESR range. It stays
  `unverified`.

The numbers, each also worked by hand in `tests/unit/test_buck.py`:

- the rail is 4.863 V to 5.298 V (0.581 V to 0.611 V times 1 + 100k /
  13.3k, resistors 1%), inside 5 V within 7%;
- the ripple current is at most 0.656 A (12.6 V in, 390 kHz, 12 uH), so
  the peak is at most 1.828 A against 3.5 A of saturation and 2.5 A of
  current limit;
- the output ripple is at most 31.8 mV into 37.6 uF and 40 mohm,
  against 50 mV.

Things this showed:

- **The datasheet's own divider does not hold 5 V within 5%.** It gives
  5.08 V at nominal and up to 5.30 V. The reference asks for 7%; the
  same circuit asked for 5% fails the rail check, which a test holds.
- **One earlier record moved**: `ldo_adjustable`'s regulator load went
  from `unverified` ("at least 100 mA") to `pass` (105.2 mA), because
  its divider's 5.2 mA is now counted, and its junction's lower bound
  rose from 86.8 to 89.2 degC with it. The design is a refusal for its
  rail voltage and still is. The other eleven records are unchanged.
- The plan's acceptance for a ripple requirement on a capacitor with no
  ESR (`unverified`, naming the ESR) is met by the calculation, and
  tested.

Limits: the ripple is worked out for one output capacitor only, and a
ceramic one needs a DC-bias curve first; the formulas are first-order
and leave out the switches' voltage drops and every inductance, so the
spikes a real board shows are not in the figure; the converter's
junction temperature and its input current are not worked out (its
efficiency is a curve in the datasheet, not a limit); nothing was
simulated, since a switching regulator needs its maker's model. The
design schema is 10,851 characters, where it was 9,779.

### A rail's greatest current, through chips and linear regulators (M1.7; 2026-10-11)

A requirement that rises with a rail's current (a regulator's load, its
junction temperature) needs the most the rail can draw. That was worked
out only for stated loads and resistor branches (A139); a rail that
feeds a chip's supply pin or another regulator was left `unverified`
with "its greatest value is not worked out". Now:

- **Through a linear regulator**, the rail it makes is worked out the
  same way and added, since the regulator passes that current on. What
  that rail lacks is named from the rail that asked.
- **A part's supply current and a regulator's own current** count as
  their most only where the design marks the stated value as the
  datasheet's maximum (`limits: {"i_q": "max"}`). A figure not so
  marked may be typical, and the result says so and what to do.
- A switching regulator's input current is still not worked out.

On the regulator example: the input rail's most is the 300 mA load plus
the LED branch at its worst, (3.366 - 1.8) / 990 = 1.58 mA, plus 5 mA
of quiescent current once that is marked a maximum. The power tree's
draws now carry what kind each is (`supply`, `quiescent`, `passed_on`).

### KiCad's own SPICE netlist is compared with the design (M2.1; 2026-10-11)

`verify_design` now also has KiCad export its SPICE netlist of the
project (`kicad-cli sch export netlist --format spice`) and compares
each two-pin resistor, capacitor and inductor in it with the design:
the two nets it joins and its value (`kairo.spice.crosscheck`). The
result is `agrees`, or `differs` with a line per difference. KiCad's
netlister and Kairo's are separate programs reading the file and the
design, so a difference means the file does not say what the design
says, or one of them is wrong.

- On the regulator example built through the server: `agrees`.
- Each kind of difference (another net, another value, a part missing,
  a part extra) is named when seeded; `1k` against `1000` is not one.

Only passives are compared: KiCad writes every other part from its
`Sim.*` fields, which Kairo does not set, so those lines carry no model
and would only say so. `Sim.*` write-back is still not built.

### Loop gain, the accuracy report's simulation side, and comparison at the worst corner (M2.3; 2026-10-11)

The last three items on M2.3's list.

**Loop gain.** A request with `loop: {pin, into}` on an `ac` analysis
measures the gain of the feedback loop that passes through that pin,
with the loop left closed: a voltage is injected in series at the pin
and a current into the downstream side, in two runs (the method of
Tian and others, 2001). The result gives the crossover frequency, the
phase margin, and the gain margin where the phase reaches -180 degrees.
`kairo.spice.loop` derives the formula from the two-port equations in
its docstring, so it does not rest on a memory of the paper: a first
attempt from memory was wrong by a factor of thousands on the test
loop and was thrown out.

- The plan's accuracy circuit 10, a loop of two poles that load each
  other, whose gain is known in closed form: both simulators give the
  loop gain to parts in a million million, and the phase margin
  (6.36 degrees) within 0.05 degrees, measured at the amplifier's input
  and again at its output.
- It is `checked` only if the part's model is a maker's (`vendor`), the
  tightened run agrees and the simulators agree; any other model class
  is `unverified`, since only a maker's model represents the loop.
- Which way the signal flows through the pin matters: told the wrong
  way the measurement is of another quantity. An input or output pin
  says it; for any other type the request must (`into`), and is refused
  without it.

No model on this machine has a loop to measure: TI's LM1117-3.3 model
has its loop inside the part. The test uses an amplifier written for
it. So the method is checked and no real regulator has been measured.

**The accuracy report's simulation side.** `accuracy_report` now also
says what a simulation of the design would rest on, without running
one: the model for each part and its class, the parts left open, the
values with no tolerance, the simulators installed with their versions
and whether each is one the accuracy set was recorded on, and the
agreement thresholds in force. The report file saved in a project is
unchanged, since which simulators are installed is a fact about the
machine.

**Calculation and simulation compared at the worst corner.** A
steady-state run already put the power tree's rail currents beside the
simulated ones at nominal. It now does the same at the worst case: the
calculation's greatest current for a rail beside the simulation's at
the corner the simulation's own runs find worst. An LED and resistor on
5 V, 2%: 3.3333 mA calculated, within 0.5% simulated, at the corner
supply +2%, resistor -1%, forward voltage -10%. Only for rails a source
feeds directly; a rail a regulator makes is left out, because the
regulator stand-in's output is not varied.

M2.3's box stays open: of its acceptance, the ripple requirement on a
capacitor with no ESR has not been tried, and "every reference design
that has models" has not been run since these changes.

### Simulated worst case: a search between the ends, temperature corners, and a sampled spread (M2.3; 2026-10-11)

Three of the plan's analyses, each with the plan's own test circuit.

**A worst case that lies between the ends is searched for.** When a
simulated number does not move one way with a value, no corner has its
worst case, and such a limit was simply `unverified`. Now each such
value is tried at nine points across its tolerance and nine more about
the worst of them, twice round, and the worst point found is simulated
in full. A checked number there that breaks the limit is a `fail`: it
is a board that could be built. Finding nothing proves nothing, so the
limit stays `unverified` and gives the worst number found. Also fixed:
a number whose two ends came out equal was taken not to depend on the
value at all, which missed a peak sitting exactly at nominal.

- The plan's accuracy circuit 9: power into a 110 ohm, 20% load from a
  100 ohm source. The greatest power, 0.257576 W, is inside the load's
  range and above both ends. A limit of 0.2572 W, which both ends meet,
  is failed by the search, which finds the peak within 2 parts in
  10,000; a limit of 0.26 W stays `unverified`.

**Temperature corners.** `analysis.temperatures` names corners, such as
-40degC and 85degC. Limits are judged at 27 degC and again at each
corner, where every value's tolerance is widened by what its part
states: a `tempco` such as `100ppm/degC`, taken as a limit either way,
or a ceramic capacitor's dielectric code (EIA-198: 30 ppm per degree
for C0G, the class II codes' whole band). A limit passes only if it
passes at every one. A part that states nothing leaves the limit
`unverified`, naming the part and what to give; so does a behavioural
model, which has no temperature data, and a diode's forward voltage,
for whose change with temperature a design has no parameter yet.

- A divider of two 10 kilohm, 1%, 100 ppm/degC resistors on 5 V, 2%:
  its highest output is 2.5755 V at 27 degC and 2.59259 V at -40 degC
  (the formula gives 2.592585). A limit of 2.58 V passes at 27 degC and
  fails with the corners; one of 2.60 V passes.

**A sampled spread, for a report only.** `monte_carlo` in a request
gives `samples`, a `seed` and a distribution for each value to be
sampled (`uniform`, or `normal` with the number of standard deviations
the tolerance is). Only those values are sampled: a datasheet tolerance
is a limit and says nothing of the spread inside it, so the others stay
at nominal and are named. The result gives each metric's lowest,
highest, mean and standard deviation, and says first that it decides
nothing. No verdict is changed by it (tested).

- The plan's accuracy circuit 8: the same seed gives the same 120
  samples; every sample of the divider's output lies inside its
  analytic extremes, 2.4255 V to 2.5755 V.

Limits: the search and the samples run in one simulator with one group
of settings, and only the search's worst point is run in full; the
search has no guarantee of finding a narrow peak between its points,
which is why it never passes a limit; temperature reaches Kairo's own
part models only through the widened tolerances, so a rail made by a
behavioural regulator is `unverified` at any corner. Not built: loop
gain, the simulation side of `accuracy_report`, and sharing the
calculations' corners.

### Part of a drawn schematic can be simulated (M2.4; 2026-10-11)

`simulate_schematic` (propose tier) takes a project, the parts to
simulate together, a request as `simulate` takes and facts as
`check_schematic` takes. The netlist is cut down to the parts named
(`restrict` in `kairo.kicad.drawn`), the facts about the rest are left
out and listed, and the result says which parts and nets were in the
circuit. This is the owner's question of 2026-10-10 answered as
recommended then: not whole boards, whose chips have no models, but
the parts that have.

Measured on the pedalboard fixture: R14 (20 kilohms) and C31 (1.2 nF),
the compensation network of its switching regulator, cut off at
6,632.4 Hz in both simulators, against 6,631.5 Hz from
1 / (2 pi R C).

Two faults that trial showed, both fixed with a test that fails without
the fix:

- **A run that measured nothing was reported `checked`.** The board
  writes R14's value as `20K`, which Kairo did not read, so the
  resistor was left open, no number was measured, and the status,
  taken from the numbers, was `checked` with none to judge. A result
  with no metrics is now `unverified` and says why. This applied to
  `simulate` on a built design too.
- **Values in IEC 60062's marking are read**: `20K`, `2K2`, `4R7`,
  `R47`, `1M5`, `4n7`, where a letter stands for the decimal point.
  Only for a component's value and only for its own unit's letters; a
  lower-case `m` on a resistor is still refused. Every check that reads
  a resistor's or capacitor's value gains by this on real boards.

Not built: keeping a project's facts between calls, which waits on the
owner's answer about a per-project `kairo/` folder.

### Parts added to, removed from and reconnected in a drawn schematic (M2.4; 2026-10-11)

`edit_schematic` (write tier) changes a schematic a person drew, by
three operations applied together or not at all:

- `add_part` draws a new part in free space, nearest the part named in
  `beside`, with a short wire on each pin ending in a power symbol (if
  the project already draws that net with one), a global label, or a
  local label for a net of that sheet. Nothing already drawn is moved.
- `remove_part` removes the part and follows the drawing from each of
  its pins: a wire that then leads nowhere goes, a wire that something
  else joins part-way is cut back to there, and a label or power symbol
  left serving nothing goes too. A hierarchical label is left and named.
- `reconnect` frees one pin the same way and joins it to another net by
  a new wire and label.

KiCad is then the judge: its netlist must hold exactly the connections
the operations should give, every other part as it was, and its rule
check no kind of violation more often than before (the baseline). If
not, the files are put back. `max_changed_ratio` refuses a change
larger than that share of any file's lines, before writing.

Measured on the pedalboard fixture (KiCad 10, six sheets, 169 parts),
each judged by KiCad:

- adding a resistor between a power rail and a sheet's net: written;
  every old item of the sheet still there in order, five new ones;
- swapping a pull-up (add one, remove one): written, 433 of 20,937
  lines changed, one old line altered (a wire cut back);
- moving a pin to another net: written, 21 lines;
- adding an LED and its resistor on a new net: written;
- ten removals of two-pin parts chosen across the board: after the
  first fault below was fixed, five written and five refused by the
  baseline, each for something the removal left joined to nothing (a
  pin of another part, or a label);
- six removals that cut a wire back: four written, two refused by the
  baseline.

Two faults found on the way, both fixed:

- A symbol copied from another project keeps that project's references
  beside its own. Reading all of them sent three of the ten removals to
  the wrong part on another sheet. KiCad's netlist caught each and the
  files were put back. `set_value`, shipped on 2026-10-10, read
  references the same way and could have been sent to the wrong part;
  it now reads only the open project's entry (`references` in
  `kairo.kicad.edit`). The test fails with the fix taken out.
- A wire was removed whole when other wires joined it part-way; it is
  now cut back to the nearest of them. This one was found by reading
  the code while looking for the first, not by a trial: none of the ten
  removals had such a wire. The swap test covers it (R19's wire is cut
  back), and it was not run against the code without the fix.

Limits: only files in KiCad 10's format (2 of the 16 real boards), since
the symbol written is the writer's and only its KiCad 10 output has
been compared with KiCad's own; single-unit symbols; a net KiCad named
from a pin cannot be joined by name; a pin that wires pass through
cannot be moved; a sheet used more than once is refused; the symbol a
removed part used stays among the file's own symbols. Not done: a look
at the result in the KiCad window (the placement is checked for
overlap by generous boxes, not by eye), and a KiCad re-save of an
edited file. The plan's acceptance asks for scripted edits on the
re-saved real boards; these ran on one board.

### A third local-model attempt (M1.9, partly; 2026-10-11)

`qwen3.5:9b` on `rc_lowpass`, after the changes the second attempt led
to (`evals/results/2026-10-11T101657Z-model-qwen3.5-9b`): a miss again,
so the record is three attempts and three misses. 33 calls in 30 turns,
490,936 prompt and 2,577 output tokens, 252 s. This number was taken
after tuning Kairo on this very design and model, so it says nothing
about other designs.

It got further: it read the patch half of the schema, was told where a
design's shape is, and read that. It then searched for a capacitor
footprint as `C_Ceramic 0603`, which no library has, and repeated that
search 20 times. Kairo refused the repeats from the fourth on
(`E_REPEATED_CALL`), and the model went on asking until its turns ran
out: a server can refuse a call, not stop a client from making it.

Changed because of it: `find_symbol` and `find_footprint`, when nothing
has every word, return what has all but one with a note naming the word
left out.

### A second local-model attempt, and what it changed (M1.9, partly; 2026-10-11)

`qwen3.5:9b` on `rc_lowpass` again, now with the smaller schema
(`evals/results/2026-10-11T100046Z-model-qwen3.5-9b`): a miss. 34 calls
in 30 turns, 433,671 prompt and 8,256 output tokens, 628 s (the first
attempt took 1,817 s). One attempt, as before, so not a comparison.

What it did: asked `design_schema` for part `patch`, so it never saw
the shape of a design; found the symbols, pins and footprints
correctly; then called `plan_design` 25 times with shapes of its own
(lists where a design has objects keyed by name, patch operations in
place of a design), each refused with `E_DESIGN_INVALID`, until its
turns ran out. No two of those calls were alike, so the loop guard,
which looked for the same call with the same answer, never fired. The
split of the schema into two parts, made the day before, is what let it
read the wrong one.

Changed because of it:

- `design_schema` with part `design` now starts with a whole, valid
  design at its smallest (`kairo.ir.schema.EXAMPLE`, held valid by a
  test). With part `patch` it says that this is not a design's shape
  and where that is.
- `E_DESIGN_INVALID` now carries that example and says that nets,
  components and connections are objects keyed by name, not lists.
- The loop guard has a third limit: one tool failing with the same
  error code 5 times running is refused (`E_REPEATED_FAILURE`) until
  some other call has succeeded. The 5 is Kairo's own number (A143).

Not measured: whether the model now succeeds. That is a third attempt,
not yet run.

### A keyword starts a design: "Kairo" or "kairo go" (2026-10-10)

Asked by the owner. `kairo.protocol` holds Kairo's order of work for a
design or a check, one list of steps per tier. The server gives it three
ways: its instructions tell a model that a message starting with "Kairo"
or "kairo go" means call the new `go` tool first; `go` (read tier)
returns the steps; and a prompt named `go` carries the same steps for a
client's prompt menu.

- The steps: ask for what is missing and choose no value, pins from the
  library, numbers from `calc`, `plan_design` before any write,
  `open_in_kicad` after one, and a report that lists every unverified
  result unchanged, then the review's faults and suggestions.
- A test holds that the steps for a tier name only tools that tier
  offers.
- Tools: 23 at the write tier, 13 at read.
- Not measured: whether a model follows the steps. The keyword is
  matched by the model, not by code, and no run with a model has used it
  yet.

### A model in a loop is stopped, and the design schema is nearly halved (2026-10-10)

- **A guard on every tool (`kairo.guard`).** The owner's concern is a
  model on a paid service calling tools without end. Two limits now
  cover every tool, where before only the write tools had one:
  - the same call with the same answer three times running is refused
    the fourth time (`E_REPEATED_CALL`). A call that fails the same way
    each time counts too. A call whose answer changed, as after the
    person edits the schematic, is not a loop and starts the count
    again;
  - more than 150 calls in any ten minutes are refused until the ten
    minutes pass (`E_TOO_MANY_CALLS`), which catches a loop that
    alternates between calls.
  Each refusal tells the model to stop and tell the user. A refused
  call is not counted, so the limit clears on time.
- `--calls-per-ten-minutes N` sets the second limit, and 0 turns it
  off, for a model that costs nothing to run. The local-model harness
  runs with it off.
- **`design_schema` gives what writing a design needs in 10,027
  characters, where it gave 18,083.** The patch operations, which only
  an edit needs, are now asked for with `part: "patch"` (5,802
  characters). The titles pydantic adds, which repeat each field's
  name, are left out, and the descriptions' indentation is closed up.
  Nothing a field means was removed; a test checks that. A chat client
  sends the schema again with every turn, so this saves about 8,000
  characters on each.
- Not done: the same trimming of the other tools' descriptions (about
  16,000 characters at the write tier), and a rerun of the local-model
  attempt with the smaller schema.

### A harness for local models, and its first attempt (M1.9, partly; 2026-10-10)

- **`evals/model_run.py`** gives a model served by Ollama on this
  machine one reference design's spec in words and Kairo's tools, as an
  MCP client would offer them, and lets it work until it stops or runs
  out of turns. It saves every tool call with its arguments and result,
  the tokens Ollama counted, the time, and KiCad's netlist of whatever
  was built. `rescore` works the verdict out again from those files
  with no model, no KiCad and no Ollama.
- **A design passes** if Kairo accepted the model's last write and
  KiCad's netlist of it is the reference circuit whatever the model
  named its parts and nets: the same symbols joined the same way by the
  same pin numbers, found by pairing parts and nets with a backtracking
  search (`matches`). **A refusal passes** if Kairo refused for the
  expected requirement and nothing was written; a model that loosens
  the requirement until something is written misses.
- **First attempt, one run, not a comparison:** `qwen3.5:9b` on the RC
  low-pass. A miss. It called the schema, searched symbols and
  footprints, read pins from the library and planned three times, all
  well formed; it left out the two headers the spec asks for, so Kairo
  reported both end nets as joined to nothing, and it added a
  requirement the spec does not state. It then stopped without
  building. 10 tool calls in 8 turns, 81,852 prompt tokens and 21,238
  output tokens, 30 minutes on this laptop's 8 GB GPU.
- **The miss is partly the harness's.** It passed the model at most
  12,000 characters of each result, which cut the 18,083-character
  design schema short. The limit is now 25,000, Kairo's own bound. The
  run was not repeated. The script also stopped with a Windows error
  while removing its temporary folder, after every record was saved;
  that is fixed, and the run's summary, written afterwards from its
  records, says so.
- **What it shows about cost:** the design schema is the largest thing
  a model reads (18,083 characters), and a chat client sends it again
  with every later turn. Trimming it is the first place to save tokens.
- A run now saves its transcript after every tool call and prints each
  call as it happens.
- Not done: the plan's three attempts per design and model, the other
  eleven designs, the other local models (`qwen3.8:27b` and
  `gemma4:12b` also offer tool calling), and any hosted model.

### Kairo's own review of a design: faults and suggestions (M2.5, partly; 2026-10-10)

- **`kairo.verify.review`** reads a design by rules of Kairo's own, so a
  result depends less on the model that wrote the design. It looks only
  at what a netlist holds (each pin's net and type, each part's symbol,
  the net roles the design states), so it reads a design Kairo built
  and a schematic a person drew alike, and says nothing of how either
  is drawn. `analyze_power_tree` and `check_schematic` return it.
- **Two kinds of remark, and neither blocks a write.** A fault does not
  work as drawn, whoever drew it. A suggestion is where the design may
  well be sound and Kairo would do it another way, and says what Kairo
  would do. This is the owner's request of the same day.
- **Faults:** an input alone on its net (TI SCBA004); a rail that feeds
  chips with no capacitor to ground and no part that could be one
  (Analog Devices MT-101).
- **Suggestions:** fewer decoupling capacitors than supply pins; an I2C
  line with no pull-up drawn (NXP UM10204); an open-collector output
  with nothing drawn to set its other level; an LED's symbol straight
  across a rail; a USB data line from a connector with no protection
  part drawn (IEC 61000-4-2).
- **The rules were tightened against real boards before they were
  kept.** In a trial that stated `GND` as ground and each net named
  like `+3V3` as a supply (the trial's doing, read off the names; Kairo
  itself never does), the first version called 65 faults across the
  twelve real boards, and nearly all were sound designs: boards that draw
  capacitors and resistors with symbols of their own, open outputs that
  sink an LED or rely on a pull-up inside the chip they drive, and a
  "TVS 5V" protection diode drawn with the LED symbol across its rail
  (VESC6, D5). Now a part Kairo does not recognise is taken as possibly
  the cure, a line that leaves through a connector is left alone, and
  what rested on a guess is a suggestion. The same boards now get 2
  faults (two inputs alone on their nets, on the OSM-L board) and 22
  suggestions; seven of the twelve get no remark at all.
- A review lists at most 15 of each kind, with the totals, to keep its
  cost to a client small. With no net roles stated, the four rules that
  need supplies are not run and the review says so.
- Seeded tests: each rule is caught when its fault is put into a sound
  design, the sound design gets no remark, and a part of unknown kind
  silences the rule it could cure.
- Not built of the plan's M2.5: reset and boot pins, floating enables
  beyond a lone input, and ESD on connectors other than USB. Those need
  datasheet facts about each pin, which a netlist does not hold.

### Opening a project in the KiCad window (2026-10-10)

- **`open_in_kicad` (read tier)** starts the KiCad installed beside
  kicad-cli on a project, so a person can look at what Kairo built or
  changed. If KiCad's own lock file shows the project is open already,
  no second window is started and the result says who has it open.
- **It is the fallback for a live view, which KiCad 10 does not allow.**
  Measured on KiCad 10.0.6 with the API server on and a schematic open:
  the schematic editor answers `GetOpenDocuments` and has no handler
  for `RefreshEditor`, `RevertDocument`, `GetItems`, `GetSelection`,
  `GetTitleBlockInfo` or `RunAction`. KiCad's developer documentation
  gives schematic editor support to KiCad 11. So every result tells
  the person to revert or reopen the sheet after a later change.
- Tried for real on this machine: a built regulator project opened in
  KiCad, the lock file `~ldo_3v3.kicad_pro.lck` appeared, and a second
  call started nothing. The tests use a stand-in launcher and never
  open a window. `kairo.kicad.gui` holds the lock file's layout, read
  from one KiCad 10.0.6 wrote; the schematic editor started on a lone
  sheet with no project file left no lock.
- Not built: opening the window by itself after each write, and
  anything on KiCad 11.

### A project's thermal policy (2026-10-10)

- **A design may state `policy: {theta_ja_margin: "20%"}`.** A
  datasheet gives theta_JA as a typical value on the JEDEC test board,
  never as a limit, so until now no junction temperature could be
  proven from one. With the policy stated, the check reckons theta_JA
  from the datasheet's value up to that value plus the margin, and can
  pass or fail on it. This is the owner's decision of the same day.
- Kairo chooses no margin. With no policy the result is `unverified`
  as before, and a part whose theta_JA the design states as a maximum
  is not widened by the policy.
- **A result that used the policy says so**, in its summary and in the
  source of its theta_JA input, and the accuracy report (the tool's and
  the file in the project) has a "Project policies in use" section
  saying that a pass under it is not a pass against a datasheet limit.
- `check_schematic` takes the policy among its facts.
- Worked by hand in the test: 0.6573 W at 90 degC/W plus 20% from 40
  degC is 111.0 degC, and plus 50% is 128.7 degC, which fails a 125
  degC limit. The reference designs state no policy, since the margin
  is a project's to choose; 12 of 12 with identical records.

### A filter's attenuation as a requirement, and the filter refusal (M1.9, partly; 2026-10-10)

- **`min_attenuation`** is a fifth requirement type: a first-order RC
  low-pass between two nets must lose at least so many decibels at a
  frequency. It is judged with the resistor and capacitor at the bottom
  of their tolerances, where the loss is least, through a new
  calculation, `rc_lowpass_attenuation`
  (`10 log10(1 + (2 pi f R C)^2)`), which has its interval enclosure
  like the rest (`log10` was added to the interval arithmetic).
- It is `unverified`, naming what is missing, when a tolerance is not
  given, when the capacitor is a class II ceramic or has no stated
  dielectric (the capacitance it keeps is not worked out here), or when
  anything but the resistor, the capacitor and connectors is on the
  output. An unloaded output is the worst case for a passive load,
  which only adds loss; the reasoning is in the check's docstring.
- **Reference design 12, the filter refusal** (`rc_lowpass_refusal`):
  40 dB wanted at 10 kHz from 1 k and 100 nF, which give 16.07 dB at
  nominal and 15.55 dB at worst, both worked by hand in the tests. Kairo
  refuses with that arithmetic and writes nothing. The recorded run is
  12 of 12, and the other eleven records are identical to the run
  before.
- `dB` is a unit a design may write.
- Remaining of the plan's thirteen: buck, charger and hold-up.

### A rail's worst-case current where it feeds resistor branches (2026-10-10)

- **A junction temperature can now be proven on a rail that also feeds
  a resistor to ground or an indicator.** The rail's greatest current
  is the stated loads plus each branch at its own worst case: highest
  rail voltage, lowest resistance and, through a diode, lowest forward
  voltage, each from a stated tolerance. Before, any branch left the
  current with no tolerance and the junction `unverified`.
- A missing tolerance is named (the rail's, the resistor's, or the
  diode's `v_f_tolerance`) and the result stays `unverified`. Any other
  kind of draw on the rail (a chip's supply current, another
  regulator) is not yet given a greatest value.
- **The figure is a bound, and is treated as one.** A branch draws most
  at the highest output voltage and the regulator drops most at the
  lowest, so the sum is above the true worst case. It can prove a
  limit is met. Where it exceeds the limit and the nominal value does
  not, the result is `unverified`, not `fail`; a test puts a limit in
  the gap (99.43 degC between a true 99.42 and a bound of 99.44).
- Checked by hand in the test: 1.5818 mA for the indicator, 99.44 degC.
  The reference designs' results are unchanged (11 of 11): none states
  the tolerances this needs.

### A datasheet limit says which side it is (2026-10-10)

- **`limits` is now a mapping**, `{"i_q": "max", "i_out_max": "min"}`.
  A check that needs a value's maximum takes it with no further
  tolerance only when the design states it as the maximum. Before, any
  value marked as a limit was trusted whichever side it was, so a
  stated minimum quiescent current could prove a maximum junction
  temperature; a test now shows that case is `unverified`. A plain list
  of names is still read, proves nothing, and the result says how to
  write the mapping.
- The two reference designs that state limits now say which side each
  is, read from the datasheets as downloaded that day (the same hashes as
  before): LM1117 (SNOS412Q, p.6) quiescent current
  10 mA maximum, dropout 1.25 V maximum at 500 mA, current limit 800 mA
  minimum; LM317 (SLVS044Z, p.6 and p.8) adjustment pin current 100 uA
  maximum, input-to-output differential 3 V, current limit 1.5 A
  minimum. 11 of 11 again, with identical records.
- Only the junction-temperature check reads `limits` so far, and it
  needs maxima; no check yet needs a minimum.

### Checking a schematic a person drew, and two faults in the power tree (M2.4, a second slice; 2026-10-10)

- **`check_schematic` (propose tier)** reads a drawn schematic's parts
  and connections from KiCad's netlist into a design
  (`kairo.kicad.drawn`), adds the facts the caller states (net roles and
  voltages, parts' datasheet values, loads, requirements), and returns
  the rails, the regulators and each requirement's result. Nothing is
  guessed: a net is a signal until its role is stated, and the nets that
  have supply pins and no stated role are listed. A pin KiCad marks as
  left open goes to `no_connect`.
- Measured: all twelve boards saved by the KiCad 9 and 10 programs
  import, with every part of KiCad's netlist kept (68 to 805 parts).
  Two things in them a design could not hold before, and now can: a
  reference with dots in it (`C.0.1`, on a sheet used several times),
  and a symbol whose library name is empty.
- **Fault fixed: a capacitor marked with no unit was read as a
  resistor.** The power tree read any two-pin part's value as ohms, so
  "100n" across a rail was 100 nanoohms and the rail's current came out
  in millions of amps. Found on the first real board. A value is now
  read as a resistance only for a resistor's symbol.
- **Fault fixed: an indicator's current was counted as known when a
  chip's pin shared its net.** A resistor and a diode in series are now
  solved only when nothing else is on the net between them.
- **A two-pin part of no known kind on a rail is named.** A bead, a
  fuse or a diode leading off a rail was left out without a word. It is
  now counted at its stated `i_supply` or listed as unknown, which
  makes the rail's total a lower bound. Capacitors and KiCad's
  connector libraries are still taken to carry no current of their own.
- The reference designs were run again: 11 of 11, and every design's
  record is identical to the run before, so the fixes changed no
  recorded result. (None of the eleven has a bare-valued capacitor or a
  shared indicator net.)
- A review of the calculations against their sources found the
  formulas, constants and equation numbers of TI SLVA477C, SLVA372D and
  SLVA689 as cited, IPC-2221's current constants and revision B's
  spacing table as held, and the second-order formulas correct.
  SLVA689 is now revision A (October 2026); its equations and numbers
  are unchanged.
- Not built: adding, removing or reconnecting parts in a drawn
  schematic, the ERC baseline, the surgicality bound, simulating a
  drawn board, and keeping facts between calls.

### Text placement looks only at nearby parts (2026-10-10)

- **Placing text on a large sheet is about 30 times faster.** Each
  candidate spot was compared with every part on the sheet; it is now
  compared with the parts in the grid cells around it. A part can only
  matter to a spot if it is closer than the clearance or than the
  spot's own part, so the result is the same: a test prices every
  candidate of five kinds of sheet both ways and they agree exactly.
  Measured on an 86-part sheet, the same score in 2.9 s against 87 s
  under the profiler; 34 parts in 1.7 s against 7.6 s.

### Changing a value in a schematic a person drew (M2.4, a first slice; 2026-10-09)

- **`set_value` (write tier)** changes one part's value in any KiCad
  schematic, by replacing one string in the lossless tree, so every
  other byte of the file is its author's. KiCad then confirms it: the
  netlist's connections are the same, every other part's value is the
  same, and the rule check gives the same counts as before. If any of
  those is not so, the file is put back. `undo` restores it.
- Measured on a real five-sheet board (`git4dcc-rtb-c13`): one line of
  5,800 changes, KiCad confirms, and undo gives back the same bytes.
- A part on a sheet that is used more than once is refused: one value
  serves several parts there.
- This is not the plan's editing of drawn schematics, which imports the
  schematic to a design and writes back only the changed part of the
  tree. Adding, removing and reconnecting parts in a drawn schematic is
  not built.

### Three more reference designs, eleven in all (M1.9, more of it; 2026-10-09)

- **A USB-C power sink** (receptacle, CC pull-downs), **a non-inverting
  op-amp stage** and **an ATtiny85's minimum system**. The last two
  passed first time.
- **A fault found by the USB-C design and fixed:** KiCad's symbol stacks
  its four VBUS pins on one spot, and Kairo drew four power symbols on
  top of each other, leaving three names with no room. Pins of one part
  stacked on one spot on one net now share one stub; KiCad's netlist
  still lists all four on the net. The run before the fix is recorded
  (10 of 11) beside the one after (11 of 11).
- The grounding step already refused `J1.VBUS` as ambiguous and asked
  for `J1.VBUS*`, which is what the design now says.
- The harness no longer stops when a design fails before any layer runs.
- Left of the plan's thirteen: buck, charger and hold-up, and the filter
  refusal.

### DC sweeps, temperature, part dissipation, two more exact circuits (M2.2, more of it; 2026-10-09)

- **`dc` analysis**: the steady state as one source is swept, with each
  net's lowest, highest, first and last voltage and a graph against the
  swept voltage. On the regulator example it shows the output following
  the input less the dropout until it reaches 3.3 V, on both simulators.
- **A run's temperature** can be set (`temperature` on the analysis). It
  reaches only models that carry temperature data.
- **`power`** joins the steady state's numbers: each two-pin part's
  voltage times its current, which a limit can bound.
- **The accuracy set has seven circuits.** Added: a buck cell with a
  near-ideal switch and diode, checked against the piecewise-linear
  ripple formulas, and a resistor with a temperature coefficient at 127
  degC. Both simulators are inside every budget with the `accurate`
  settings. One measurement is worth knowing: on the buck cell's output
  ripple Xyce is within 0.8%, and within 0.003% with tighter settings,
  while ngspice is 2.0% low and, tighter, 1.7% high; its error does not
  shrink. The budget for that check is the plan's 2.5% for switching
  ripple.
- **CI runs the simulator tests** on Ubuntu's packaged ngspice, in a job
  of its own.
- Not done: Monte Carlo, the interior-extreme search and loop gain
  (accuracy circuits 8 to 10), and the symbolic third check.

### Simulated limits are judged at the worst case (M2.3, the first part; 2026-10-09)

- **A limit can now pass.** `kairo.spice.sweep` takes each limit that
  holds at nominal to the worst case over the stated tolerances: each
  toleranced value is moved to each end to learn which way the number
  goes, the corner that pushes it toward the limit is simulated in full
  (both settings, every simulator), and the corner is confirmed by
  moving each value back in turn. The limit passes only if the corner's
  number is checked and inside the limit. `simulate` does this unless
  the request says `sweep: false`.
- **What is varied:** resistors, capacitors, inductors, fixed-drop
  diodes' forward voltages and sources. Loads are the stated maxima.
  Capacitor ESR, temperature and the insides of maker models are not
  varied, and the result says so.
- **Nothing is assumed.** A value the number depends on that has no
  tolerance leaves the limit `unverified` and is named. A value it does
  not depend on (a signal's amplitude, for a settling time) is found by
  moving it 5% and does not stand in the way. A number that does not
  move one way with a value is `unverified`, since a corner then proves
  nothing.
- **Measured against closed forms** on both simulators: a divider's
  worst-case output over 1% resistors and a 2% supply is 2.4255 V to
  2.5755 V, as the formula gives; a 1k 1%, 100 nF 5% filter's slowest
  settling time is 488 us against 460 us at nominal, so a 470 us limit
  fails and a 500 us limit passes.
- **A fault found by the first test of it:** a resistor whose 1%
  tolerance moved the result by less than the 1% agreement threshold was
  treated as having no effect and left out of the corner, which made the
  worst case too mild. Every toleranced value now goes to an end.
- Not done from M2.3: the search for a worst case that lies between the
  ends, Monte Carlo (report-only), loop gain, temperature corners.

### Maker models, datasheet stability, adjustable regulators (2026-10-09)

- **Maker simulation models can be registered** (`kairo.spice.registry`,
  `--models-dir`). A models folder holds the files and `models.json`:
  the part each stands for (by part number or symbol), the subcircuit,
  which symbol pin each of its nodes goes to (by pin name), the maker,
  the source, the version, the dialect, the simulators it runs on, its
  fidelity class and the file's SHA-256. A file whose hash no longer
  matches is refused with `E_MODEL_CHANGED` until registered again. A
  part with a registered model uses it in place of Kairo's stand-in, and
  every result names the model and its hash.
- **Texas Instruments' LM1117-3.3 model runs through Kairo** ("LM1117 3P3
  Unencrypted PSpice Transient Model", SNOM488, Final 1.1). The file is
  TI's to distribute and is not in the repository;
  `evals/simulation/models/lm1117-3.3.json` holds its facts so anyone can
  fetch and register it. Measured: ngspice 47 runs it unchanged in its
  PSpice compatibility mode. Xyce 7.10 needs three syntax edits to read
  it and then does not converge, so it is recorded as running in ngspice
  only. By the plan's rule a model run through a compatibility mode
  counts for nothing until compared with the simulator it was written
  for, and PSpice is not installed, so every number from it is
  `unverified` and says so.
- **A regulator's output capacitor is checked against its datasheet's
  stable region** (`regulator_stability`, reported for every regulator).
  The regulator states the least capacitance and the ESR range it is
  stable with; the capacitor states its ESR range. Nothing is assumed:
  with no region, or one ESR value instead of a range, it is
  `unverified`. This is the fault a steady-state check misses: the
  LM1117 needs 0.3 to 22 ohm of ESR (datasheet SNOS412Q, page 15), which
  a ceramic output capacitor does not have.
- **An adjustable regulator's output is worked out from its divider**
  (`adjustable_regulator_output`: Vout = Vref (1 + R2 / R1) + Iadj R2,
  LM317 datasheet SLVS044Z, equation 2), with the reference's and the
  resistors' tolerances. The divider is found from the connections.
- **A supply from outside can state its tolerance** (`tolerance` on a
  net), and **a part can mark values as datasheet limits** (`limits`), so
  a maximum quiescent current is not asked for a further tolerance.
- **A tantalum, electrolytic, polymer or film capacitor** counts at its
  marked value less its tolerance; before, anything that was not C0G was
  treated as a class II ceramic needing a DC-bias curve. Capacitors are
  recognised by their symbol, no longer by a reference starting with C.
- **Two reference designs rewritten from datasheets, one added.**
  `ldo_lm1117` takes every regulator value from TI's LM1117 datasheet,
  with a tantalum output capacitor: its rail, capacitance, load and
  stability pass, and its junction temperature is rightly unverified
  (the datasheet gives one thermal resistance, with no range).
  `ldo_adjustable`, with the LM317's datasheet values, is now a refusal:
  the reference alone is 4% wide, so with 1% resistors the output is
  4.704 V to 5.323 V and cannot be held within 5% of 5 V. Eight designs,
  8 of 8 in the recorded run.
- **Measured with TI's model on the LM1117 design:** at rest 3.291 V;
  with the supply rising in 100 us the output starts once the input
  passes 1.7 V and settles with no overshoot; plugged into a live 5 V
  supply through 1 uH and 50 mohm the input rings to 8.9 V. The run with
  tightened settings still fails to converge on that last one.

### Conductor spacing, and the file-write rule made exact (2026-10-09)

- **`conductor_spacing_internal` and `conductor_spacing_external_uncoated`**
  give IPC-2221's least spacing for a voltage (DC or AC peak), 30
  calculations in all. `kairo.calc.spacing` holds Table 6-1 for both
  revision B (2012) and revision C (December 2023), every column, with
  the per-volt additions above 500 V. The two named calculations take
  the larger of the two revisions' values: at 48 V on an outer layer
  that is 0.64 mm (revision C) against 0.6 mm (revision B).
- **Where the table came from.** Not from the standard, which is paid:
  revision B from the picture in Altium's article "Using an IPC-2221
  Calculator for High Voltage Design", revision C from the picture in
  Siemens' article "PCB high voltage spacing: what every engineer should
  know", printed there with IPC's permission. Tested against the printed
  values and against Siemens' worked example (B1 at 600 V is 0.50 mm).
  Revision C's coated and component-lead columns are held but not
  offered as named calculations, because its picture does not say what
  each column is for and values have moved between columns since
  revision B.
- **The rule that every file write goes through the transaction module
  now says "in a project folder".** Kairo's own cache folder (the
  library index, drawings, simulation graphs) is written directly, as
  the index and drawings always were. Backups and undo are for the
  user's files. Decided by the owner.

### Fixes from a review of the simulation code (2026-10-09)

- **A rail whose regulator has no model is reported as undriven.**
  Before, such a rail got no source and the reason given was only that
  the regulator had no model. Nothing is put in the regulator's place.
- **A stimulus on a net a regulator already makes is refused**, instead
  of putting two sources on one net.
- **A parts file or DC-bias curve that cannot be used** (for example a
  curve that stops below the operating voltage) leaves the capacitor at
  nominal with the reason in its model note, instead of failing the call.
- **The steady state can be held to limits**: `voltage`,
  `supplied_current` and `current` join the metrics a limit can name.
- **A limit's bound is checked with the request**, so a wrong unit is
  refused before any simulator runs.
- **Settling time is counted from when the step begins**, not from the
  start of the record.
- **A run that stops before its end is a failed run**, never measured as
  if it were whole.
- **`boost_peak_switch_current` refuses a duty cycle of 1 or more**, and
  a tolerance range that reaches 1 gets no worst case and says why.
- **Each recorded reference run keeps the expectations it was judged
  against**, so a later change to a reference design cannot change an
  old run's verdict. The first run of the three new designs was judged
  before this, and its two changed expectations were written back into
  it from this session's record of them.
- **The four runs of a simulation are made side by side**, which roughly
  halves the wait.
- Left as it is, for the owner to decide: `simulate` writes its graphs
  into the cache folder directly, while the project rule says every file
  write goes through the transaction module (A111).

### Three more reference designs, seven in all (M1.9, more of it; 2026-10-09)

- **An adjustable regulator** (LM317 with its feedback divider), **an I2C
  temperature sensor** with pull-ups and an unused output marked not
  connected, and **reverse-polarity protection** with a series Schottky
  diode.
- **Two runs are recorded.** The first scored 5 of 7. One miss was a
  fault: on the sensor design a ground symbol in the middle of a
  four-pin header had no room for its name, which was hidden and
  reported. A power symbol on a pin with connected neighbours one pitch
  away on both sides now gets a longer lead; a test over every reference
  design fails without it. The other miss was the expectation, not
  Kairo: the adjustable regulator's spec gives no current rating, so a
  third requirement is rightly unverified, and KiCad's netlist gives a
  pin marked not connected a net of its own. The second run scores 7 of
  7. Still a scripted baseline: no model was called.
- The adjustable regulator's two requirements are expected to be
  `unverified`, and are: Kairo does not work out an adjustable
  regulator's output from its divider yet.

### Boost converter, trace width and trace resistance calculations (M1.6, more of it; 2026-10-09)

- **Nine more calculations**, 28 in all: `boost_duty_cycle`,
  `boost_inductor_ripple`, `boost_inductor` and
  `boost_peak_switch_current` (TI SLVA372D, equations 1, 2, 5 and 4);
  `trace_width_external`, `trace_width_internal` and their inverses
  `trace_current_external` and `trace_current_internal` (IPC-2221's
  curve fit, with the constants the plan gives); and `trace_resistance`
  (annealed copper, IEC 60028). Each runs on the worst-case engine with
  its directions declared and checked over a grid.
- **Lengths are quantities in millimetres** (`0.25mm`), with no prefix,
  so `1m` still means a thousandth.
- **Intervals can be raised to a power**, which the IPC-2221 formula
  needs, with each end rounded outward.
- Tested against hand arithmetic. For the trace width, also against the
  figure calculators built on IPC-2221 are usually shown with: 1 A at 10
  degC of rise in 1 oz copper needs 11.8 mil outside and 30.8 mil inside.
  SLVA372D gives equations and no worked numbers.
- **Fixed: a resistor from a rail to a signal net, or between two rails,
  was left out of the rail's current without a word.** The rail's total
  then looked complete when it was not. Such a resistor is now listed as
  unknown, the total is marked a lower bound, and the result points to
  `simulate` for the current. Found while writing the adjustable
  regulator reference design, whose feedback divider is such a branch.
- **Not written: `conductor_spacing`.** It is a table in IPC-2221, a
  paid standard Kairo's author has not been given. Published summaries
  agree in part only, and a table typed from memory is exactly the kind
  of value Kairo does not make up. It needs the owner to supply Table
  6-1.

### Simulation: steady state, response in time and frequency, with graphs (M2.1 and M2.2, partly; 2026-10-09)

- **`simulate` (propose tier)** runs a design in ngspice 47 and Xyce 7.10
  and returns each net's voltage and each part's current at rest (`op`);
  peak, overshoot, rise time, settling time and peak source current for a
  step (`tran`); or gain and cutoff against frequency (`ac`). Graphs, the
  curves as CSV and the netlist are saved in the cache folder.
- **A design becomes one circuit** (`kairo.spice.circuit`) with a writer
  for each simulator's dialect. Every connected pin reaches its net
  through its own 0 V source, so any pin's current is read the same way
  from either simulator, and the netlist's connections read back equal to
  the design's (tested for both dialects, with a moved pin caught).
- **What stands in for each part is recorded**: ideal resistors,
  capacitors and inductors; a capacitor at its bias-adjusted value when
  the parts folder has its curve, with its stated ESR in series; a diode
  as its stated forward drop; a linear regulator as a source limited by
  its stated dropout. A part with no model is left open and named, and
  every number is then `unverified`.
- **Every run is made twice**, once with the `accurate` settings and once
  ten times tighter with the step halved. A number that moves by more
  than its threshold is `numerically unstable`; one the two simulators
  disagree on says so. Kairo's own reader takes both simulators' raw
  files, text and binary (fixtures written by both are in
  `tests/fixtures/spice`), and one measurement library computes every
  metric, so neither simulator's own `.meas` is used.
- **The accuracy set** (`evals/accuracy`) has five circuits with exact
  answers: a resistor ladder, an RC step, an underdamped RLC step, an RC
  low-pass sweep, and a diode with a resistor solved by the Lambert W
  function. The `accurate` settings (reltol 1e-4, trapezoidal, at least
  2000 steps) are the loosest of six tried at which every check is inside
  its budget on both simulators; the measurements are in
  `evals/accuracy/results`. With settings close to SPICE's defaults the
  RLC step is 5 to 8 times over budget on both.
- **Design-practice remarks** (`kairo.spice.practice`), each citing its
  source: ringing when a live supply is plugged into a ceramic capacitor
  through lead inductance (Analog Devices AN88); inrush current (TI
  SLYT332); an overshoot expressed as the damping ratio and phase margin
  of the second-order response with that overshoot (TI SLVA381B for the
  45 degree floor); a capacitor's stated rating exceeded at the peak; a
  rail above its required band on the way up.
- **Measured on the reference designs** (`evals/simulation`): the
  regulator at rest gives 5 V, 3.3 V and 301.3 mA on both simulators,
  equal to the calculation. An RC filter's rise and settling times equal
  2.197 RC and 4.605 RC. A hot-plug test circuit's peak equals the
  closed-form 1.6 times within 0.3% on both simulators. Plugging the
  regulator example into a live 5 V supply through 1 uH and 50 mohm rings
  its input to about 10 V and drops the 3.3 V rail out twice; adding 1
  ohm in series removes the ringing. Those two are `unverified`, because
  the regulator is a behavioural model.
- **Known limits.** Everything is at nominal values: tolerances are not
  swept, so a limit that holds is reported as not a pass. The regulator
  model does not converge reliably on edges faster than about 10 us:
  on the hot-plug run ngspice's tightened run and both of Xyce's fail,
  and the result says so. The fixed-drop diode's own drop is about 5 mV,
  which makes the LED current 0.4% low. Not done from the plan: vendor
  model files registered by hash, `Sim.*` fields written into the
  schematic, the cross-check against kicad-cli's own SPICE netlist, the
  `.dc` sweep, the symbolic third check, and accuracy-set circuits 6 to
  10 (buck cell, temperature coefficient, Monte Carlo, the interior
  extreme, loop gain).

### Reference designs and a scoring harness (M1.9, partly; 2026-10-09)

- **Four reference designs** in `evals/reference_designs`, each with a
  spec in words, a known-good design and an expected outcome: the 3.3 V
  regulator, an RC low-pass filter, an LED indicator, and a refusal (the
  same regulator asked for 900 mA, which cannot stay under its junction
  limit).
- **`evals/reference.py run`** builds each with the installed KiCad and
  saves what the verdict rests on: the design, KiCad's netlist as text,
  the rule check's counts, each requirement's result and the schematic.
  **`rescore`** recomputes every verdict from those files alone, with no
  KiCad, no libraries and no model, and must agree with the run.
- **A strict verdict**: for a design, every verifier layer passes,
  KiCad's rule check is clean, KiCad's netlist is exactly the expected
  one, the unverified requirements are exactly the expected ones, none
  fails, and no text overlaps or is hidden. For a refusal, the expected
  requirement is the one that fails and nothing is written.
- **Three runs are recorded**, and all three are kept. The first scored
  2 of 4 and the second 3 of 4, because the two new designs found two
  faults: a net drawn with power symbols was not flagged as driven when
  no part on it had a supply pin (KiCad's rule check: power pin not
  driven), and a power flag was put between two connector pins 2.54 mm
  apart, where its name had no room. Both are fixed; the third run
  scores 4 of 4. This is a scripted baseline: no model was called.
- Not done, so the milestone stays open: 9 more reference designs; a
  holdout set, which has to be written by someone other than the one
  developing against it; and runs with a model under the agent eval
  protocol, which need the owner's choice of models.

### Design tools on the server, in tiers (M1.8, 2026-10-09)

- **Three tiers.** `--tier read` (the default) offers the tools that
  look; `propose` adds checks and calculations that write nothing;
  `write` adds building and changing projects. Eighteen tools in all.
- **New tools**: `design_schema`, `accuracy_report` (read); `calc`,
  `analyze_power_tree`, `plan_design`, `verify_design` (propose);
  `apply_design`, `edit_design`, `undo` (write).
- **The whole loop, through the MCP client with real KiCad**: read the
  schema, plan the regulator example, apply it, read it back with the
  read tools (6 parts, 4 nets, rule check clean), verify, change R1 to
  2k2 (4 lines changed, under 1% of the file), read the change back,
  undo, and read the original back.
- **An unmeetable requirement is refused with the arithmetic** and
  nothing is written: a 900 mA load puts the regulator's junction at
  least at 177.9 degC against 125, and the reply carries the equation
  and inputs and tells the model not to loosen the requirement.
- **A model cannot loop.** Each project has 12 write attempts per server
  run. After 3 failed attempts in a row that do not beat the best
  verifier score so far, the reply says stop, and further attempts on
  that project are refused with `E_NO_PROGRESS`, a correct design
  included, until the user is consulted. Both rules are tested.
- **Accuracy travels with the design**: `apply_design` puts the status
  and what to resolve first in its reply, and saves the same as
  `kairo/accuracy-report.md` in the project. For the example it names
  U1's quiescent current and C2's missing DC-bias curve.
- Every reply is under 25,000 characters on the example, checked in the
  loop test.
- The extension now asks for the tier when installed.
- Not done: the loop has been driven by the MCP library's client, not
  yet by Claude Code itself, which is the owner's to try; a design
  session lives in the server's memory, so a project built in an earlier
  run cannot be edited; the schema is a tool, not an MCP resource.

### The power tree and requirement checks (M1.7, 2026-10-09)

- **Power tree** (`kairo.power`): each rail's current from the loads the
  design states, resistors to ground, a resistor through an LED to
  ground, each part's stated supply current, and each regulator's input
  current. A part whose current is not known makes its rail's total a
  lower bound and is named with what to supply.
- **Requirements, in three states** (`kairo.verify.requirements`):
  `pass` only when the worst case over every input's tolerance is
  proven; `fail` when a reached value breaks the limit; `unverified`
  when an input is missing, naming each one and its part.
- **On the plan's example**: 301.3 mA on the 3.3 V rail; the regulator's
  junction is at least 86.1 degC with its quiescent current named as
  missing, so the requirement is `unverified`, as the plan intends; the
  capacitance requirement is `unverified` naming C2 until a part number
  with a curve is supplied, and then passes or fails from the curve.
- **Never a pass on a missing tolerance**: with every tolerance given, the
  LED resistor's power requirement is proven; remove the resistor's
  tolerance and it becomes `unverified`.
- **Seeded faults fail**: a 2 A load on a 1 A regulator; a junction whose
  lower bound is already past the limit (a failure, not merely
  unverified, since the missing term can only add); a capacitor that
  keeps too little under bias; and a resistor that passes at nominal and
  fails at the worst corner of the tolerances.
- A regulator's load against its rating is checked whether or not a
  requirement asks.
- The design gained three parameters: `regulator` (linear or switching),
  `i_supply` and `v_f_tolerance`. The plan's example did not say U1 is
  linear; the reference design now does.
- Limits: a rail that feeds resistor branches has no worst-case current
  yet, so a junction requirement on such a rail stays `unverified` even
  with every tolerance given; an LED's direction is not checked; other
  circuit shapes are not solved and are reported as not established.

### The calculation engine (M1.6, partly; 2026-10-09)

- **Worst cases** (`kairo.calc.worst_case`): a formula's range over its
  inputs' tolerances, three ways. Where a formula is declared to move one
  way with an input, trying the ends is exact. Where it is not, a
  bounded, repeatable search looks inside the range. And the same formula
  run on intervals with outward rounding (`kairo.calc.interval`) gives a
  range sure to contain every result.
- **Judging a limit**: it fails if a reached value breaks it, passes if
  the corners are exact and meet it or the enclosure meets it, and is
  otherwise `unverified`: a search that found no failure proves nothing.
- **Sixteen calculations** (`kairo.calc.catalogue`), each written once so
  the same code gives the nominal value, the worst case and the
  enclosure, each naming its source: resistor dividers (plain and
  loaded), LED current and resistor power, I2C pull-up limits, RC cutoff
  and gain, LC resonance, a regulator's feedback divider, a stress
  ratio, and four of a buck converter's power stage.
- **Checked**: every declared direction holds over a grid of points;
  every worst case lies inside its enclosure; an extreme inside a range
  (a buck inductor's dependence on output voltage, which peaks at half
  the input) is found by search and missed by corners; a formula used
  outside the values it is for (an LED fed below its forward voltage) is
  refused, at nominal and across the tolerances.
- **Sources read**: TI SLVA689 for the I2C pull-up equations, whose
  worked example (966.667 ohm and 1.77 kohm) the code reproduces; TI
  SLVA477C for the buck equations, read from the rendered pages. The
  buck note has no numeric example, so those tests are worked by hand.
- **Ceramic capacitors** (`kairo.calc.capacitor`): the least capacitance
  in use, from tolerance, the part's DC-bias curve and the dielectric's
  temperature limit. With no curve it is `unverified` and names the part
  and what to supply; there is no default bias factor. A result from a
  curve is marked typical. Bias is read at the top of the rail's
  tolerance.
- **Regulator stability**: whether the output capacitor's capacitance
  and ESR sit in the datasheet's stable region; with no region or no ESR
  it is `unverified`. A seeded ceramic-ESR fault is flagged.
- **Part facts** (`kairo.parts`): one JSON file per part number; a value
  or curve with no source is refused when read; extracted values are
  `unreviewed` until a person says otherwise.
- **Not done**, so the milestone stays open: no manufacturer's DC-bias
  curve has been obtained (the test curve is made up and says so), so
  "matches a manufacturer's published curve at two bias points" is not
  met; most formulas' sources give no worked numbers, so "two worked
  examples from its cited source" is met only for the I2C pull-up
  (one example from the source, one from its table); `trace_width`,
  `boost_stage`, `conductor_spacing` and `trace_resistance` are not
  written; `ldo_thermal` is not yet run through the worst-case engine.

### The verifier's layers (M1.5, 2026-10-09)

- **Structure, L2** (`kairo.verify.structure`): a supply pin on no net or
  on a signal net, two outputs on one net, a floating input, a net with
  one pin, any other open pin, and (a warning, and switchable) a supply
  pin of an integrated circuit whose net has no capacitor to ground.
  Each finding names the part, pin and net and says what would fix it.
- **Open pins are a decision**: a design lists pins meant to be left open
  under `no_connect`, and the writer marks them. An open pin that is
  neither listed nor typed no-connect by the library is a fault.
- **The drawing, L2** (`kairo.verify.layout`): overlapping symbols,
  anything off the sheet, text over text, a symbol or a wire, and a
  reference or value nearer another part than its own. The checks read
  what the writer reports it drew, and the writer's own drawing of the
  example passes them.
- **The report** (`kairo.verify.report`): each layer is `pass`, `fail`,
  `unavailable` or `not run`; a design passes only if every layer ran
  and none found an error; findings come 20 to a page, by layer; and a
  score from 0 to 1 is given for a repair loop to watch, marked as not
  the verdict.
- **`build_project` runs the layers in order and stops at the first that
  fails.** Two regulators' outputs on one net now stop at L2, before
  anything is drawn or KiCad is asked. A logic gate's output tied to the
  ground rail passes L1 to L3 and is caught by KiCad's rule check at L4,
  and nothing is kept.
- **Seeded faults**: 7 structure faults and 7 drawing faults, each found
  at L2 with the right part, pin and net; with the pin, pad and netlist
  faults from earlier stages that makes every layer from L1 to L4.
- Fixed: a pin with no name could not be reached when its symbol's other
  pins had names (a 74LVC1G04's input and output). A pin number is now
  accepted for any pin that has no name of its own; a named pin must
  still be called by its name.

### Text placed where it can be read (M1.10, 2026-10-09)

- **KiCad's text size, measured** (`kairo.text_placement.kicad_text`):
  every printable ASCII character's advance, from the lengths KiCad
  states in its own SVG drawings. Each is a whole number of 21sts of the
  text size, plus a constant 0.16 of the size per text. Checked on three
  real boards: every plain text at the default 1.27 mm size (5,139 on
  the HackRF Pro alone) has exactly the predicted length. At other sizes
  the table is a few percent off, and Kairo writes only the default.
  `evals/text_placement/calibrate.py` repeats the measurement, and a
  test fails if the installed KiCad disagrees with the table.
- **Fields placed by the text placer** (`kairo.kicad.field_placement`):
  each shown reference and value, and the name of each power symbol and
  flag, is a label owned by its symbol; symbol bodies, wires and net
  labels are obstacles. The writer hands these to the existing placer
  and writes each field centred on the spot found.
- **Checked against KiCad's ink**: on the built example, every placed
  text's strokes in KiCad's drawing fall inside the box Kairo reasoned
  about, to within 0.1 mm (half the stroke width is 0.076 mm).
- **No room**: the parts are spread by 1.5 and then 2 times and the text
  placed again; a text that still has no clear spot is hidden and
  reported as `W_TEXT_NO_SPOT`. Text is never drawn over something.
- **Result on the example**: 22 shown texts, none overlapping, none
  nearer another part than its own, none unplaced. KiCad's rule check,
  netlist and re-save are unchanged by the new positions.
- Adding a part moves no text of the parts already there (tested), and a
  sheet with 34 parts has no two texts overlapping.
- The two power flags are now put on different parts; on neighbouring
  pins of one connector there was no room for both names.
- Measured cost: text for a 6-part sheet takes about 0.2 s, for an
  86-part sheet about 17 s. The placer prices every spot against every
  part; a grid lookup would cut that and is still to do.

### Safe writes and a sheet sized to its parts (M1.4, 2026-10-09)

- **Sessions** (`kairo.kicad.transaction`): every write goes through a
  session on the project folder. A replaced file is first copied to
  `kairo/backups` inside the project; a write that fails part way, or
  whose check finds a problem, puts every file back; a file changed on
  disk since the session last read it is never written over
  (`E_FILE_CHANGED`); `undo` restores the write before, and refuses if
  the file has been edited since. Each write reports the lines changed
  per file and their share of the file.
- **Layout**: each part is given the room its body, wire stubs, labels
  and power symbols need, columns and rows are spaced from those sizes,
  a block with many parts continues in further columns, and the paper
  grows from A4 when the design needs it. A power flag now sits part way
  along its pin's wire, not on the label at the end.
- `build_project` reads the written schematic back before asking KiCad
  about it, and takes a session so an existing project can be rebuilt.
- Not done: a project's own library tables. Whether a project table can
  stand in for a missing global library has to be measured with KiCad
  first; until then a Kairo project's rule check depends on the
  libraries installed, as any KiCad project's does.
- Text still collides in places (two power flags on neighbouring pins,
  a power symbol over a value). Moving text is the text placer's job
  (M1.10).

### Design edits, pin and pad checks, symbol boxes (M1.1 to M1.3, 2026-10-09)

- **Patch operations** (`kairo.ir.patch`): `add_net`, `add_component`,
  `connect`, `disconnect`, `set_param` and `remove`. A patch applies
  whole or not at all, the error names the operation that failed, and
  the original design is never changed.
- **Schema** (`kairo.ir.schema`): the JSON schema of a design and of the
  patch operations, the parameter names with their units, and the rules
  a schema cannot state. Registering it as an MCP resource comes with
  the design tools (M1.8).
- **Field-level errors**: a malformed design fails with the path of each
  field at fault (`components.U1`, `nets.+3V3.voltage`, `loads.0.current`).
- **Pins and pads**: grounding now checks that every pin of a symbol has
  a pad of its number on the chosen footprint (`E_PIN_PAD_MISMATCH`).
- **Seeded wrong pins**: 300-odd wrong pin names a model might write from
  memory (`VIN`, `VOUT`, `ANODE`, a regulator's pin number), tried at
  every connection of the reference design against KiCad's own
  libraries, are all refused with `E_PIN_UNKNOWN`; the design as written
  grounds with no rejection.
- **Symbol boxes** (`kairo.kicad.geometry`): the box around a symbol's
  drawing and pins, with arcs measured to where they bulge, and the same
  box as placed. Every pin of every symbol embedded in the KiCad 10
  boards ends inside its symbol's box.

### The regulator example, end to end (M1.0, 2026-10-09)

Kairo now writes a schematic. The plan's 5 V to 3.3 V regulator example
goes from its description to a KiCad 10 project, and KiCad is the judge.

- **Result.** KiCad's rule check on the written project is clean; KiCad's
  netlist holds exactly the design's 13 pin connections on 4 nets; and
  `kicad-cli sch upgrade` saves the schematic back with every byte the
  same apart from the lines naming the program that wrote it and the
  embedded-fonts line, which the command-line save leaves out. Writing
  the design twice gives the same bytes.
- **Layout rules of KiCad's writer, measured** (`kairo.kicad.emit`): they
  reproduce all 223 installed symbol libraries byte for byte and 114 of
  the 149 real sheets; the other 35 hold embedded images or long bus
  alias lists, which Kairo does not write.
- **Pin positions, measured** (`kairo.kicad.geometry`): over 22,491 pins
  on the real boards, in the eight placements KiCad writes, a pin is
  turned counter-clockwise and then mirrored. The other orders fit
  fewer pins (254 of 269 against 163 at 90 degrees mirrored).
- **Embedded symbols, measured** (`kairo.library.embed`): a derived
  symbol is stored flattened under its own name. 75 of 89 symbols the
  KiCad 10 program embedded in the real boards are reproduced token for
  token; the rest were edited or come from older libraries.
- **Design description** (`kairo.ir`): parts, nets with roles,
  connections by pin name, loads, four requirement types, blocks and
  placement hints. Unknown fields, parameters and requirement types are
  errors. A quantity needs its unit: `300` for a current is refused.
- **Grounding** (`kairo.library.grounding`): every symbol, footprint and
  pin is checked against the libraries before anything is written, and
  every problem is reported, not only the first. The plan's example
  wrote `J1.1`; the library names that pin `Pin_1`, so the reference
  design says `J1.Pin_1`.
- **`ldo_thermal`** (`kairo.calc`): dissipation, junction temperature,
  efficiency and dropout headroom. With no quiescent current given it
  returns proven one-sided bounds (at least 0.512 W and 86.1 degC for the
  example) and never puts a number in its place. Every result carries an
  accuracy block, which is `unverified` until worst cases are computed.
- **`build_project`** (`kairo.build`): ground, draw, write, then KiCad's
  netlist and rule check. A project that fails a check is not left on
  disk, and an existing project is never written over.
- The extension's launcher now runs `python -m kairo`. Run as `kairo`,
  the server kept `kairo.exe` open, and `uv` could not rebuild the
  environment while Claude Desktop was running.
- Not done here, by design: readable placement (the first sheet is
  correct but crowded), rotated or mirrored parts, writing for KiCad 9,
  editing an existing project, and worst cases over tolerances.

### A Claude Desktop extension for this machine (M0.5, 2026-10-09)

- `packaging/mcpb/build.py` writes `dist/kairo.mcpb`, an extension file
  Claude Desktop can install so Kairo appears under Settings, Extensions.
  It holds a manifest and a launcher that runs Kairo from the checkout
  with `uv`, and asks for the projects folder when installed. Windows
  only. The launcher was checked by driving it with the MCP client; the
  install itself is the owner's to try.
- The owner installed it on 2026-10-09: Claude Desktop lists it, enabled,
  and starts the server. The extension now carries an icon (a zener
  diode) and names JoshChomps as its author; because the author is part
  of the extension's identity, the next install is a new entry and the
  old one should be removed.

### Four more real boards (2026-10-08)

- Added with the owner's agreement, schematic-side files only: Clavier
  (CERN-OHL-P-2.0), VESC6 Open Hardware (MIT), pedalboard-hw
  (CERN-OHL-P-2.0, saved by KiCad 10) and RTB C13 (Apache-2.0). 30 more
  sheets saved by the KiCad 9 and 10 programs; all round-trip byte for
  byte, and kicad-cli's netlist and ERC for each are read. Two of them
  have real ERC errors (13 and 7), which the earlier boards mostly lack.
- The owner ran ERC in the KiCad 10.0.6 window on Antmicro's OSM-L board
  and saw both `label_dangling` errors that kicad-cli reports.

### M0.3 closed by the owner (2026-10-08)

- Closed with two parts of its acceptance not met, both recorded in
  the plan: the four KiCad 8 boards re-saved from the KiCad 10 program,
  and a saved GUI ERC report for each project (carried to M1.5).
- One GUI observation: on Antmicro's OSM-L board the KiCad 10.0.6 window
  shows "Label not connected" for `JTAG_{OUT}.TDO`, as kicad-cli does.

### What the tools cost a client, measured and cut (M0.5, 2026-10-08)

Every tool was called through the MCP client on nine real boards (68 to
805 parts), with every page of every list, and `get_symbol_pins` on all
22,860 installed symbols. Sizes are characters of the text a client is
sent; a token is roughly three to four of them.

- Fixed: `read_schematic` used one page number for parts and nets, and
  refused a page past the end of the shorter list. On Antmicro's OSM-L
  board (384 parts, 1062 nets) that left 700 nets out of reach. It now
  returns one list per call, chosen with `what`, and always both totals.
- Fixed: `run_erc` started KiCad again for every page (3 to 8 s each; a
  board with 1413 warnings has 71 pages). A call with page 1 and no type
  runs KiCad; other calls read that run unless a `.kicad_sch`,
  `.kicad_pro` or `.kicad_sym` file in the project has changed.
- `run_erc` lists errors before warnings, and takes `violation_type` to
  show one type only, so a caller need not page through hundreds of
  library warnings to reach two design errors.
- Tool text is now compact JSON. The MCP library's default is indented,
  which cost about two fifths more characters for the same content.
- Largest response: 30,439 characters before, 20,730 after (the pins of a
  100-pin STM32H7). A page of 100 parts is at most 17,400, a page of 100
  nets about 4,600, a page of 20 ERC violations about 5,000 (was 6,400).
  A new integration test holds every response on three real boards and on
  the STM32H7 library under 25,000 characters.
- Found: KiCad 10.0.6's ERC is not repeatable on Antmicro's PolarFire SoM.
  Six runs of kicad-cli on untouched copies gave 15, 16, 17, 15, 16 and
  16 `multiple_net_names` warnings. `run_erc` now says so in its notes
  when a run's counts differ from the previous run's and no project file
  changed between them.
- Checked and unchanged: a wrong call costs 26 to 295 characters, names
  what to do instead, and gives the same answer when repeated; the same
  call on unchanged files gives the same text for every tool but ERC on
  that one board; 150 calls on nine boards left every project folder byte
  for byte as it was.

### ERC lengths double-checked (2026-10-08)

- The hundredfold fault in kicad-cli's ERC JSON report was checked more
  widely: 61 reported positions on four real boards are each exactly one
  hundredth of the schematic's; lengths inside descriptions are affected
  too; `--units in` and `--units mils` are off by the same factor; the
  text report of the same run is right. It is KiCad issue 25582, listed
  as fixed in KiCad 10.0.7 (released 2026-10-07).
- `run_erc` now adds a note saying so whenever the report comes from a
  KiCad older than 10.0.7, because the descriptions it passes on quote
  those lengths.

### Boards saved by KiCad 9 and 10, and what they showed (M0.3, 2026-10-08)

- Eight more open-hardware projects are in `tests/fixtures/real/`: six from
  Antmicro (Apache-2.0), Great Scott Gadgets' HackRF Pro and Sanctuary
  Systems' rpi-cm5io-mini-itx (both CERN-OHL-P-2.0). 119 schematic sheets,
  115 saved by the KiCad 9 program and 4 by the KiCad 10 program, plus
  their project files and own symbol libraries. Schematic-side files only.
- All of them round-trip byte for byte and pass the format check, in CI as
  well. With KiCad present, their netlists (up to 805 parts and 1062 nets)
  and ERC reports are read with no malformed result.
- Schematic format 20260306 is now known to be what the KiCad 10 program
  itself saves, not only `kicad-cli sch upgrade`.
- Fixed: an ERC report was classed `malformed` when it held a violation
  with an empty severity. KiCad 10.0.6 writes that for a violation of a
  check the project has set to ignore, when a person has also excluded it
  (found on Antmicro's PolarFire SoM). Such a violation is now read as
  `ignored` and never counted; an empty severity on a check that is on is
  still malformed.
- Fixed: ERC item positions were named `x_mm` and `y_mm`, but kicad-cli
  10.0.6 writes one hundredth of the schematic's position while calling
  the units mm (measured: a label at 189.23 mm is reported at 1.8923).
  They are now `x` and `y`, documented as KiCad's values as written, and
  Kairo does not convert them.
- Three ERC reports from real boards are fixtures, so excluded violations
  are tested against what KiCad writes (they keep their severity and gain
  `"excluded": true`).

### Server skeleton (M0.5, awaiting a try in Claude Code and Claude Desktop, 2026-10-08)

- `kairo` now starts an MCP server over stdio (the SDK's `MCPServer`, which
  is what FastMCP became in mcp 2.x). Options: `--allowed-root`,
  `--kicad-cli`, `--cache-dir`.
- Nine read-tier tools: `list_projects`, `project_summary`,
  `read_schematic`, `trace_net`, `find_symbol`, `get_symbol_pins`,
  `find_footprint`, `run_erc`, `render_schematic`. Results are paged, a
  failure starts with a stable `E_` code, and paths outside the allowed
  roots are refused. A test fails if any tool's description lacks what it
  does, what it needs, its limits, what it is not for, or its tier.
- `kairo.kicad.netlist` reads kicad-cli's netlist (parts, nets, pins with
  their types), so connectivity always comes from KiCad itself.
- `KicadCli.export_svg` draws every sheet; drawings go to the cache folder,
  never into the project.
- kicad-cli 10.0.6 writes a `.kicad_prl` file into a project that has none
  (measured for ERC, netlist and SVG export). Kairo now removes one that
  appeared during a run and says so in the result.
- ERC results count findings that only say a library is missing on this
  machine apart from findings about the design; neither is waived.
- Measured over stdio with the SDK's client on the mikoto board: every
  tool answers; the first library search took 28 s (indexing 15,673
  files), later ones under 0.1 s; a project summary with ERC 2.7 s.
- The MCP SDK and the 27 packages it brings in are recorded in NOTICE; all
  are MIT, BSD, Apache-2.0 or PSF licensed (pywin32 bundles an LGPL
  module Kairo does not import).

### Real-board fixtures (M0.3, still in progress, 2026-10-08)

- `tests/fixtures/real/` holds the four open-hardware boards CopperBench
  vendors (Antmicro's Jetson Orin baseboard and microphone board, the Open
  Lighting Project's DMXsun baseboard, zhiayang's mikoto), unmodified, with
  their licences, upstream commits and CopperBench's recorded ERC reports.
  The 35 MB routed Jetson board is left out.
- Every KiCad file in them (13 schematic sheets, 3 boards, 35 footprints,
  3 symbol libraries, tables, rules, project files) round-trips byte for
  byte, in CI as well. They are KiCad 8 files, so the format check refuses
  them by name. With KiCad present, each sheet is re-saved by
  `kicad-cli sch upgrade --force` in a temporary copy and that copy
  round-trips too and runs ERC with no errors.
- The tree hash Kairo computes for mikoto equals the one CopperBench
  publishes for it, so the two hashing schemes agree and the copy is the
  one CopperBench tested.

### Library index (M0.4, 2026-10-08)

- `kairo.library.reader` reads symbols (pins with unit, position, type and
  alternates; the fields that identify a part) and footprints (pads) out of
  parsed library files. A derived symbol takes its pins from the end of
  its `extends` chain. Anything laid out differently from the surveyed
  KiCad 10.0.6 libraries is refused, naming the symbol.
- `kairo.library.tables` reads `sym-lib-table` and `fp-lib-table`,
  following KiCad 10's `Table` entries and expanding `${VAR}` and `$(VAR)`.
  An entry it cannot use is returned as a problem, never dropped.
- `kairo.library.LibraryIndex` stores it all in SQLite. An update re-reads
  only files whose size or time changed, removes files that are gone, and
  lists every file it could not read. Lookup by `Library:Name` returns the
  exact pins or pads; search matches words in name, library, description
  and keywords.
- Measured on this machine's KiCad 10.0.6 through its own library tables:
  223 symbol libraries and 155 footprint libraries, 22,860 symbols with
  541,758 pins and 15,450 footprints with 386,597 pads, indexed with no
  problem in 30 s on 14 worker processes (146 s of parsing on one); a
  second update takes 0.2 s; a symbol lookup about 1.5 ms. The database is
  117 MB.

### Fixes from the first code review (2026-10-08)

- CI never ran: the workflow named `astral-sh/setup-uv@v10`, a tag that
  does not exist. It now names `v10.2.0`.
- `kairo.kicad.sexpr` prints with an explicit stack, so a tree nested
  deeper than Python's recursion limit prints back instead of raising
  `RecursionError` after it parsed.
- `kairo.kicad.cli`: a registry uninstall entry with no install location
  no longer hides the KiCad entries after it; what kicad-cli wrote is
  decoded from bytes, so a netlist keeps its line endings, and output that
  is not UTF-8 gives `unavailable` instead of an exception; lock clean-up
  no longer fails on Python 3.12 when the user cannot be named.
- `kairo.text_placement`: a label's price no longer depends on the order
  the labels come in. The scan used to stop at the first overlapping
  label, so the too-close term covered only the labels before it. An
  overlapping label now always pays the overlap and the full too-close
  weight, as a label overlapping a part already did. Benchmark rerun with
  the recorded seed (`2026-10-08-fresh20261006`, beside the record of
  2026-10-06): every summary table is unchanged; of 300 runs, one annealing
  run moved (random_packed seed 3, 9.586 to 9.664 points) and 14 runs
  used a different number of evaluations.
- The benchmark's solve time no longer includes its own bookkeeping.

### Lossless KiCad files (M0.3, in progress, 2026-10-06)

- `kairo.kicad.sexpr` parses KiCad S-expression files into a tree that
  keeps every token and the whitespace before it, so an untouched file
  prints back byte for byte and an edit changes only what it replaces.
  Tested on all 16,291 S-expression files in a KiCad 10.0.6 install
  (schematics, symbol and footprint libraries, boards, worksheets, rules,
  library tables): every one round-trips exactly except one demo footprint
  whose parentheses do not balance, which is refused.
- `kairo.kicad.formats` refuses file-format versions Kairo has not seen
  written by a released KiCad 9 or 10, naming the version and the KiCad
  that wrote it.
- `kairo.kicad.project` reads and writes `.kicad_pro` files, refusing any
  that would not print back exactly.
- `kairo.hashing.tree_sha256` hashes fixture trees; a test fails if any
  fixture tree changes without its recorded hash.
- Hypothesis property tests: any text either round-trips exactly or is
  refused, printed trees parse back to the same text, and string escaping
  is reversible.

### kicad-cli runner (M0.2, 2026-10-06)

- `kairo.process.run_process` runs an external program with a timeout and
  kills its whole process tree on timeout, and sweeps it after a normal
  exit too: a Windows Job Object (the program starts suspended, so no child
  can escape) or a POSIX process group.
- `kairo.kicad.cli` finds kicad-cli (`KAIRO_KICAD_CLI`, `PATH`, then the
  Windows registry), reads its version, runs ERC and exports netlists.
  Every flag was checked against `kicad-cli --help` on 10.0.6.
- ERC results are `clean`, `findings`, `malformed` or `unavailable`. The
  report is checked field by field against KiCad's erc.v1 schema and
  against kicad-cli's exit code (0 clean, 5 violations, 3 load failure,
  measured on 10.0.6). A missing KiCad is `unavailable`, never `clean`.
- kicad-cli holds a lock file in the project folder while it runs and
  leaves it behind when killed (measured). After a timeout Kairo removes
  that lock, but only one that appeared during the run and names this
  machine and user.
- Tests: real ERC reports from KiCad's demo projects as fixtures, a
  stand-in kicad-cli for the failure modes, and tests against the
  installed KiCad that copy its demos into a temporary folder.

### Text placer core (2026-10-06)

- `kairo.text_placement`: places references, values and labels so they can
  be read. Candidate spots around each part, one cost function shared by
  search and judge, and five strategies (greedy, descent with ejection
  chains, annealing that keeps its best layout, negotiated congestion,
  and a deterministic portfolio), all bounded by a work budget. Pinned
  labels stay put; a warm start keeps a layout stable after an edit.
- The judge prices a label that reads as another part's above leaving it
  out, so solvers never keep a misleading label to save points.
- `evals/text_placement/run.py`: a benchmark over five synthetic board
  families, with seen and fresh boards scored separately, a search-weights
  experiment and an edit-stability experiment. Results and method are in
  `docs/text-placement.md`.
- Strict type checking now covers `evals/` too.

### Project set-up (M0.1, 2026-10-06)

- A uv project with the package under `src/kairo` and a `kairo` command
  that answers `--help` and `--version`. With no arguments it says the
  server is not written yet and exits with code 1.
- Ruff for linting and formatting, including Google-style docstring checks;
  pyright in strict mode; pytest, with `kicad` and `spice` markers for tests
  that need those programs.
- A documentation site built with Sphinx, MyST and Furo: getting started,
  an API reference generated from docstrings, a guide to writing code and
  docs, the assumptions log and this changelog.
- GitHub Actions runs lint, types, tests, the docs build and the command on
  Ubuntu and Windows with Python 3.12 and 3.13.
- NOTICE lists the licence of every locked package, the external programs
  Kairo runs, and borrowed code (none so far). A test fails when `uv.lock`
  and NOTICE disagree.
- No LICENSE file and no licence field yet; a `Private :: Do Not Upload`
  classifier stops an accidental PyPI upload.
- The reference projects' licences were checked at pinned commits and
  recorded in the plan.
