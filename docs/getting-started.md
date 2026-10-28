# Getting started

## What you need

- [uv](https://docs.astral.sh/uv/getting-started/installation/), which
  installs the right Python (3.12 or newer) and every package Kairo uses.
- [KiCad](https://www.kicad.org/download/) 10, for the schematic checks
  and boards (9 reads projects too).
- For simulation, [ngspice](https://ngspice.sourceforge.io/) (47 or newer)
  and, for a second opinion, [Xyce](https://xyce.sandia.gov/) (7.10).

Kairo runs on Windows (x64), macOS (Apple silicon and Intel) and Linux
(x86_64 and arm64).

## Where Kairo finds KiCad

Kairo looks for `kicad-cli` in this order and uses the first it finds:

1. the `KAIRO_KICAD_CLI` environment variable, if set (the full path to the
   executable; if that path does not exist, Kairo stops there rather than
   trying another KiCad);
2. `kicad-cli` on `PATH`;
3. on Windows, the install folder KiCad's installer recorded in the
   registry, newest version first.

A standard Windows install needs no setting. If KiCad cannot be found,
every check that needs it reports `unavailable` and says where Kairo
looked; it never reports a pass.

## Install

Each release on the project's GitHub Releases page carries:

- `kairo.mcpb`, the Claude Desktop extension, for every platform;
- a wheel for each platform, `kairo_eda-<version>-<platform>.whl`, which
  holds Kairo with its compiled loops, for Claude Code or any MCP client.

### Claude Desktop

Install uv first. Then, in Claude Desktop, open Settings, Extensions,
Advanced settings, choose Install Extension and pick `kairo.mcpb`. Its
settings ask for the folder your KiCad projects are under and the tier:
`read` (the default) only looks, `propose` also checks and simulates,
`write` also builds and changes projects. The extension runs Kairo with
uv from the wheel it carries for your computer.

### Claude Code, or another MCP client

Install the wheel for your platform as a tool, then add the server:

```bash
uv tool install ./kairo_eda-<version>-<platform>.whl
claude mcp add kairo -s user -- kairo --allowed-root /path/to/kicad/projects --tier write
```

`-s user` makes it available in every folder; leave it out to add it for
the current folder only. `claude mcp list` shows whether it connected, and
inside a session `/mcp` shows its tools. Another client runs the same
command, `kairo` with its options; for Claude Desktop without the
extension, add it to `claude_desktop_config.json` under `mcpServers`:

```json
"kairo": {
  "command": "kairo",
  "args": ["--allowed-root", "/path/to/kicad/projects", "--tier", "write"]
}
```

Keep projects outside KiCad's own install folder: installing or removing
KiCad replaces that folder.

### From source

Working on Kairo itself needs [Rust](https://rustup.rs/) too, which builds
its compiled loops. From a clone of the repository:

```bash
uv sync
uv run kairo --help
claude mcp add kairo -- uv run --directory /path/to/Kairo kairo --allowed-root /path/to/kicad/projects
```

To build the extension from a checkout: `uv run python
packaging/mcpb/build.py --wheels <folder of wheels>` makes one that carries
those wheels; with no `--wheels` it makes one that runs this checkout, on
this computer only.

## Run

```bash
kairo --help
kairo --version
```

`kairo` with no arguments starts the MCP server on standard input and
output, which is how an MCP client runs it. It then waits for a client and
prints nothing, so it is not something to run by hand.

| Option | What it sets |
|---|---|
| `--allowed-root DIR` | A folder the tools may read KiCad projects under. May be given more than once. Without it, only the folder the server was started in. |
| `--tier read\|propose\|write` | Which tools are offered (see below); `read` unless set. |
| `--kicad-cli PATH` | The kicad-cli to use, instead of the search above. |
| `--ngspice PATH`, `--xyce PATH` | The simulators to use, if they are not on `PATH`. |
| `--calls-per-ten-minutes N` | The most tool calls accepted in any ten minutes, 150 unless set. A model that passes it is told to stop and report to you. 0 turns the limit off, for a model that costs nothing to run. |
| `--parts-catalogue FILE` | A parts catalogue built by `kairo.parts.catalogue`. Without it, `parts-catalogue.sqlite3` in the cache folder if there is one. |
| `--cache-dir DIR` | Where the library index and drawings are kept. Without it, the `kairo` folder in `%LOCALAPPDATA%` on Windows, and `$XDG_CACHE_HOME/kairo` or `~/.cache/kairo` elsewhere. |

## Starting a design: say "Kairo"

Begin your message with **Kairo** or **kairo go** and say what you
want:

> Kairo, a 3.3 V rail from 5 V USB for a sensor that draws 120 mA

The model then asks Kairo for its order of work (the `go` tool) and
follows it: ask you for anything the request leaves out and never
choose a value itself, take pins from the library and numbers from
the calculations, check the design before writing it, open it in
KiCad, and report every result that is not established. The steps
are Kairo's own and the same whichever model runs them; they name
only the tools the server's tier offers.

The same steps are a prompt named `go`, for clients that list a
server's prompts: in Claude Code type `/mcp__kairo__go` and what
you want; in Claude Desktop pick it from the menu beside the
message box.

The keyword is read by the model, from the instructions the
server gives every client, so a client that does not pass those
on will not know it; the prompt works there.

## What the server can do so far

Tools come in three tiers, and a server offers its tier and those below
it. Start it with `--tier read` (the default), `--tier propose` or
`--tier write`.

**Read**: look at projects and libraries. Nothing is changed.

| Tool | What it gives |
|---|---|
| `list_projects` | The KiCad projects under the allowed roots |
| `project_summary` | Sheets, part and net counts, power nets and the ERC status |
| `read_schematic` | Parts or nets (one list per call), from KiCad's own netlist |
| `trace_net` | Every pin on one net, with its type |
| `find_symbol`, `find_footprint` | A search of the installed libraries |
| `get_symbol_pins` | A symbol's exact pin names, numbers and types |
| `run_erc` | KiCad's rule check, classified, errors first; only `clean` is a pass |
| `render_schematic` | An SVG of every sheet, for judging readability |
| `open_in_kicad` | Opens the project in the KiCad window, unless it is open already. KiCad 10 cannot be told to reload, so after a later change you revert or reopen the sheet |
| `design_schema` | The exact shape of a design and of patch operations |
| `read_datasheet` | The limit tables of a datasheet PDF, with minimum, typical and maximum kept apart |
| `save_part` | Keeps a part's datasheet figures under its part number, for every later design that names it |
| `find_part` | Real resistors, capacitors, inductors and LEDs of one value, with part numbers, tolerances, ratings and stock; and regulators by output voltage |
| `go` | Kairo's order of work for a design or a check, for the server's tier |
| `server_info` | Which build is serving: version, commit, folder, whether its code changed on disk since it started (restart the client if so), whether the Claude Desktop extension lists this build's tools, and where kicad-cli, ngspice and Xyce were found |
| `recent_calls` | This server's latest tool calls with their outcomes and refusal codes, and each tool's counts; the full log is `calls.jsonl` in the cache folder |
| `accuracy_report` | Every tolerance with its source, and what to supply for each unverified result |

**Propose**: check and calculate. Nothing is written.

| Tool | What it gives |
|---|---|
| `calc` | One calculation with a cited formula, its worst case and accuracy block |
| `analyze_power_tree` | Each rail's current, each regulator's load, each requirement's result |
| `plan_design` | A full check of a design: pins, structure, drawing, requirements |
| `verify_design` | KiCad's netlist and rule check of a project this server built |
| `check_schematic` | A schematic a person drew, read from KiCad's netlist and checked against the facts you state: each rail's current and each requirement's result |
| `simulate_schematic` | Some parts of a schematic a person drew, run the same way: you name the parts, and state what a model needs |
| `simulate` | A design run in ngspice and Xyce: the steady state, the response to a step, or a frequency sweep, with graphs |

**Write**: build and change projects.

| Tool | What it does |
|---|---|
| `apply_design` | Builds a design into a project folder and checks it with KiCad |
| `edit_design` | Changes a built project by patch operations and checks it again |
| `set_value` | Changes one part's value in a schematic a person drew, touching nothing else, and has KiCad confirm it |
| `edit_schematic` | Adds a part to, removes a part from, or moves a pin in a schematic a person drew, moving nothing that is there, and has KiCad confirm it |
| `undo` | Puts a project back as it was before the last write |

A write that fails any check leaves the project as it was. A design whose
requirement cannot be met is refused with the arithmetic. A project gets
12 write attempts per server run, and after 3 failed attempts in a row
that do not improve, further attempts are refused until the server is
restarted: the model is told to stop and report to you.

Every built project holds `kairo/accuracy-report.md`, which lists what is
and is not established and what to supply; and `kairo/backups`, the
versions each write replaced.

### If a model gets stuck

A model on a paid service is charged for every call, so Kairo ends a
loop itself, four ways:

- a project gets 12 write attempts per server run, and after 3 failed
  attempts in a row that do not improve, writing is refused;
- any call that has given the same answer three times running is
  refused the fourth time;
- a tool that has failed with the same error five times running is
  refused until some other call has succeeded;
- more than 150 calls in ten minutes are refused until the ten
  minutes pass.

Each refusal tells the model to stop and tell you what it was doing.
Checking a schematic again after you have edited it is not a loop: a
changed answer starts the count afresh.

### Choosing real parts

`find_part` searches a catalogue of real parts kept on your computer:

> find_part: a 10k resistor in 0603, 1% or better

It answers with a short list: part number, LCSC number, tolerance,
ratings and stock, with JLCPCB's basic and preferred parts first.

Give a part its number in a design and you need not restate its
figures:

```json
"R1": {"lib_id": "Device:R", "value": "10k",
       "params": {"mpn": "AT0603FRD0710KL"}}
```

Kairo fills in the tolerance, ratings and the like from that part's
listing, wherever the design states none, and says in the result
which figures came from the catalogue. A part number that is another
part than the one drawn (a 10k resistor with the number of a 12k one)
is reported and gives nothing.

It finds regulators too, by what they must do:

> find_part: a buck regulator for 5V out from 12V in, at least 1.5A

A regulator's listed figures are for choosing one. They are never
filled into a design, because a listing does not say which figure
is typical and which is a limit; those come from the datasheet (see
below).

The catalogue is a distributor's listing, not the datasheet, and it
holds resistors, capacitors, inductors, LEDs and regulators, and no
other chips. To get it, once (about 160 MB to download; Kairo keeps
77 MB of it):

```bash
uv run python -m kairo.parts.catalogue fetch FOLDER
```

```bash
uv run python -m kairo.parts.catalogue build FOLDER/cache.sqlite3 %LOCALAPPDATA%/kairo/parts-catalogue.sqlite3
```

Kairo finds it there by itself; `--parts-catalogue FILE` names another.
Run the two commands again for a newer snapshot.

### A chip's limits from its datasheet

The catalogue helps choose a regulator and gives none of its limits.
For a regulator or any other IC, put
its datasheet PDF under a folder Kairo may read (or in the
`datasheets` folder of Kairo's cache) and ask:

> read_datasheet tps54202.pdf, search "feedback voltage"

Each row comes back with what is printed under MIN, TYP and MAX in
its own column, the unit and the page. A figure under TYP is not a
limit; only MIN and MAX are. The model then writes the limit into
the design, marks it under `limits`, and cites the page as its
source. Check a row against the page: a parameter's name is read
roughly, though its figures and columns are read well.

This needs one extra package: `uv sync --extra datasheets`, or
`pip install kairo[datasheets]`.

To read a chip's datasheet only once, have its figures saved:

> save_part TPS54202, Texas Instruments, datasheet SLVSD26C revision C:
> i_limit 2.5A (page 5, a minimum), f_sw 390kHz (page 5, a minimum)

Any design whose part gives `"mpn": "TPS54202"` then takes those
figures where it states none, and so does a schematic you drew
whose part has that number in an MPN field. A saved figure is
marked unreviewed, and results say so, until you check it against
the page and change `"review": "unreviewed"` to `"reviewed"` in
the part's file (the tool's answer gives the file). The tool never
changes a figure you have marked reviewed.

### Part numbers in a schematic you drew

`check_schematic` reads a part's number from its fields: a maker's
number from a field named `MPN`, `Part_number`, `Manufacturer Part`
or the like, or an LCSC number from a field with `LCSC` in its
name. The part then takes its saved figures, or the catalogue's,
with nothing restated. A number you give in the facts comes first.

### Tolerances you have not stated

Where a design gives no tolerance, Kairo uses a wide one of its own:
5% for a resistor, 20% for a capacitor, 30% for an inductor, 10% for
a supply from outside, 20% for a diode's forward voltage, 5% for a
regulator's output. A tolerance you state, or one from a datasheet,
always takes its place, and so does the figure listed for a part
whose number the design gives (see above).

Every result says what it rested on: `basis` is `default margins` or
`stated values`, and each default used is listed. A pass on defaults
holds for any ordinary part; a fail on defaults may pass once you
give the real figure, and the result says which to give.

Only tolerances are filled in. A fact about one part, such as a
regulator's dropout or a capacitor's ESR, is never guessed, and a
result that needs it stays `unverified`.

To change a default, or to turn them off for the last check before a
board is ordered:

```json
"policy": {"default_margins": {"resistor": "1%"}}
"policy": {"margins": "stated"}
```

### A project's own policies

A regulator's datasheet gives its thermal resistance, theta_JA, as a
typical value measured on a standard test board, not as a limit, so
Kairo cannot prove a junction temperature from it and reports the check
as `unverified`. If your project is content to reckon with the
datasheet's value plus a margin, say so in the design:

```json
"policy": {"theta_ja_margin": "20%"}
```

The check can then pass or fail, every result that used the policy says
so, and the accuracy report lists it. Kairo never chooses the margin.

### Checking a schematic you drew

`check_schematic` takes a project and, optionally, facts:

```json
{"nets": {"GND": {"role": "ground"},
          "+3V3": {"role": "power", "voltage": "3.3V"}},
 "components": {"U2": {"params": {"regulator": "linear", "i_q": "5mA"},
                       "source": "datasheet rev C, page 5"}},
 "loads": [{"net": "+3V3", "current": "120mA"}]}
```

Called with no facts it says what it read and lists the nets that have
supply pins and no stated role. Kairo does not take a net called `GND`
to be ground, or guess a rail's voltage: state them, and call it again.
Each part on a rail whose current is not known is named with what to
supply, and the rail's total is then a lower bound.

### Changing a schematic you drew

`set_value` changes one value. `edit_schematic` takes a list of
operations and makes them together or not at all:

```json
[{"op": "add_part", "ref": "R40", "lib_id": "Device:R", "value": "4k7",
  "footprint": "Resistor_SMD:R_0603_1608Metric",
  "pins": {"1": "+3V3", "2": "/MIDI/I2C_SDA"}, "beside": "R19"},
 {"op": "remove_part", "ref": "R19"},
 {"op": "reconnect", "pin": "R18.2", "net": "/MIDI/I2C_SCL"}]
```

A new part goes in free space near the part named in `beside`, with
a short wire and a label or power symbol on each pin; nothing you
placed is moved. A removed part takes with it only the wires and
labels that then lead nowhere. Nets are named as `read_schematic`
names them.

A part of several units, such as a dual op amp, is added whole: each
unit goes in free space near the one before, and every pin of every
unit needs a net or a place under `no_connect`. A pin name found in
two units (an op amp's `+`) is refused; give the pin's number, or
write `+*` to mean both. On a crowded sheet the units may land far
apart.

KiCad then checks the result: the connections must be exactly those
asked for, and its rule check must have nothing it did not have
before. If not, your files are put back and the result says why.
Removing a part that leaves another part's pin joined to nothing is
refused this way; give the pin somewhere to go in the same call.

This works on schematics saved by KiCad 10. An older file is refused
with a message to open and save it in KiCad 10 first. KiCad does not
reload a schematic that changed on disk: revert or reopen the sheet
to see the change.

### Kairo's review

`analyze_power_tree` and `check_schematic` also return a review, by
rules of Kairo's own, in two kinds:

- a **fault** does not work as drawn, whoever drew it: an input alone
  on its net, a rail that feeds chips with no capacitor on it;
- a **suggestion** is where the design may well be sound and Kairo
  would do it another way, such as one decoupling capacitor shared by
  several supply pins, or an I2C line with no pull-up drawn.

Neither blocks anything. The rules read connections, not the drawing, so
a schematic laid out your own way is not marked down for it, and a part
drawn with a symbol Kairo does not recognise is given the benefit of
the doubt.

### Simulating a design

`simulate` answers what a steady-state check cannot: how a rail comes up,
what plugging in a live supply does to the input, how long a filter takes
to settle. It takes a design and a request:

```json
{"analysis": {"kind": "tran", "duration": "400us"},
 "stimuli": [{"net": "VIN_5V", "kind": "step", "value": "5V", "rise": "1us",
              "resistance": "50mohm", "inductance": "1uH"}],
 "limits": [{"metric": "peak", "net": "VIN_5V", "max": "6V"}]}
```

- `kind` is `op` (every net's voltage and every part's current and
  dissipation at rest), `dc` (the same as one supply is swept from
  `v_start` to `v_stop`), `tran` (peak, overshoot, 10% to 90% rise time, settling time and peak
  source current for each net) or `ac` (gain and cutoff frequency).
- A step must state its rise time, and may state the source's resistance
  and inductance. Kairo does not choose these: they decide the overshoot.
- A limit is judged at the worst case over the tolerances the design
  states, and passes only there. A value the result depends on that has
  no tolerance leaves the limit `unverified` and is named.
- Where a number does not move one way with a value, its worst case
  lies between that value's ends. Kairo searches there: a point it
  finds that breaks the limit is a failure, and finding none leaves
  the limit `unverified`, since a search proves nothing.
- `"temperatures": ["-40degC", "85degC"]` in the analysis has each
  limit judged at those corners as well as at 27 degC. Each part must
  state how its value moves with temperature (`tempco`, such as
  `100ppm/degC`, or a ceramic capacitor's `dielectric`); one that does
  not leaves the limit `unverified` and is named.
- `"monte_carlo": {"samples": 200, "seed": 1, "distributions":
  {"R1": {"shape": "uniform"}}}` adds a sampled spread of every
  number. Only values whose distribution you state are sampled, and
  the spread is a report: it never decides a limit.
- `"loop": {"pin": "U1.ADJ", "into": "pin"}` on an `ac` analysis
  measures the feedback loop through that pin: its crossover, phase
  margin and gain margin. It is `checked` only with a maker's model
  of the part, because nothing else represents the loop.
- Each number is `checked` or `unverified` with the reason. A run in time
  through a regulator or diode model is always `unverified`, because
  those models are built from a few datasheet values and do not represent
  the part's dynamics.
- `practice` compares the response with published design practice: ringing
  on a hot-plugged ceramic input capacitor (Analog Devices AN88), inrush
  current (TI SLYT332), how ringy a response is in terms of phase margin
  (TI SLVA381B), a capacitor's rating exceeded at the peak, a rail above
  its required band on the way up.
- Graphs (`voltages.svg`, `currents.svg` or `gain.svg`), the curves as
  CSV and the netlist are saved in Kairo's cache folder, and the result
  says where. Nothing is written into the project.

`simulate_schematic` does the same for part of a schematic you drew.
A whole board cannot be simulated, because most chips have no model,
so you name the parts that belong together (a regulator and its
capacitors, a filter) and state in `facts` the ground net and, as
loads, whatever the rest of the board draws from their nets. The
result says which parts and nets were in the circuit, and holds for
that circuit only.

`--models-dir DIR` names a folder of simulation models from parts'
makers. Put a model file there, write its facts as JSON (the part number
or symbol, the subcircuit, which pin each node goes to, the maker, the
source and version, the dialect and the simulators it runs on), and
record it with `python -m kairo.spice.registry DIR FACTS.json`. Kairo
then uses it for that part, names it and its hash in every result, and
refuses it if the file changes. `evals/simulation/models` has an example
for TI's LM1117-3.3 model.

ngspice must be installed, and Xyce too for a second opinion. Kairo looks
for them in `KAIRO_NGSPICE` and `KAIRO_XYCE`, then on `PATH`; or start the
server with `--ngspice PATH` and `--xyce PATH`. With one simulator the
result says no second one checked it and is `unverified`.

`--parts-dir DIR` names a folder of datasheet facts, one JSON file per
part number (see the reference for `kairo.parts`), which is where a
capacitor's DC-bias curve comes from.

The first library search in a session indexes every installed library,
which takes about half a minute; after that the index is only refreshed
for files that changed. A tool that cannot answer refuses with a stable
code such as `E_KICAD_UNAVAILABLE`, what went wrong, a `fix` saying what
to do next, and whether the same call may work if made again
(`retryable`); it never returns a partial answer that could be read as a
pass. While a call runs, a client that asks for progress hears from it
every few seconds.
