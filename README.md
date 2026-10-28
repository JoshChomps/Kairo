# Kairo

An MCP server that turns a described circuit into a checked KiCad project.
Every voltage and current is worked out twice, by formulas with cited
sources and by simulation (ngspice and Xyce), and anything that could not
be checked is marked unverified instead of guessed.

Kairo gives an AI model, such as Claude, the tools to design and check
electronics without making numbers up:

- **Designs from a description.** Pins come only from the KiCad libraries,
  never from memory; each build is checked by KiCad's own netlist and
  electrical rules check before anything is written.
- **Numbers with sources.** Every value comes from a formula with a cited
  source and its worst case over the parts' tolerances; each requirement is
  pass, fail or unverified, with what would settle it.
- **Simulation in two engines.** ngspice and Xyce, judged at the worst case;
  Monte Carlo is a report and never decides.
- **Schematics you drew.** Read any KiCad project, check it against the
  facts you state, simulate parts of it, and change values or parts with
  KiCad confirming nothing else moved.
- **Real parts.** Resistors, capacitors, inductors, LEDs and regulators
  from a parts catalogue; datasheets fetched and their limit tables read.
- **Boards.** Placement by block and edge, pours, Kairo's own router, and
  KiCad's DRC against net classes and voltage spacing, with fab files.
- **A review.** Faults and suggestions, each with its source: rails with no
  decoupling, switches bypassed by a body diode, floating inputs, crossed
  UARTs, and on a prototype, rails with no test point, light or link to
  open, and spare I/O left unreachable.

## Install

Releases carry a Claude Desktop extension (`kairo.mcpb`) and a wheel for
Windows, macOS and Linux. You need [uv](https://docs.astral.sh/uv/) and
[KiCad](https://www.kicad.org/download/) 10. See
[docs/getting-started.md](docs/getting-started.md) for each client.

Then start a message with **Kairo**:

> Kairo, a 3.3 V rail from 5 V USB for a sensor that draws 120 mA

## Documentation

- [Getting started](docs/getting-started.md): install, connect, the tools
  and their tiers.
- [Board layout](docs/board-layout.md) and
  [router design](docs/router-design.md): how boards are made.
- [Design decisions](docs/assumptions.md): each choice made, and why.
- [Research](docs/research.md): what was studied, and what Kairo took from
  it, credited.
- [Writing code and docs](docs/development.md), for contributors.

## Development

```bash
uv sync                                       # builds the compiled loops; needs Rust
uv run pytest -m "not kicad and not spice"    # drop -m when KiCad and ngspice are installed
uv run ruff check && uv run ruff format --check && uv run pyright
uv run sphinx-build -W docs docs/_build/html  # the documentation site
```

## Licence

Apache License 2.0; see [LICENSE](LICENSE). Third-party licences, and the
licences of the open-hardware boards used as test data, are listed in
[NOTICE](NOTICE).
