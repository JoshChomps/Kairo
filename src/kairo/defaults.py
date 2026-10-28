"""Kairo's own margins, used where a design does not state a spread.

A check over tolerances needs every tolerance. Left to wait for each one,
most results come back ``unverified`` and are of little use to someone in
the middle of a design. So where a design gives no figure for how far a
value may be from nominal, Kairo reckons with a generous one of its own,
and says so: every result that rested on a default names it, and a
figure the design states (from a datasheet, or the user's own) always
takes its place.

This is for spreads only: how far a resistor may be from its marked
value, or a supply from its nominal voltage. It is never used for a fact
about one particular part that has no general answer, such as a
regulator's dropout, a current limit, a thermal resistance or a ceramic
capacitor's loss under DC bias. Those stay ``unverified`` until given.

The figures are Kairo's choices, set wide on purpose: a check that
passes on them holds for any ordinary part, and a design that fails on
them may pass once the real figure is given, which the result says. A
project can change any of them, or turn them off (``margins: stated`` in
the design's policy) for the check before a board is ordered.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from kairo.ir.models import Component, Design, MarginKind
from kairo.library.kinds import PASSIVES

#: Kairo's default margin for each kind of spread, in percent.
DEFAULT_MARGINS: dict[MarginKind, float] = {
    # A general-purpose resistor is 1% or 5%.
    "resistor": 5.0,
    # Ceramic and electrolytic capacitors are commonly 10% or 20%.
    "capacitor": 20.0,
    # Power inductors are commonly 20% or 30%.
    "inductor": 30.0,
    # A supply from outside the design: a wall adapter or a bus.
    "supply": 10.0,
    # A diode's or LED's forward voltage at a stated current.
    "forward_voltage": 20.0,
    # A regulator's output, or its reference, over load and temperature.
    "regulator_output": 5.0,
}
#: Which margin each tolerance parameter of a part takes.
_PARAMETER_KINDS: dict[str, MarginKind] = {
    "v_f_tolerance": "forward_voltage",
    "v_out_tolerance": "regulator_output",
    "v_ref_tolerance": "regulator_output",
}
_PASSIVE_KINDS: dict[str, MarginKind] = {
    "R": "resistor",
    "C": "capacitor",
    "L": "inductor",
}
#: What a default's source reads, so a reader and a test can tell one.
DEFAULT_SOURCE = "Kairo's default margin"


@dataclass(frozen=True, slots=True)
class Spread:
    """How far a value may be from nominal, and where the figure came from.

    Attributes:
        value: The spread, in percent.
        source: Where it came from: the part's own source, ``user-stated``,
            or a line that begins with `DEFAULT_SOURCE`.
        is_default: True if it is one of Kairo's defaults.
    """

    value: float
    source: str
    is_default: bool = False


_used: ContextVar[list[str] | None] = ContextVar("kairo_defaults_used", default=None)


@contextmanager
def collecting() -> Generator[list[str]]:
    """Gather a line for each default used while the block runs.

    Yields:
        The list, filled as defaults are used: ``R1's tolerance: 5%``.
    """
    used: list[str] = []
    token = _used.set(used)
    try:
        yield used
    finally:
        _used.reset(token)


def _note(line: str) -> None:
    used = _used.get()
    if used is not None and line not in used:
        used.append(line)


def margin(design: Design, kind: MarginKind) -> float | None:
    """A project's margin for one kind of spread.

    Args:
        design: The design, for its policy.
        kind: One of `DEFAULT_MARGINS`' names.

    Returns:
        The margin in percent, or ``None`` if the project has turned
        defaults off.
    """
    if design.policy.margins == "stated":
        return None
    changed = design.policy.default_margins.get(kind)
    return changed.value if changed is not None else DEFAULT_MARGINS[kind]


def part_spread(
    design: Design, part: Component, name: str = "tolerance"
) -> Spread | None:
    """A part's spread: the stated one, or failing that Kairo's default.

    Args:
        design: The design the part is in.
        part: The part.
        name: The tolerance parameter: ``tolerance``, ``v_f_tolerance``,
            ``v_out_tolerance`` or ``v_ref_tolerance``.

    Returns:
        The spread, or ``None`` if none is stated and no default applies:
        the project turned defaults off, or the part is of no kind that
        has one.
    """
    stated = part.quantity(name)
    if stated is not None:
        return Spread(stated.value, part.source)
    if name == "tolerance":
        kind = _PASSIVE_KINDS.get(PASSIVES.get(part.lib_id, ("", ""))[0])
    else:
        kind = _PARAMETER_KINDS.get(name)
    if kind is None:
        return None
    percent = margin(design, kind)
    if percent is None:
        return None
    ref = next((r for r, item in design.components.items() if item is part), "a part")
    what = "tolerance" if name == "tolerance" else name
    _note(f"{ref}'s {what}: {percent:g}%")
    return Spread(
        percent,
        f"{DEFAULT_SOURCE} for a {kind.replace('_', ' ')} ({percent:g}%); "
        f"{ref} states none",
        True,
    )


def net_spread(design: Design, net: str) -> Spread | None:
    """A supply net's spread: the stated one, or failing that Kairo's default.

    Args:
        design: The design.
        net: The net, a supply that comes from outside the design.

    Returns:
        The spread, or ``None`` if none is stated and defaults are off.
        A net given as a min and max is read as its nominal plus or
        minus the wider side, which holds the whole stated range.
    """
    given = design.nets[net]
    stated = given.tolerance
    if stated is not None:
        return Spread(stated.value, "user-stated")
    if given.min is not None and given.max is not None and given.voltage is not None:
        nominal = given.voltage.value
        wider = max(given.max.value - nominal, nominal - given.min.value)
        if nominal != 0:
            return Spread(
                wider / abs(nominal) * 100.0,
                f"user-stated range {given.min.value:g} V to {given.max.value:g} V",
            )
    percent = margin(design, "supply")
    if percent is None:
        return None
    _note(f"{net}'s tolerance: {percent:g}%")
    return Spread(
        percent,
        f"{DEFAULT_SOURCE} for a supply ({percent:g}%); {net} states none",
        True,
    )
