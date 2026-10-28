# Writing code and docs

How Kairo's code, comments and documents are written, so the project reads
as one piece no matter who wrote which part.

## Checks to run before a change goes in

```bash
uv run pytest -m "not kicad and not spice"   # drop -m when KiCad and ngspice are installed
uv run ruff check
uv run ruff format --check
uv run pyright
uv run sphinx-build -W docs docs/_build/html
cargo fmt --check && cargo clippy --release -- -D warnings   # in rust/kairo_fast
```

CI runs the same commands on Ubuntu and Windows.

## The compiled loops

`rust/kairo_fast` holds Kairo's few hot loops in Rust. maturin builds
them into the package as `kairo._fast`: `uv sync` does it (Rust must be
installed to work on Kairo), and each platform's wheel carries it built.
Each loop is a line-for-line port of a Python function that stays beside
it as the reference, and Kairo runs the Python one when the module is not
there:

- the router's A* (`search._astar_py`) and its grazing steps
  (`grid._grazes`), switched by `kairo.pcb.autoroute.grid._FAST`;
- the drawing annealer's pricing (`arrange._State`), switched by
  `kairo.kicad.arrange._FAST`.

A port must give the same answer as its reference, bit for bit: the
same arithmetic in the same order, Python's rounding where Python
rounds. `tests/unit/test_autoroute_compiled.py` compares the two, and a
change to a port is not done until a deliberate break in it fails that
test. Port only what a profile shows is hot; everything else stays in
Python.

Tests marked `kicad` find KiCad the same way Kairo does (see Getting
started) and use the demo projects and libraries in its install. Set
`KAIRO_FULL_CORPUS=1` to round-trip every KiCad file in the install
rather than a sample (about 90 s).

## Docstrings

Every public module, class and function has a docstring in Google style.
Ruff checks this (`D` rules), and the API reference is built from them.

- Start with one line that says what it does, in plain words.
- Add `Args:`, `Returns:` and `Raises:` when the function takes arguments,
  returns something worth describing, or raises on purpose.
- Name the unit of every physical quantity, and the source of every formula
  or constant (a datasheet with its page, a standard, an app note).
- Say what the function refuses to do when that matters, for example that
  it returns `unverified` instead of filling in a missing datasheet fact.
  A missing tolerance is the one thing Kairo does fill in, with a default
  from `kairo.defaults` that the result names; read it with `part_spread`
  or `net_spread`, never straight from the part.

```python
def divider_output(v_in: Quantity, r_top: Quantity, r_bottom: Quantity) -> Quantity:
    """Return the unloaded output voltage of a resistive divider.

    Source: Ohm's law; V_out = V_in * R_bottom / (R_top + R_bottom).

    Args:
        v_in: Input voltage, in volts.
        r_top: Resistor from the input to the output node, in ohms.
        r_bottom: Resistor from the output node to ground, in ohms.

    Returns:
        The output voltage, in volts.

    Raises:
        ValueError: If both resistors are zero.
    """
```

Private helpers (names starting with `_`) and tests do not need one when the
name already says what they do.

## Comments

- Explain why, not what. The code already says what it does.
- Write the way you would explain it to a colleague: whole sentences, no
  shorthand, no tags or ticket numbers.
- Put history (what changed, what was tried) in the commit message or
  `CHANGELOG.md`, not in the code.
- No em dashes. Use a comma, a colon or a new sentence.

## Names

- Modules and packages are nouns for what they hold: `schematic_writer`,
  `library`.
- Functions are verbs for what they do: `resolve_pin`, `run_erc`.
- Prefer values that carry their unit. When a bare number cannot be avoided,
  put the unit in the name: `timeout_s`, `current_ma`.
- Documents use lowercase words joined by hyphens: `getting-started.md`.
- Outside pages are cited by link, with the date they were read; their
  text is never copied into the repository.

## Tests

- A test for a bug fix must fail without the fix. Check by reverting it.
- Tests that need KiCad or a simulator are marked `kicad` or `spice` and
  skip when the program is missing.
- Tests never use the network.

## Choices made without asking

Development choices made without the project owner's say go in the
[assumptions log](assumptions.md), with how to undo each one.
