"""Layer L5: does the design meet what was asked of it?

Each requirement comes out as one of three things. ``pass`` means the
worst case over every input's tolerance is proven to meet it. ``fail``
means a value that is reached breaks it, and the result says which.
``unverified`` means an input was missing: the result names each one and
the part it belongs to, and is never counted as a pass. A nominal value
that looks fine proves nothing about a part at the edge of its tolerance,
so a requirement with a missing tolerance is ``unverified`` too.

A regulator loaded beyond its rated current is reported whether or not a
requirement asks, and so is every pin a part gives an absolute maximum
rating for (``pin_ratings``), against the range its net can reach.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from kairo.calc import (
    CATALOGUE,
    CalcResult,
    Given,
    calculate,
    check_limit,
    mlcc_effective_capacitance,
    settable,
    worst_case,
)
from kairo.calc.capacitor import (
    BIAS_FREE,
    STABLE_KINDS,
    TEMPERATURE_LOSS,
    least_capacitance,
    within_stable_region,
)
from kairo.defaults import collecting, net_spread, part_spread
from kairo.ir import (
    Component,
    Design,
    MaxComponentPower,
    MaxInductorCurrent,
    MaxJunctionTemp,
    MaxRailCurrent,
    MaxRipple,
    MinAttenuation,
    MinCapacitance,
    MinHoldupTime,
    PinRating,
    ProgrammedCurrent,
    RailVoltage,
    Setting,
)
from kairo.library.grounding import Grounded
from kairo.library.kinds import PASSIVES, TERMINAL_LIBRARIES
from kairo.parts import PartsError, PartStore
from kairo.power.tree import Draw, PowerTree, Regulator, build_tree, resistance
from kairo.units import Quantity, parse_quantity
from kairo.verify.findings import Finding, Where
from kairo.verify.voltages import (
    Clamp,
    End,
    Reach,
    clamps_of,
    followers,
    net_reach,
)

Status = Literal["pass", "fail", "unverified"]


@dataclass(frozen=True, slots=True)
class RequirementResult:
    """How one requirement came out.

    Attributes:
        index: Its position in the design's list of requirements, or -1
            for a check Kairo makes unasked.
        type: The requirement's type.
        status: ``pass``, ``fail`` or ``unverified``.
        summary: One line saying what was found, with the numbers.
        missing: For ``unverified``, each missing input as a line naming
            the part.
        to_resolve: What to supply, one line each.
        results: The calculations behind it.
        where: The part or net it concerns.
        on_defaults: Each of Kairo's default margins the result rested
            on, a line each, such as ``R1's tolerance: 5%``. Empty if
            every spread it used was stated.
    """

    index: int
    type: str
    status: Status
    summary: str
    missing: tuple[str, ...] = ()
    to_resolve: tuple[str, ...] = ()
    results: tuple[CalcResult, ...] = ()
    where: Where = field(default_factory=Where)
    on_defaults: tuple[str, ...] = ()

    @property
    def basis(self) -> str:
        """What the result's spreads rest on: stated values, or defaults too."""
        return "default margins" if self.on_defaults else "stated values"

    def as_dict(self) -> dict[str, Any]:
        """The result as JSON-ready data, status and what to resolve first."""
        out: dict[str, Any] = {
            "requirement": self.index,
            "type": self.type,
            "status": self.status,
            "to_resolve": list(self.to_resolve),
            "basis": self.basis,
            "summary": self.summary,
            "missing": list(self.missing),
            "calculations": [result.as_dict() for result in self.results],
        }
        if self.on_defaults:
            out["on_default_margins"] = list(self.on_defaults)
        return out

    def finding(self) -> Finding | None:
        """The result as a finding, if it is not a pass."""
        if self.status == "pass":
            return None
        return Finding(
            "L5",
            "E_REQUIREMENT_FAILED"
            if self.status == "fail"
            else "W_REQUIREMENT_UNVERIFIED",
            "error" if self.status == "fail" else "warning",
            f"{self.type}: {self.summary}",
            self.where,
            "; ".join(self.to_resolve),
        )


def evaluate_requirements(
    grounded: Grounded, store: PartStore | None = None
) -> tuple[PowerTree, list[RequirementResult]]:
    """Check every requirement of a design.

    Args:
        grounded: The design with its pins resolved.
        store: Datasheet facts by part number, for DC-bias curves.

    Returns:
        The power tree the checks used, and one result per requirement
        followed by two per regulator: its load against its rating, and
        its output capacitor against the stable region its datasheet
        gives; then one per part that states pin ratings.
    """
    design = grounded.design
    tree = build_tree(grounded)
    results: list[RequirementResult] = []

    def judged(check: Callable[[], RequirementResult]) -> None:
        """Run one check, and note which of Kairo's default margins it used."""
        with collecting() as used:
            result = check()
        if used:
            result = _on_defaults(result, tuple(used))
        results.append(result)

    for index, requirement in enumerate(design.requirements):
        if isinstance(requirement, MaxJunctionTemp):
            judged(lambda i=index, r=requirement: _junction(i, r, design, tree))
        elif isinstance(requirement, RailVoltage):
            judged(lambda i=index, r=requirement: _rail(i, r, grounded, tree))
        elif isinstance(requirement, MaxComponentPower):
            judged(lambda i=index, r=requirement: _power(i, r, grounded, tree))
        elif isinstance(requirement, MinAttenuation):
            judged(lambda i=index, r=requirement: _attenuation(i, r, grounded))
        elif isinstance(requirement, MaxInductorCurrent):
            judged(
                lambda i=index, r=requirement: _inductor_current(i, r, grounded, tree)
            )
        elif isinstance(requirement, MaxRipple):
            judged(lambda i=index, r=requirement: _ripple(i, r, grounded, tree, store))
        elif isinstance(requirement, MinHoldupTime):
            judged(lambda i=index, r=requirement: _holdup(i, r, grounded, tree, store))
        elif isinstance(requirement, MaxRailCurrent):
            judged(lambda i=index, r=requirement: _rail_budget(i, r, grounded, tree))
        elif isinstance(requirement, ProgrammedCurrent):
            judged(lambda i=index, r=requirement: _programmed(i, r, grounded))
        elif isinstance(requirement, Setting):
            judged(lambda i=index, r=requirement: _setting(i, r))
        else:
            judged(
                lambda i=index, r=requirement: _capacitance(i, r, grounded, tree, store)
            )
    for regulator in tree.regulators:
        judged(lambda reg=regulator: _loading(reg, design, tree))
        if regulator.kind == "switching" and regulator.inductor is not None:
            judged(lambda reg=regulator: _current_limit(reg, grounded, tree))
        judged(lambda reg=regulator: _stability(reg, grounded, tree, store))
    rated = [ref for ref, part in design.components.items() if part.pin_ratings]
    if rated:
        with collecting() as used:
            reach = net_reach(grounded, _rail_ranges(grounded, tree))
        clamps = clamps_of(grounded)
        follow = followers(grounded)
        for ref in rated:
            result = _pin_ratings(ref, grounded, reach, clamps, follow)
            if used:
                result = _on_defaults(result, tuple(used))
            results.append(result)
    return tree, results


def _rail_ranges(grounded: Grounded, tree: PowerTree) -> dict[str, Given | None]:
    """Each power net's voltage with its tolerance, for the pin checks."""
    design = grounded.design
    made = {regulator.output_net: regulator for regulator in tree.regulators}
    ranges: dict[str, Given | None] = {}
    for net in tree.rails:
        if net in made:
            given, missing, _ = _made_voltage(grounded, tree, made[net])
            ranges[net] = None if missing else given
        else:
            ranges[net] = _rail_voltage(design, tree, net)
    return ranges


def _pin_ratings(
    ref: str,
    grounded: Grounded,
    reach: dict[str, Reach],
    clamps: list[Clamp],
    follow: dict[str, tuple[str, str]],
) -> RequirementResult:
    """Check each rated pin of a part against the range its net can reach.

    A pin that goes past its rating at a value its net is shown to reach
    fails. One that might, by what Kairo can show, is unverified, and so
    is one whose net's range is not known: the result names the net and
    what would settle it.
    """
    design = grounded.design
    part = design.components[ref]
    on = [pin for pin in grounded.pins if pin.ref == ref]
    failed: list[str] = []
    unsure: list[str] = []
    fine: list[str] = []
    resolve: list[str] = []
    for name, rating in part.pin_ratings.items():
        nets = sorted({pin.net for pin in on if pin.pin.name == name})
        if not nets:
            unsure.append(f"{name}: no connected pin of {ref} has that name")
            resolve.append(f"Check the pin name {name!r} against {ref}'s pins")
            continue
        if rating.to is None:
            bases: list[str | None] = [None]
        else:
            bases = list[str | None](
                sorted({pin.net for pin in on if pin.pin.name == rating.to})
            )
            if not bases:
                unsure.append(f"{name}: no connected pin of {ref} is named {rating.to}")
                resolve.append(f"Check the pin name {rating.to!r} against {ref}'s pins")
                continue
        source = rating.source or part.source
        for net in nets:
            for base in bases:
                low, high = _relative(net, base, reach, clamps, follow)
                where = f"{name} on {net}" + (f", from {rating.to}" if base else "")
                verdicts = _judge_pin(where, net, low, high, rating, source)
                for kind, line, fix in verdicts:
                    {"fail": failed, "unsure": unsure, "fine": fine}[kind].append(line)
                    if fix:
                        resolve.append(fix)
    if failed:
        status: Status = "fail"
    elif unsure:
        status = "unverified"
    else:
        status = "pass"
    lines = [*failed, *unsure, *fine]
    held = any("held by" in line for line in lines)
    note = (
        ". A clamp holds a net only at the current its datasheet figure is for"
        if held
        else ""
    )
    return RequirementResult(
        -1,
        "pin_ratings",
        status,
        f"{ref}: " + "; ".join(lines) + note,
        tuple(unsure) if status == "unverified" else (),
        tuple(dict.fromkeys(resolve)),
        where=Where(ref=ref),
    )


def _relative(
    net: str,
    base: str | None,
    reach: dict[str, Reach],
    clamps: list[Clamp],
    follow: dict[str, tuple[str, str]],
) -> tuple[End, End]:
    """The lowest and highest a net can be from a base net (ground if None).

    A clamp straight between the two bounds their difference, and so does
    one between the nets they follow through a resistor that carries
    nothing (`followers`).
    """
    own = reach.get(net)
    if own is None:
        gone = End(None, False, f"{net}'s range is not known")
        return gone, gone
    if base is None or base == net:
        if base == net:
            zero = End(0.0, True, "on the same net")
            return zero, zero
        return own.low, own.high
    other = reach.get(base)
    if other is None:
        gone = End(None, False, f"{base}'s range is not known")
        return gone, gone
    grounded_base = (
        other.low.value == 0.0 and other.high.value == 0.0 and other.low.reached
    )
    low = _difference(own.low, other.high, grounded_base, base)
    high = _difference(own.high, other.low, grounded_base, base)
    ups = _followed(net, follow)
    downs = _followed(base, follow)
    for clamp in clamps:
        for near, via_near in ups:
            for far, via_far in downs:
                if {clamp.anode, clamp.cathode} != {near, far}:
                    continue
                via = ", ".join(r for r in (via_near, via_far) if r)
                note = f", through {via} at rest" if via else ""
                rise, why = clamp.above(near)
                if rise is not None and (high.value is None or rise < high.value):
                    high = End(rise, False, f"held by {why}{note}")
                fall, why = clamp.below(near)
                if fall is not None and (low.value is None or -fall > low.value):
                    low = End(-fall, False, f"held by {why}{note}")
    return low, high


def _followed(net: str, follow: dict[str, tuple[str, str]]) -> list[tuple[str, str]]:
    """A net and the nets it follows through resistors, with the resistors."""
    chain = [(net, "")]
    seen = {net}
    resistors: list[str] = []
    while net in follow and len(chain) < 4:
        net, resistor = follow[net]
        if net in seen:
            break
        seen.add(net)
        resistors.append(resistor)
        chain.append((net, " and ".join(resistors)))
    return chain


def _difference(end: End, base: End, grounded_base: bool, name: str) -> End:
    """One end of a net's range less the opposite end of its base's."""
    if end.value is None:
        return end
    if base.value is None:
        return End(None, False, base.basis)
    if grounded_base:
        return end
    # Two nets that move are not shown to reach their far ends together.
    return End(end.value - base.value, False, f"{end.basis}, against {name}")


def _judge_pin(
    where: str, net: str, low: End, high: End, rating: PinRating, source: str
) -> list[tuple[str, str, str]]:
    """Each side of one pin against its rating: fine, unsure or failed."""
    out: list[tuple[str, str, str]] = []
    sides = ((rating.max, high, "highest", True), (rating.min, low, "lowest", False))
    for limit, end, word, upper in sides:
        if limit is None:
            continue
        rated = f"rated {limit.value:g} V ({source})"
        if end.value is None:
            out.append(
                (
                    "unsure",
                    f"{where}: its {word} is not known ({end.basis}); {rated}",
                    f"State {net}'s range, or a clamp's voltage that holds it",
                )
            )
        elif (end.value <= limit.value) if upper else (end.value >= limit.value):
            out.append(
                (
                    "fine",
                    f"{where}: {word} {end.value:.4g} V ({end.basis}); {rated}",
                    "",
                )
            )
        elif end.reached:
            out.append(
                (
                    "fail",
                    f"{where}: reaches {end.value:.4g} V ({end.basis}); {rated}",
                    f"Clamp or divide {net} so it stays within the rating",
                )
            )
        else:
            out.append(
                (
                    "unsure",
                    f"{where}: may reach {end.value:.4g} V ({end.basis}); {rated}",
                    f"State {net}'s range if it stays lower, or clamp it",
                )
            )
    return out


def _setting(index: int, requirement: Setting) -> RequirementResult:
    """Whether a chip can hold the value a design relies on for a setting."""
    found = settable(
        requirement.value.value,
        requirement.step.value,
        0.0 if requirement.offset is None else requirement.offset.value,
        None if requirement.min is None else requirement.min.value,
        None if requirement.max is None else requirement.max.value,
    )
    unit = requirement.value.unit
    name = f"{requirement.component} {requirement.name}"

    def shown(value: float | None) -> str:
        return "none" if value is None else f"{value:.6g} {unit}".rstrip()

    if found.exact and found.in_range:
        return RequirementResult(
            index,
            requirement.type,
            "pass",
            f"{name}: {shown(requirement.value.value)} is step {found.code} "
            f"({requirement.source})",
            where=Where(ref=requirement.component),
        )
    reasons: list[str] = []
    if not found.in_range:
        reasons.append(
            f"{shown(requirement.value.value)} is outside the range the chip "
            f"accepts, {shown(found.low)} to {shown(found.high)}"
        )
    if not found.exact:
        reasons.append(
            f"{shown(requirement.value.value)} is not a whole step of "
            f"{shown(requirement.step.value)}; the nearest the chip holds are "
            f"{shown(found.below)} and {shown(found.above)}"
        )
    return RequirementResult(
        index,
        requirement.type,
        "fail",
        f"{name}: " + "; ".join(reasons) + f" ({requirement.source})",
        to_resolve=(
            f"Choose a value for {name} the chip can hold, and check what "
            "depends on it",
        ),
        where=Where(ref=requirement.component),
    )


def _on_defaults(result: RequirementResult, used: tuple[str, ...]) -> RequirementResult:
    """Mark a result as resting on Kairo's default margins, and say what follows.

    A pass on defaults holds for any ordinary part, since the defaults
    are wide. A fail on defaults may be the default's doing: the real
    figure may be tighter, so the result asks for it.
    """
    summary = f"{result.summary}. On Kairo's default margins: {'; '.join(used)}"
    resolve = result.to_resolve
    if result.status == "fail":
        resolve = (
            *resolve,
            "This fails on Kairo's default margins, which are wide. State the "
            f"real figures ({', '.join(line.split(':')[0] for line in used)}) and "
            "check again",
        )
    return dataclasses.replace(
        result, summary=summary, to_resolve=resolve, on_defaults=used
    )


def _given(
    name: str,
    value: float,
    unit: str,
    *,
    ref: str | None = None,
    origin: str = "",
    tolerance: float | None = None,
    source: str = "missing",
) -> Given:
    return Given(name, Quantity(value, unit), origin, ref, tolerance, source)


def _rail_voltage(design: Design, tree: PowerTree, net: str) -> Given:
    """A rail's voltage, with the tolerance of the regulator that makes it."""
    rail = tree.rails[net]
    tolerance = None
    source = "missing"
    if rail.source is not None:
        part = design.components[rail.source]
        # A regulator set by a reference and a divider has the range
        # those give, which the checks that need it work out.
        fixed = part.quantity("v_ref") is None or part.quantity("v_out") is not None
        stated = (
            part_spread(design, part, "v_out_tolerance")
            if fixed or part.quantity("v_out_tolerance") is not None
            else None
        )
        if stated is not None:
            tolerance, source = stated.value, stated.source
    else:
        given = net_spread(design, net)
        if given is not None:
            tolerance, source = given.value, given.source
    return _given(
        f"V({net})",
        rail.voltage,
        "V",
        ref=rail.source,
        origin=f"net {net}",
        tolerance=tolerance,
        source=source,
    )


@dataclass(frozen=True, slots=True)
class _RailCurrent:
    """A rail's current as an input to a check of a maximum.

    Attributes:
        given: The current. With only stated loads it is their sum; with
            branches as well it is the most the rail can draw.
        is_bound: True if the value is the sum of each branch's own
            greatest current, which need not be reached together with
            the other inputs' worst values.
        missing: What kept the most from being worked out, a line each.
        resolve: What to supply, a line each.
    """

    given: Given
    is_bound: bool = False
    missing: tuple[str, ...] = ()
    resolve: tuple[str, ...] = ()


def _load_current(
    design: Design,
    tree: PowerTree,
    net: str,
    grounded: Grounded | None = None,
    floor: float | None = None,
) -> _RailCurrent:
    """The most a rail draws, for a check that rises with its current.

    Loads the design states are taken as maxima. A resistor to ground
    draws most at the rail's highest voltage and its own lowest
    resistance, and one through a diode at the diode's lowest forward
    voltage as well (``I = (V - Vf) / R``, Ohm's law), each end taken
    from a stated tolerance. A part's supply current or a regulator's
    own current counts as its most only if the design marks the
    stated value as the datasheet's maximum (``limits``): a typical
    figure is not a bound. A linear regulator passes on what its output
    rail draws, so that rail's own most is worked out the same way and
    added. A switching regulator draws most with its output rail at its
    highest voltage and greatest current, its input at its lowest and
    its efficiency at its least (``Iin = Vout * Iout / (efficiency *
    Vin)``, conservation of power), and its efficiency counts as a
    least only if the design marks it ``min`` under ``limits``. With a
    ``floor``, the rail is taken to fall that far, as it does while it
    holds up with no supply, and a switching regulator is taken at the
    floor, where it draws most. A
    branch whose tolerance is missing, or any other kind of
    draw, leaves the most unknown: the current then carries no
    tolerance and what depends on it stays unverified.
    """
    rail = tree.rails[net]
    if not rail.unknown and all(draw.kind == "load" for draw in rail.draws):
        return _RailCurrent(
            _given(
                "Iout",
                rail.current,
                "A",
                origin="power tree",
                tolerance=0.0,
                source="loads as the design states them, taken as maxima",
            )
        )
    volts = _rail_voltage(design, tree, net)
    maker = tree.regulator(tree.rails[net].source or "")
    if grounded is not None and maker is not None and volts.tolerance is None:
        # A rail set by a reference and a divider has the range they give.
        volts, _, _ = _made_voltage(grounded, tree, maker)
    led = CATALOGUE["led_current"]
    most = 0.0
    # A draw the tree could not work out at all (a regulator with no
    # efficiency, a part with no stated current) has no greatest value
    # either, whatever the rest come to.
    missing: list[str] = list(dict.fromkeys(u.why for u in rail.unknown))
    resolve: list[str] = list(dict.fromkeys(u.resolve for u in rail.unknown))
    for draw in rail.draws:
        if draw.kind == "load":
            most += draw.current
            continue
        if draw.kind in ("supply", "quiescent"):
            name = "i_supply" if draw.kind == "supply" else "i_q"
            owner = draw.refs[0]
            if design.components[owner].limit_side(name) == "max":
                most += draw.current
            else:
                missing.append(
                    f"{draw.what}: {owner}'s stated {name} is not marked as "
                    "a maximum, so it may be a typical figure"
                )
                resolve.append(
                    f"If {owner}'s {name} is its datasheet maximum, list it "
                    "under limits as max; if not, give the maximum"
                )
            continue
        if draw.kind == "divider":
            upper, lower = draw.refs
            gaps = len(missing)
            if volts.tolerance is None:
                missing.append(f"{net}: no tolerance on its voltage")
                resolve.append(
                    f"Give the output tolerance of the regulator that makes "
                    f"{net}, or the net's own tolerance if it comes from outside"
                )
            ranges: dict[str, tuple[float, float]] = {}
            values: dict[str, float] = {"v": volts.number}
            for key, resistor in (("r1", upper), ("r2", lower)):
                ohms = resistance(design, resistor)
                spread = part_spread(design, design.components[resistor], "tolerance")
                assert ohms is not None
                values[key] = ohms.value
                if spread is None:
                    missing.append(f"{resistor}: no tolerance")
                    resolve.append(f"Give {resistor}'s tolerance")
                else:
                    ranges[key] = _span(ohms.value, spread.value)
            if len(missing) == gaps:
                assert volts.tolerance is not None
                ranges["v"] = _span(volts.number, volts.tolerance)
                series = CATALOGUE["series_resistors_current"]
                most += worst_case(
                    series.formula, values, ranges, series.directions
                ).maximum
            continue
        if draw.kind == "passed_on":
            fed = _load_current(design, tree, draw.through, grounded)
            if fed.missing:
                missing += [
                    f"{draw.through}, fed through {draw.refs[0]}: {line}"
                    for line in fed.missing
                ]
                resolve += list(fed.resolve)
            else:
                most += fed.given.value.value
            continue
        if draw.kind == "switched":
            found = _switched_most(design, tree, net, draw, volts, grounded, floor)
            if isinstance(found, tuple):
                missing += found[0]
                resolve += found[1]
            else:
                most += found
            continue
        if draw.kind not in ("resistor", "indicator"):
            missing.append(f"{draw.what}: its greatest value is not worked out")
            resolve.append(
                f"Simulate the worst case of {net}'s current, or state it as a load"
            )
            continue
        resistor = draw.refs[0]
        ohms = resistance(design, resistor)
        spread = part_spread(design, design.components[resistor], "tolerance")
        assert ohms is not None
        gaps = len(missing)
        if volts.tolerance is None:
            missing.append(f"{net}: no tolerance on its voltage")
            resolve.append(
                f"Give the output tolerance of the regulator that makes {net}, or "
                "the net's own tolerance if it comes from outside"
            )
        if spread is None:
            missing.append(f"{resistor}: no tolerance")
            resolve.append(f"Give {resistor}'s tolerance")
        forward = 0.0
        forward_spread = 0.0
        if draw.kind == "indicator":
            diode = design.components[draw.refs[1]]
            stated = diode.quantity("v_f")
            assert stated is not None
            forward = stated.value
            given_spread = part_spread(design, diode, "v_f_tolerance")
            if given_spread is None:
                missing.append(f"{draw.refs[1]}: no tolerance on its forward voltage")
                resolve.append(
                    f"Give {draw.refs[1]}'s forward voltage tolerance (v_f_tolerance)"
                )
            else:
                forward_spread = given_spread.value
        if len(missing) > gaps:
            continue
        assert volts.tolerance is not None and spread is not None
        ranges = {
            "v_supply": _span(volts.number, volts.tolerance),
            "v_f": _span(forward, forward_spread),
            "r": _span(ohms.value, spread.value),
        }
        if ranges["v_supply"][0] <= ranges["v_f"][1]:
            missing.append(
                f"{draw.what}: over the tolerances {net} can be below the diode's "
                "forward voltage, where the branch's formula does not hold"
            )
            resolve.append(f"Simulate the worst case of {net}'s current")
            continue
        nominal = {"v_supply": volts.number, "v_f": forward, "r": ohms.value}
        most += worst_case(led.formula, nominal, ranges, led.directions).maximum
    if missing:
        return _RailCurrent(
            _given("Iout", rail.current, "A", origin="power tree"),
            missing=tuple(missing),
            resolve=tuple(resolve),
        )
    return _RailCurrent(
        _given(
            "Iout",
            most,
            "A",
            origin="power tree: the most the rail draws over the stated tolerances",
            tolerance=0.0,
            source="stated loads as maxima, and each branch at its own worst case",
        ),
        is_bound=True,
    )


def _switched_most(
    design: Design,
    tree: PowerTree,
    net: str,
    draw: Draw,
    volts: Given,
    grounded: Grounded | None,
    floor: float | None = None,
) -> float | tuple[list[str], list[str]]:
    """The most a switching regulator draws from its input rail.

    With a ``floor``, the input is taken there, its lowest while the rail
    holds up with no supply, in place of the low end of its tolerance.

    Returns:
        The current in amps, or what is missing and how to supply it.
    """
    ref = draw.refs[0]
    part = design.components[ref]
    missing: list[str] = []
    resolve: list[str] = []
    fed = _load_current(design, tree, draw.through, grounded)
    if fed.missing:
        missing += [
            f"{draw.through}, fed through {ref}: {line}" for line in fed.missing
        ]
        resolve += list(fed.resolve)
    efficiency = part.quantity("efficiency")
    if efficiency is None or part.limit_side("efficiency") != "min":
        missing.append(
            f"{ref}: its stated efficiency is not marked as a least, so it may "
            "be a typical figure"
        )
        resolve.append(
            f"Give the least efficiency {ref} reaches at this load and input, and "
            "list efficiency under limits as min"
        )
    out = _rail_voltage(design, tree, draw.through)
    maker = tree.regulator(ref)
    if grounded is not None and maker is not None and out.tolerance is None:
        out, _, _ = _made_voltage(grounded, tree, maker)
    if out.tolerance is None:
        missing.append(f"{draw.through}: no tolerance on its voltage")
        resolve.append(f"Give {ref}'s output tolerance (v_out_tolerance)")
    if volts.tolerance is None and floor is None:
        missing.append(f"{net}: no tolerance on its voltage")
        resolve.append(
            f"Give the tolerance of {net}: the net's own if it comes from outside, "
            "or the output tolerance of the regulator that makes it"
        )
    if missing:
        return missing, resolve
    assert efficiency is not None and out.tolerance is not None
    if floor is not None:
        lowest = (floor, floor)
    else:
        assert volts.tolerance is not None
        lowest = _span(volts.number, volts.tolerance)
    formula = CATALOGUE["switcher_input_current"]
    share = efficiency.value / 100.0
    most_out = fed.given.value.value
    return worst_case(
        formula.formula,
        {
            "v_out": out.number,
            "i_out": most_out,
            "efficiency": share,
            "v_in": volts.number,
        },
        {
            "v_out": _span(out.number, out.tolerance),
            "i_out": (most_out, most_out),
            "efficiency": (share, share),
            "v_in": lowest,
        },
        formula.directions,
    ).maximum


def greatest_rail_current(grounded: Grounded, net: str) -> float | None:
    """The most a rail draws over the stated tolerances, by the calculations.

    This is the number a requirement on the rail is checked with, given
    to the simulation so the two can be compared at the same corner.

    Args:
        grounded: The design with its pins resolved.
        net: The rail.

    Returns:
        The current in amps, or ``None`` if the net is not a rail, draws
        only stated loads (which no tolerance moves), or has a branch
        whose greatest current the calculations do not work out.
    """
    tree = build_tree(grounded)
    rail = tree.rails.get(net)
    if rail is None or all(draw.kind == "load" for draw in rail.draws):
        return None
    # A regulator's stand-in in simulation does not vary its output, so
    # what passes through one cannot be compared at a corner.
    if any(draw.kind not in ("load", "resistor", "indicator") for draw in rail.draws):
        return None
    found = _load_current(grounded.design, tree, net)
    if found.missing or not found.is_bound:
        return None
    return found.given.value.value


def _span(value: float, percent: float) -> tuple[float, float]:
    """The range a tolerance in percent gives a positive value."""
    return (value * (1.0 - percent / 100.0), value * (1.0 + percent / 100.0))


def _limit_or_plain(
    part: Component, name: str, label: str, value: float, unit: str, ref: str
) -> Given:
    """A part's parameter as an input to a check that needs its maximum.

    It carries no tolerance only if the design states it as the
    datasheet's maximum. A stated minimum, or a limit whose side is not
    stated, is no bound on the side this check needs.
    """
    if part.limit_side(name) == "max":
        return _given(
            label,
            value,
            unit,
            ref=ref,
            tolerance=0.0,
            source=f"{part.source} (a limit, so no further tolerance)",
        )
    return _given(label, value, unit, ref=ref)


def _limit_gaps(
    part: Component, names: dict[str, str], ref: str
) -> tuple[list[str], list[str]]:
    """Why stated limits do not serve a check that needs maxima, and the cure.

    Args:
        part: The part.
        names: Each parameter the check takes a maximum of, with its label.
        ref: The part's reference.

    Returns:
        One reason and one thing to do for each limit that is a minimum
        or does not say which side it is.
    """
    missing: list[str] = []
    resolve: list[str] = []
    for name, label in names.items():
        side = part.limit_side(name)
        if side == "min":
            missing.append(
                f"{ref}: {label} is stated as its minimum; this check needs its maximum"
            )
            resolve.append(f"Give {ref}'s maximum {name} from its datasheet")
        elif side == "unstated":
            missing.append(
                f"{ref}: {label} is marked as a limit, and the design does not say "
                "whether it is the maximum or the minimum"
            )
            resolve.append(
                f'Write {ref}\'s limits as a mapping, for example {{"{name}": "max"}}'
            )
    return missing, resolve


def _unverified(
    index: int,
    kind: str,
    summary: str,
    missing: list[str],
    resolve: list[str],
    where: Where,
) -> RequirementResult:
    return RequirementResult(
        index, kind, "unverified", summary, tuple(missing), tuple(resolve), (), where
    )


def _from_result(result: CalcResult) -> tuple[list[str], list[str]]:
    return list(result.accuracy.unverified_because), list(result.accuracy.to_resolve)


def _junction(
    index: int, requirement: MaxJunctionTemp, design: Design, tree: PowerTree
) -> RequirementResult:
    ref = requirement.component
    where = Where(ref=ref)
    kind = requirement.type
    regulator = tree.regulator(ref)
    part = design.components[ref]
    if regulator is None or regulator.kind != "linear":
        return _unverified(
            index,
            kind,
            f"{ref} is not a linear regulator Kairo can work out the dissipation of",
            [f"{ref}: not marked as a linear regulator between two power nets"],
            [f"Set {ref}'s regulator parameter to linear, or state its dissipation"],
            where,
        )
    out = tree.rails[regulator.output_net]
    theta = part.quantity("theta_ja")
    if theta is None:
        return _unverified(
            index,
            kind,
            f"{ref}'s junction temperature cannot be computed",
            [f"{ref}: no theta_ja"],
            [f"Give {ref}'s theta_ja from its datasheet, with the page"],
            where,
        )
    quiescent = part.quantity("i_q")
    current = _load_current(design, tree, regulator.output_net)
    inputs = {
        "v_in": _rail_voltage(design, tree, regulator.input_net),
        "v_out": _rail_voltage(design, tree, regulator.output_net),
        "i_out": current.given,
        "i_q": _given(
            "Iq",
            0.0,
            "A",
            ref=ref,
            origin="not given; its term is left out, so the result is a lower bound",
        ),
        "theta_ja": _given("theta_JA", theta.value, "degC/W", ref=ref),
        "t_ambient": _given(
            "TA",
            requirement.ambient.value,
            "degC",
            origin="the requirement",
            tolerance=0.0,
            source="the requirement",
        ),
    }
    if quiescent is not None:
        inputs["i_q"] = _limit_or_plain(part, "i_q", "Iq", quiescent.value, "A", ref)
    inputs["theta_ja"] = _limit_or_plain(
        part, "theta_ja", "theta_JA", theta.value, "degC/W", ref
    )
    # A datasheet gives theta_JA as a typical figure on the JEDEC test
    # board, never as a limit. A project may say how far above it to
    # reckon; Kairo chooses no margin of its own.
    margin = design.policy.theta_ja_margin
    by_policy = margin is not None and part.limit_side("theta_ja") != "max"
    policy_note = ""
    if margin is not None and by_policy:
        inputs["theta_ja"] = _given(
            "theta_JA",
            theta.value,
            "degC/W",
            ref=ref,
            tolerance=margin.value,
            source=(
                "the project's thermal policy: the datasheet's theta_JA, a typical "
                f"value, with a margin of {margin.value:g}%"
            ),
        )
        policy_note = (
            f"; theta_JA is the datasheet's typical value plus {margin.value:g}% by "
            "the project's thermal policy, not a limit"
        )
    limit = requirement.limit.value
    result = calculate("ldo_junction_temperature", inputs)
    assert result.value is not None
    value = result.value.value
    if current.is_bound:
        # The value at nominal uses the rail's nominal current, not the
        # most it can draw.
        at_nominal = calculate(
            "ldo_junction_temperature",
            {**inputs, "i_out": _given("Iout", out.current, "A", origin="power tree")},
        )
        assert at_nominal.value is not None
        value = at_nominal.value.value
    lower_bound = quiescent is None or out.is_bound
    shown = f"{'at least ' if lower_bound else ''}{value:.1f} degC"
    if value > limit:
        # Even with the unknown terms left out, the limit is broken; they
        # can only add to it.
        return RequirementResult(
            index,
            kind,
            "fail",
            f"{ref}'s junction is {shown} at {requirement.ambient.value:g} degC "
            f"ambient; the limit is {limit:g} degC",
            results=(result,),
            where=where,
        )
    missing, resolve = _from_result(result)
    needed = {"i_q": "Iq"} if by_policy else {"i_q": "Iq", "theta_ja": "theta_JA"}
    gaps, cures = _limit_gaps(part, needed, ref)
    missing += gaps
    resolve += cures
    missing += current.missing
    resolve += current.resolve
    if quiescent is None:
        missing.insert(
            0,
            f"{ref}: no Iq; the omitted term Vin*Iq is not negative, so "
            f"{value:.1f} degC is a lower bound",
        )
        resolve.insert(
            0, f"Give {ref}'s quiescent current (i_q) from its datasheet, with the page"
        )
    for unknown in out.unknown:
        missing.append(f"{unknown.ref}: {unknown.why}")
        resolve.append(unknown.resolve)
    if missing:
        return RequirementResult(
            index,
            kind,
            "unverified",
            f"{ref}'s junction is {shown}; a lower bound cannot show the "
            f"{limit:g} degC limit is respected"
            if lower_bound
            else f"{ref}'s junction is {shown} at nominal; the worst case is not "
            "established",
            tuple(missing),
            tuple(resolve),
            (result,),
            where,
        )
    status = check_limit(result, limit, "at most")
    assert result.case is not None
    if current.is_bound and status == "fail":
        # The rail's greatest current and the regulator's greatest drop
        # happen at opposite ends of the output voltage, so this figure
        # is above the true worst case and does not show a failure.
        return RequirementResult(
            index,
            kind,
            "unverified",
            f"{ref}'s junction is {value:.1f} degC at nominal and no more than "
            f"{result.case.maximum:.1f} degC over the tolerances, a bound that is "
            f"not reached; the limit is {limit:g} degC and lies between",
            (f"{ref}: the worst case is bounded, not found",),
            (f"Simulate {ref}'s dissipation at the worst case",),
            (result,),
            where,
        )
    return RequirementResult(
        index,
        kind,
        status,
        f"{ref}'s junction is {value:.1f} degC at nominal and at most "
        f"{result.case.maximum:.1f} degC over the tolerances; the limit is "
        f"{limit:g} degC{policy_note}",
        results=(result,),
        where=where,
    )


def _rail(
    index: int, requirement: RailVoltage, grounded: Grounded, tree: PowerTree
) -> RequirementResult:
    design = grounded.design
    net = requirement.net
    where = Where(net=net)
    kind = requirement.type
    rail = tree.rails.get(net)
    if rail is None or rail.source is None:
        return _unverified(
            index,
            kind,
            f"nothing in the design sets {net}'s voltage",
            [f"{net}: no regulator makes this rail"],
            [f"Say what sets {net}, with its output voltage and tolerance"],
            where,
        )
    ref = rail.source
    part = design.components[ref]
    if part.quantity("v_out") is None and part.quantity("v_ref") is not None:
        if part.params.get("regulator") == "switching":
            return _switched_rail(index, requirement, grounded, ref)
        return _adjustable(index, requirement, grounded, ref)
    output, spread = (
        part.quantity("v_out"),
        part_spread(design, part, "v_out_tolerance"),
    )
    missing: list[str] = []
    resolve: list[str] = []
    if output is None:
        missing.append(f"{ref}: no Vout")
        resolve.append(f"Give {ref}'s output voltage (v_out) from its datasheet")
    if spread is None:
        missing.append(f"{ref}: no tolerance on Vout")
        resolve.append(
            f"Give {ref}'s output voltage tolerance (v_out_tolerance) from its "
            "datasheet, with the page"
        )
    if output is None or spread is None:
        return _unverified(
            index, kind, f"{net}'s range is not known", missing, resolve, where
        )
    low = output.value * (1 - spread.value / 100)
    high = output.value * (1 + spread.value / 100)
    allowed_low = requirement.nominal.value * (1 - requirement.tolerance.value / 100)
    allowed_high = requirement.nominal.value * (1 + requirement.tolerance.value / 100)
    ok = allowed_low <= low and high <= allowed_high
    return RequirementResult(
        index,
        kind,
        "pass" if ok else "fail",
        f"{net} is {low:.4g} V to {high:.4g} V ({ref}: {output.value:g} V "
        f"+/-{spread.value:g}%); allowed is {allowed_low:.4g} V to "
        f"{allowed_high:.4g} V",
        where=where,
    )


def _attenuation(
    index: int, requirement: MinAttenuation, grounded: Grounded
) -> RequirementResult:
    """Judge the loss of a first-order RC low-pass at a frequency.

    The filter is one resistor from the input net to the output net and
    one capacitor from the output net to ground, with nothing else on
    the output but connectors. The loss is least with the resistor and
    the capacitor at the bottom of their tolerances, so those decide.
    The output is taken as unloaded: a resistance or capacitance hung on
    it only adds loss (with a load RL the gain is a/sqrt(1 + (a x)^2),
    a = RL/(R + RL) < 1 and x = 2 pi f R C, which is never above
    1/sqrt(1 + x^2)).

    A ceramic capacitor that loses capacitance with bias and temperature
    (any class II dielectric) would lose attenuation with it, by an
    amount this check does not work out, so it is ``unverified``.
    """
    design = grounded.design
    kind = requirement.type
    where = Where(net=requirement.output)
    grounds = {name for name, net in design.nets.items() if net.role == "ground"}
    nets_of: dict[str, list[str]] = {}
    for pin in grounded.pins:
        nets_of.setdefault(pin.ref, []).append(pin.net)

    def passive(ref: str) -> str:
        return PASSIVES.get(design.components[ref].lib_id, ("", ""))[0]

    on_output = sorted({pin.ref for pin in grounded.net_pins(requirement.output)})
    resistors = [
        ref
        for ref in on_output
        if passive(ref) == "R"
        and sorted(nets_of[ref]) == sorted((requirement.input, requirement.output))
    ]
    capacitors = [
        ref
        for ref in on_output
        if passive(ref) == "C"
        and len(nets_of[ref]) == 2
        and any(net in grounds for net in nets_of[ref])
    ]
    others = [
        ref
        for ref in on_output
        if ref not in resistors
        and ref not in capacitors
        and design.components[ref].lib_id.partition(":")[0] not in TERMINAL_LIBRARIES
    ]
    if len(resistors) != 1 or len(capacitors) != 1 or others:
        found = (
            f"{len(resistors)} resistor(s) from {requirement.input} to "
            f"{requirement.output}, {len(capacitors)} capacitor(s) from "
            f"{requirement.output} to ground"
            + (f", and {', '.join(others)} also on the output" if others else "")
        )
        return _unverified(
            index,
            kind,
            f"{requirement.output} is not the output of a first-order RC low-pass "
            "Kairo can work out",
            [f"{requirement.output}: found {found}"],
            ["Simulate the filter with an ac analysis for its loss"],
            where,
        )
    resistor, capacitor = resistors[0], capacitors[0]
    ohms = resistance(design, resistor)
    part = design.components[capacitor]
    missing: list[str] = []
    resolve: list[str] = []
    try:
        farads = parse_quantity(part.value or "", "F", bare=True)
    except ValueError:
        farads = None
    if ohms is None:
        missing.append(f"{resistor}: no resistance in its value")
        resolve.append(f"Give {resistor} a value such as 1k")
    if farads is None:
        missing.append(f"{capacitor}: no capacitance in its value")
        resolve.append(f"Give {capacitor} a value such as 100nF")
    dielectric = str(part.params.get("dielectric", "")).upper()
    if dielectric not in BIAS_FREE and dielectric not in STABLE_KINDS:
        missing.append(
            f"{capacitor}: its dielectric is "
            f"{dielectric or 'not given'}, and how much capacitance it keeps in "
            "use is not worked out for this check"
        )
        resolve.append(
            f"Use a C0G capacitor for {capacitor} and say so, or simulate the "
            "filter with the capacitance it keeps"
        )
    if ohms is None or farads is None or missing:
        return _unverified(
            index,
            kind,
            f"the loss at {requirement.frequency.value:g} Hz cannot be established",
            missing,
            resolve,
            where,
        )
    r_tolerance = part_spread(design, design.components[resistor], "tolerance")
    c_tolerance = part_spread(design, part, "tolerance")
    result = calculate(
        "rc_lowpass_attenuation",
        {
            "f": _given(
                "f",
                requirement.frequency.value,
                "Hz",
                origin="the requirement",
                tolerance=0.0,
                source="the requirement",
            ),
            "r": _given(
                "R",
                ohms.value,
                "ohm",
                ref=resistor,
                tolerance=None if r_tolerance is None else r_tolerance.value,
                source=r_tolerance.source if r_tolerance else "missing",
            ),
            "c": _given(
                "C",
                farads.value,
                "F",
                ref=capacitor,
                tolerance=None if c_tolerance is None else c_tolerance.value,
                source=c_tolerance.source if c_tolerance else "missing",
            ),
        },
    )
    assert result.value is not None
    wanted = requirement.value.value
    status = check_limit(result, wanted, "at least")
    at_nominal = f"{result.value.value:.2f} dB at nominal"
    shown = (
        f"the loss from {requirement.input} to {requirement.output} at "
        f"{requirement.frequency.value:g} Hz is {at_nominal}"
    )
    if status == "unverified":
        gaps, cures = _from_result(result)
        return RequirementResult(
            index,
            kind,
            "unverified",
            f"{shown}; the worst case is not established",
            tuple(gaps),
            tuple(cures),
            (result,),
            where,
        )
    least = result.case.minimum if result.case is not None else result.value.value
    return RequirementResult(
        index,
        kind,
        status,
        f"{shown} and at least {least:.2f} dB over the tolerances; "
        f"{wanted:g} dB is wanted",
        results=(result,),
        where=where,
    )


def _power(
    index: int, requirement: MaxComponentPower, grounded: Grounded, tree: PowerTree
) -> RequirementResult:
    design = grounded.design
    ref = requirement.component
    where = Where(ref=ref)
    kind = requirement.type
    part = design.components[ref]
    rating = part.quantity("p_rating")
    ohms = resistance(design, ref)
    draw = next(
        (
            (rail, d)
            for rail in tree.rails.values()
            for d in rail.draws
            if ref in d.refs and d.refs[0] == ref
        ),
        None,
    )
    missing: list[str] = []
    resolve: list[str] = []
    if rating is None:
        missing.append(f"{ref}: no power rating")
        resolve.append(f"Give {ref}'s power rating (p_rating)")
    if ohms is None or draw is None:
        missing.append(f"{ref}: its current could not be worked out from the design")
        resolve.append(
            f"Kairo works out a resistor's power when it runs from a rail to "
            f"ground, alone or through an LED; state {ref}'s dissipation otherwise"
        )
    if rating is None or ohms is None or draw is None:
        return _unverified(
            index,
            kind,
            f"{ref}'s dissipation is not established",
            missing,
            resolve,
            where,
        )
    rail, found = draw
    tolerance = part_spread(design, part, "tolerance")
    resistor = _given(
        "R",
        ohms.value,
        "ohm",
        ref=ref,
        tolerance=tolerance.value if tolerance else None,
        source=tolerance.source if tolerance else "missing",
    )
    supply = _rail_voltage(design, tree, rail.net)
    if len(found.refs) == 2:
        diode = design.components[found.refs[1]]
        forward = diode.quantity("v_f")
        spread = part_spread(design, diode, "v_f_tolerance")
        assert forward is not None
        result = calculate(
            "led_resistor_power",
            {
                "v_supply": supply,
                "v_f": _given(
                    "Vf",
                    forward.value,
                    "V",
                    ref=found.refs[1],
                    tolerance=spread.value if spread else None,
                    source=spread.source if spread else "missing",
                ),
                "r": resistor,
            },
        )
    else:
        # A resistor straight to ground: the same formula with no drop.
        result = calculate(
            "resistor_power",
            {
                "i": _given("I", found.current, "A", origin="power tree"),
                "r": resistor,
            },
        )
    assert result.value is not None
    limit = rating.value * requirement.derating.value / 100
    power = result.value.value
    summary = (
        f"{ref} dissipates {power * 1000:.3g} mW at nominal; the limit is "
        f"{limit * 1000:.3g} mW ({requirement.derating.value:g}% of "
        f"{rating.value * 1000:g} mW)"
    )
    status = check_limit(result, limit, "at most")
    if status == "unverified":
        missing, resolve = _from_result(result)
        return RequirementResult(
            index,
            kind,
            "unverified",
            summary,
            tuple(missing),
            tuple(resolve),
            (result,),
            where,
        )
    if status == "fail" and result.case is not None and power <= limit:
        summary += (
            f"; at the worst corner of the tolerances it is "
            f"{result.case.maximum * 1000:.3g} mW"
        )
    return RequirementResult(
        index, kind, status, summary, results=(result,), where=where
    )


def _bank(
    grounded: Grounded,
    tree: PowerTree,
    store: PartStore | None,
    capacitors: list[str],
    between: tuple[str, str],
    basis: str,
) -> tuple[float, list[str], list[str], list[CalcResult], list[str]]:
    """The least capacitance some capacitors between two nets give together.

    Returns:
        The total in farads, what is missing and how to supply it, the
        calculations behind it, and notes on them.
    """
    design = grounded.design
    first, second = between
    total = 0.0
    missing: list[str] = []
    resolve: list[str] = []
    results: list[CalcResult] = []
    notes: list[str] = []
    for ref in capacitors:
        part = design.components[ref]
        try:
            marked = parse_quantity(part.value or "", "F", bare=True)
        except ValueError:
            missing.append(f"{ref}: no capacitance in its value field")
            resolve.append(f"Give {ref}'s value, for example 22uF")
            continue
        if basis == "nominal":
            total += marked.value
            continue
        tolerance = part_spread(design, part, "tolerance")
        mpn = part.params.get("mpn")
        mpn = mpn if isinstance(mpn, str) else None
        curve = None
        if mpn and store is not None:
            try:
                found = store.get(mpn)
            except PartsError as exc:
                missing.append(f"{ref}: the part file for {mpn} cannot be used: {exc}")
                resolve.append(f"Correct the part file for {mpn}")
                continue
            curve = found.dc_bias if found else None
        rail = first if first in tree.rails else second
        if rail not in tree.rails:
            missing.append(f"{ref}: neither net is a power net with a voltage")
            resolve.append(f"Give {first} or {second} a voltage")
            continue
        dielectric = part.params.get("dielectric")
        if isinstance(dielectric, str) and dielectric.upper() in STABLE_KINDS:
            # Not a class II ceramic: it gives its marked value less its
            # tolerance, whatever the voltage across it.
            low = least_capacitance(
                _given(
                    "Cnom",
                    marked.value,
                    "F",
                    ref=ref,
                    tolerance=tolerance.value if tolerance else None,
                    source=tolerance.source if tolerance else "missing",
                )
            )
            if low is None:
                missing.append(f"{ref}: no tolerance on its capacitance")
                resolve.append(f"Give {ref}'s tolerance")
            else:
                total += low
            continue
        result = mlcc_effective_capacitance(
            _given(
                "Cnom",
                marked.value,
                "F",
                ref=ref,
                tolerance=tolerance.value if tolerance else None,
                source=tolerance.source if tolerance else "missing",
            ),
            _rail_voltage(design, tree, rail),
            dielectric=dielectric if isinstance(dielectric, str) else None,
            curve=curve,
            ref=ref,
            mpn=mpn,
        )
        results.append(result)
        if result.value is None or result.accuracy.status != "verified":
            missing += list(result.accuracy.unverified_because)
            resolve += list(result.accuracy.to_resolve)
        if result.value is not None:
            total += result.value.value
            notes += [w for w in result.warnings if "typical" in w]
    return total, missing, resolve, results, notes


def _rail_budget(
    index: int, requirement: MaxRailCurrent, grounded: Grounded, tree: PowerTree
) -> RequirementResult:
    """A rail's greatest current against a budget.

    It fails if the rail's current at nominal values is over the budget,
    or its greatest over the tolerances is; it passes only if that
    greatest is worked out and within it.
    """
    net = requirement.net
    where = Where(net=net)
    kind = requirement.type
    budget = requirement.value.value
    asked = f"at most {budget * 1000:.4g} mA is allowed"
    rail = tree.rails.get(net)
    if rail is None:
        return _unverified(
            index,
            kind,
            f"{net} is not a power net; {asked}",
            [f"{net}: not a rail of the power tree"],
            [f"Give {net} the role power and its voltage"],
            where,
        )
    nominal = rail.current
    unknown = [u.why for u in rail.unknown]
    summary = (
        f"{net} draws {nominal * 1000:.4g} mA at nominal"
        + (" (a lower bound: some draws are not known)" if unknown else "")
        + f"; {asked}"
    )
    if nominal > budget:
        return RequirementResult(index, kind, "fail", summary, where=where)
    drawn = _load_current(grounded.design, tree, net, grounded)
    missing = list(dict.fromkeys([*unknown, *drawn.missing]))
    resolve = list(dict.fromkeys([*(u.resolve for u in rail.unknown), *drawn.resolve]))
    if missing:
        return _unverified(index, kind, summary, missing, resolve, where)
    most = drawn.given.value.value
    summary += f"; over the tolerances it draws at most {most * 1000:.4g} mA"
    return RequirementResult(
        index, kind, "pass" if most <= budget else "fail", summary, where=where
    )


def _programmed(
    index: int, requirement: ProgrammedCurrent, grounded: Grounded
) -> RequirementResult:
    """A current a chip sets from one resistor between a pin and ground.

    The resistor must be the only thing on the pin's net besides the
    pin: a second path would carry part of the pin's current, which the
    datasheet's formula does not allow for.
    """
    design = grounded.design
    ref, name, factor = requirement.component, requirement.pin, requirement.factor
    where = Where(ref=ref)
    kind = requirement.type
    low, high = requirement.min, requirement.max
    asked = "; ".join(
        text
        for text in (
            f"at least {low.value * 1000:.4g} mA" if low is not None else "",
            f"at most {high.value * 1000:.4g} mA" if high is not None else "",
        )
        if text
    )
    asked = f"{asked} is required"
    part = design.components.get(ref)
    if part is None:
        return _unverified(
            index,
            kind,
            f"the design has no part {ref}; {asked}",
            [f"{ref}: not in the design"],
            [f"Name the chip that sets the current, not {ref}"],
            where,
        )
    nets = {p.net for p in grounded.pins if p.ref == ref and p.pin.name == name}
    if len(nets) != 1:
        return _unverified(
            index,
            kind,
            f"{ref}'s pin {name} is not on one net; {asked}",
            [f"{ref}: pin {name} is connected to {len(nets)} nets"],
            [f"Connect {ref}.{name} to its programming resistor"],
            where,
        )
    net = next(iter(nets))
    grounds = {n for n, item in design.nets.items() if item.role == "ground"}
    on_net = [p for p in grounded.pins if p.net == net and p.ref != ref]
    joined: dict[str, set[str]] = {}
    for pin in grounded.pins:
        joined.setdefault(pin.ref, set()).add(pin.net)
    resistors = [
        p.ref
        for p in on_net
        if PASSIVES.get(design.components[p.ref].lib_id, ("", ""))[0] == "R"
        and len(joined[p.ref]) == 2
        and (joined[p.ref] - {net}) <= grounds
    ]
    others = sorted({p.ref for p in on_net} - set(resistors))
    if len(resistors) != 1 or others:
        found = ", ".join(sorted({p.ref for p in on_net})) or "nothing"
        return _unverified(
            index,
            kind,
            f"{ref}.{name} is not set by one resistor to ground; {asked}",
            [f"{net}: {found} besides {ref}.{name}"],
            [f"Connect one resistor from {ref}.{name} to ground and nothing else"],
            where,
        )
    resistor = resistors[0]
    ohms = resistance(design, resistor)
    if ohms is None:
        return _unverified(
            index,
            kind,
            f"{resistor}'s value is not a resistance; {asked}",
            [f"{resistor}: no resistance in its value field"],
            [f"Give {resistor}'s value, for example 1.78k"],
            where,
        )
    r_spread = part_spread(design, design.components[resistor], "tolerance")
    k = part.quantity(factor)
    k_spread = part.quantity(f"{factor}_tolerance")
    missing: list[str] = []
    resolve: list[str] = []
    if k is None:
        return _unverified(
            index,
            kind,
            f"{ref} states no {factor}; {asked}",
            [f"{ref}: no {factor}, the datasheet's factor for {name}"],
            [
                f"Give {ref}'s {factor} from its datasheet, in A*ohm written in "
                f"volts, with {factor}_tolerance covering its minimum and maximum"
            ],
            where,
        )
    result = calculate(
        "programmed_current",
        {
            "k": _given(
                "K",
                k.value,
                "V",
                ref=ref,
                tolerance=k_spread.value if k_spread is not None else None,
                source=part.source if k_spread is not None else "missing",
            ),
            "r": _given(
                resistor,
                ohms.value,
                "ohm",
                ref=resistor,
                tolerance=r_spread.value if r_spread else None,
                source=r_spread.source if r_spread else "missing",
            ),
        },
    )
    lacks, supply = _from_result(result)
    missing += lacks
    resolve += supply
    assert result.value is not None
    nominal = result.value.value
    if result.case is None or missing:
        return RequirementResult(
            index,
            kind,
            "unverified",
            f"{ref}.{name} with {resistor} sets {nominal * 1000:.4g} mA at nominal; "
            f"its range is not established; {asked}",
            tuple(missing),
            tuple(resolve),
            (result,),
            where,
        )
    least, most = result.case.minimum, result.case.maximum
    ok = (low is None or least >= low.value) and (high is None or most <= high.value)
    return RequirementResult(
        index,
        kind,
        "pass" if ok else "fail",
        f"{ref}.{name} with {resistor} sets {least * 1000:.4g} mA to "
        f"{most * 1000:.4g} mA over the tolerances; {asked}",
        results=(result,),
        where=where,
    )


def _holdup(
    index: int,
    requirement: MinHoldupTime,
    grounded: Grounded,
    tree: PowerTree,
    store: PartStore | None,
) -> RequirementResult:
    design = grounded.design
    net = requirement.net
    where = Where(net=net)
    kind = requirement.type
    wanted = requirement.time.value
    floor = requirement.floor.value
    asked = f"{wanted * 1000:g} ms above {floor:g} V is required"
    if net not in tree.rails:
        return _unverified(
            index,
            kind,
            f"{net} is not a power net with a voltage; {asked}",
            [f"{net}: no voltage to start from"],
            [f"Give {net} the role power and its voltage"],
            where,
        )
    grounds = {name for name, item in design.nets.items() if item.role == "ground"}
    joined: dict[str, set[str]] = {}
    for pin in grounded.pins:
        joined.setdefault(pin.ref, set()).add(pin.net)
    banks: dict[str, list[str]] = {}
    for ref, nets in joined.items():
        if (
            PASSIVES.get(design.components[ref].lib_id, ("", ""))[0] == "C"
            and net in nets
            and len(nets) == 2
            and (nets - {net}) <= grounds
        ):
            banks.setdefault(next(iter(nets - {net})), []).append(ref)
    capacitors = [ref for refs in banks.values() for ref in refs]
    if not capacitors:
        return RequirementResult(
            index,
            kind,
            "fail",
            f"no capacitor joins {net} and ground; {asked}",
            where=where,
        )
    total = 0.0
    missing: list[str] = []
    resolve: list[str] = []
    results: list[CalcResult] = []
    for ground, refs in banks.items():
        part, lacks, supply, behind, _ = _bank(
            grounded, tree, store, refs, (net, ground), "effective"
        )
        total += part
        missing += lacks
        resolve += supply
        results += behind
    drawn = _load_current(design, tree, net, grounded, floor)
    missing += list(drawn.missing)
    resolve += list(drawn.resolve)
    if not missing and drawn.given.value.value <= 0:
        missing.append(f"{net}: nothing is stated to draw from it")
        resolve.append(f"State what {net} must supply while it holds, as a load")
    if missing:
        return RequirementResult(
            index,
            kind,
            "unverified",
            f"how long {net} holds is not established; {asked}",
            tuple(missing),
            tuple(resolve),
            tuple(results),
            where,
        )
    start = _rail_voltage(design, tree, net)
    lowest = start.value.value * (1.0 - (start.tolerance or 0.0) / 100.0)
    if lowest <= floor:
        return RequirementResult(
            index,
            kind,
            "fail",
            f"{net} can be as low as {lowest:.4g} V before its supply goes, which "
            f"is not above the floor; {asked}",
            results=tuple(results),
            where=where,
        )
    result = calculate(
        "holdup_time",
        {
            "c": _given(
                "C",
                total,
                "F",
                origin=f"{', '.join(capacitors)}, at their least in use",
                tolerance=0.0,
                source="the capacitors' least capacitance under tolerance and bias",
            ),
            "v_start": start,
            "v_floor": _given(
                "Vfloor",
                floor,
                "V",
                origin="the requirement",
                tolerance=0.0,
                source="the requirement",
            ),
            "i": drawn.given,
        },
    )
    results.append(result)
    if result.case is None or result.accuracy.status != "verified":
        lacks, supply = _from_result(result)
        return RequirementResult(
            index,
            kind,
            "unverified",
            f"how long {net} holds is not established; {asked}",
            tuple(lacks),
            tuple(supply),
            tuple(results),
            where,
        )
    least = result.case.minimum
    return RequirementResult(
        index,
        kind,
        "pass" if least >= wanted else "fail",
        f"{', '.join(capacitors)} ({total * 1e6:.4g} uF at their least) hold {net} "
        f"above {floor:g} V for at least {least * 1000:.4g} ms at "
        f"{drawn.given.value.value * 1000:.4g} mA; {wanted * 1000:g} ms is required. "
        "The step across the capacitors' ESR is not counted",
        results=tuple(results),
        where=where,
    )


def _capacitance(
    index: int,
    requirement: MinCapacitance,
    grounded: Grounded,
    tree: PowerTree,
    store: PartStore | None,
) -> RequirementResult:
    design = grounded.design
    first, second = requirement.between
    where = Where(net=first)
    kind = requirement.type
    nets: dict[str, set[str]] = {}
    for pin in grounded.pins:
        nets.setdefault(pin.ref, set()).add(pin.net)
    capacitors = [
        ref
        for ref, joined in nets.items()
        if joined == {first, second}
        and PASSIVES.get(design.components[ref].lib_id, ("", ""))[0] == "C"
    ]
    wanted = requirement.value.value
    if not capacitors:
        return RequirementResult(
            index,
            kind,
            "fail",
            f"no capacitor joins {first} and {second}; {wanted * 1e6:g} uF is required",
            where=where,
        )
    total, missing, resolve, results, notes = _bank(
        grounded, tree, store, capacitors, (first, second), requirement.basis
    )

    basis = (
        "nominal (marked values, as asked)"
        if requirement.basis == "nominal"
        else "effective"
    )
    summary = (
        f"{', '.join(capacitors)} give {total * 1e6:.3g} uF {basis} between "
        f"{first} and {second}; {wanted * 1e6:g} uF is required"
    )
    if missing:
        return RequirementResult(
            index,
            kind,
            "unverified",
            f"the effective capacitance between {first} and {second} is not "
            "established",
            tuple(missing),
            tuple(resolve),
            tuple(results),
            where,
        )
    if notes:
        summary += "; " + notes[0]
    return RequirementResult(
        index,
        kind,
        "pass" if total >= wanted else "fail",
        summary,
        results=tuple(results),
        where=where,
    )


def _loading(
    regulator: Regulator, design: Design, tree: PowerTree
) -> RequirementResult:
    ref = regulator.ref
    where = Where(ref=ref, net=regulator.output_net)
    kind = "regulator_load"
    rated = design.components[ref].quantity("i_out_max")
    out = tree.rails[regulator.output_net]
    if rated is None:
        return _unverified(
            -1,
            kind,
            f"{ref}'s load cannot be compared with its rating",
            [f"{ref}: no rated output current"],
            [f"Give {ref}'s rated output current (i_out_max) from its datasheet"],
            where,
        )
    shown = f"{'at least ' if out.is_bound else ''}{out.current * 1000:.4g} mA"
    summary = (
        f"{ref} supplies {shown} on {regulator.output_net}; it is rated for "
        f"{rated.value * 1000:g} mA"
    )
    if out.current > rated.value:
        return RequirementResult(-1, kind, "fail", summary, where=where)
    if out.is_bound:
        return RequirementResult(
            -1,
            kind,
            "unverified",
            summary,
            tuple(f"{u.ref}: {u.why}" for u in out.unknown),
            tuple(u.resolve for u in out.unknown),
            where=where,
        )
    return RequirementResult(-1, kind, "pass", summary, where=where)


def _stability(
    regulator: Regulator,
    grounded: Grounded,
    tree: PowerTree,
    store: PartStore | None,
) -> RequirementResult:
    """A regulator's output capacitor against its datasheet's stable region."""
    design = grounded.design
    ref = regulator.ref
    net = regulator.output_net
    where = Where(ref=ref, net=net)
    kind = "regulator_stability"
    part = design.components[ref]
    region = {
        name: part.quantity(name)
        for name in ("c_out_min", "c_out_esr_min", "c_out_esr_max")
    }
    if regulator.kind == "switching":
        return _unverified(
            -1,
            kind,
            f"{ref} is a switching regulator, whose stability is a matter of "
            "its control loop and not of a capacitor's ESR range",
            [f"{ref}: its loop's phase margin is not established"],
            [
                f"Measure {ref}'s loop gain with its maker's model (simulate "
                "with loop), or follow the compensation its datasheet gives"
            ],
            where,
        )
    if any(value is None for value in region.values()):
        absent = ", ".join(name for name, value in region.items() if value is None)
        return _unverified(
            -1,
            kind,
            f"{ref}'s stable region is not given, so its output capacitor "
            "cannot be checked against it",
            [f"{ref}: no {absent}"],
            [
                f"Give {ref}'s least output capacitance (c_out_min) and the "
                "lowest and highest ESR it is stable with (c_out_esr_min, "
                "c_out_esr_max) from its datasheet"
            ],
            where,
        )
    grounds = {name for name, item in design.nets.items() if item.role == "ground"}
    joined: dict[str, set[str]] = {}
    for pin in grounded.pins:
        joined.setdefault(pin.ref, set()).add(pin.net)
    capacitors = [
        c
        for c, nets in joined.items()
        if PASSIVES.get(design.components[c].lib_id, ("", ""))[0] == "C"
        and net in nets
        and len(nets) == 2
        and nets - {net} <= grounds
    ]
    if not capacitors:
        return RequirementResult(
            -1,
            kind,
            "fail",
            f"{ref} has no capacitor from {net} to ground, and its datasheet "
            "requires one",
            where=where,
        )
    if len(capacitors) > 1:
        return _unverified(
            -1,
            kind,
            f"{ref} has {len(capacitors)} output capacitors "
            f"({', '.join(capacitors)}); their combined ESR is not worked out",
            [f"{ref}: several output capacitors in parallel"],
            [
                "Simulate the loop with the regulator's own model, or leave one "
                "output capacitor for this check"
            ],
            where,
        )
    cap = capacitors[0]
    item = design.components[cap]
    missing: list[str] = []
    resolve: list[str] = []
    c_low: float | None = None
    try:
        marked = parse_quantity(item.value or "", "F", bare=True)
    except ValueError:
        marked = None
        missing.append(f"{cap}: no capacitance in its value field")
        resolve.append(f"Give {cap}'s value, for example 22uF")
    dielectric = item.params.get("dielectric")
    tolerance = part_spread(design, item, "tolerance")
    if marked is not None:
        nominal = _given(
            "Cnom",
            marked.value,
            "F",
            ref=cap,
            tolerance=tolerance.value if tolerance else None,
            source=tolerance.source if tolerance else "missing",
        )
        if not isinstance(dielectric, str):
            missing.append(f"{cap}: its kind (dielectric) is not given")
            resolve.append(
                f"Say what {cap} is (dielectric: X7R, C0G, tantalum, "
                "electrolytic, polymer)"
            )
        elif dielectric.upper() in TEMPERATURE_LOSS:
            mpn = item.params.get("mpn")
            curve = None
            if isinstance(mpn, str) and store is not None:
                try:
                    found = store.get(mpn)
                    curve = found.dc_bias if found else None
                except PartsError as exc:
                    missing.append(
                        f"{cap}: the part file for {mpn} cannot be used: {exc}"
                    )
            effective = mlcc_effective_capacitance(
                nominal,
                _rail_voltage(design, tree, net),
                dielectric=dielectric,
                curve=curve,
                ref=cap,
                mpn=mpn if isinstance(mpn, str) else None,
            )
            if effective.value is None or effective.accuracy.status != "verified":
                missing += list(effective.accuracy.unverified_because)
                resolve += list(effective.accuracy.to_resolve)
            else:
                c_low = effective.value.value
        elif dielectric.upper() in STABLE_KINDS | BIAS_FREE:
            c_low = least_capacitance(nominal)
            if c_low is None:
                missing.append(f"{cap}: no tolerance on its capacitance")
                resolve.append(f"Give {cap}'s tolerance")
        else:
            missing.append(f"{cap}: Kairo does not know the dielectric {dielectric}")
            resolve.append(
                f"Give {cap}'s dielectric as a class II code (X7R, X5R), C0G, or "
                "its kind (tantalum, electrolytic, polymer, film)"
            )
    esr_low, esr_high = item.quantity("esr_min"), item.quantity("esr_max")
    if esr_low is None or esr_high is None:
        missing.append(
            f"{cap}: its ESR range is not given"
            + (" (one value is not a range)" if item.quantity("esr") else "")
        )
        resolve.append(
            f"Give the lowest and highest ESR {cap} can have (esr_min, esr_max) "
            "from its datasheet"
        )
    c_min, need_low, need_high = (
        _given(name, value.value, value.unit, ref=ref, source=part.source)
        for name, value in region.items()
        if value is not None
    )
    asked = (
        f"{ref}'s datasheet requires at least {c_min.number * 1e6:g} uF with an "
        f"ESR from {need_low.number:g} to {need_high.number:g} ohm"
    )
    # What is known is judged even when something else is missing: one
    # value outside the region is a failure whatever the rest turns out to be.
    reasons = within_stable_region(
        c_low if c_low is not None else c_min.number,
        esr_low.value if esr_low is not None else need_low.number,
        esr_high.value if esr_high is not None else need_high.number,
        c_min=c_min,
        esr_min=need_low,
        esr_max=need_high,
        ref=ref,
    )
    if reasons:
        return RequirementResult(
            -1, kind, "fail", f"{cap}: {'; '.join(reasons)}. {asked}", where=where
        )
    if missing:
        return _unverified(
            -1,
            kind,
            f"{cap} cannot be shown to be inside {ref}'s stable region. {asked}",
            missing,
            resolve,
            where,
        )
    assert c_low is not None and esr_low is not None and esr_high is not None
    return RequirementResult(
        -1,
        kind,
        "pass",
        f"{cap} gives at least {c_low * 1e6:.3g} uF with an ESR from "
        f"{esr_low.value:g} to {esr_high.value:g} ohm. {asked}",
        where=where,
    )


def _switched_rail(
    index: int, requirement: RailVoltage, grounded: Grounded, ref: str
) -> RequirementResult:
    """A rail a switching regulator sets by its reference and feedback divider."""
    net = requirement.net
    where = Where(ref=ref, net=net)
    kind = requirement.type
    result, missing, resolve, divider = _switcher_output(grounded, ref, net)
    allowed_low = requirement.nominal.value * (1 - requirement.tolerance.value / 100)
    allowed_high = requirement.nominal.value * (1 + requirement.tolerance.value / 100)
    band = f"allowed is {allowed_low:.4g} V to {allowed_high:.4g} V"
    if result is None or result.value is None:
        return _unverified(
            index,
            kind,
            f"{net}'s voltage cannot be worked out from {ref}'s feedback divider",
            missing,
            resolve,
            where,
        )
    if result.case is None or missing:
        return RequirementResult(
            index,
            kind,
            "unverified",
            f"{net} is {result.value.value:.4g} V at nominal from {ref}'s divider "
            f"({divider}); its range is not established; {band}",
            tuple(missing),
            tuple(resolve),
            (result,),
            where,
        )
    low, high = result.case.minimum, result.case.maximum
    ok = allowed_low <= low and high <= allowed_high
    return RequirementResult(
        index,
        kind,
        "pass" if ok else "fail",
        f"{net} is {low:.4g} V to {high:.4g} V from {ref}'s reference and its "
        f"divider ({divider}), with the feedback pin's own current taken as "
        f"nothing, as the datasheet's formula takes it; {band}",
        results=(result,),
        where=where,
    )


def _adjustable(
    index: int, requirement: RailVoltage, grounded: Grounded, ref: str
) -> RequirementResult:
    """A rail set by an adjustable regulator's feedback divider.

    The divider is found from the connections: one resistor from the
    output net to a net that one of the regulator's other pins is on, and
    one resistor from that net to ground.
    """
    design = grounded.design
    net = requirement.net
    where = Where(ref=ref, net=net)
    kind = requirement.type
    part = design.components[ref]
    grounds = {name for name, item in design.nets.items() if item.role == "ground"}
    joined: dict[str, list[str]] = {}
    for pin in grounded.pins:
        joined.setdefault(pin.ref, []).append(pin.net)
    sense = {n for n in joined.get(ref, []) if n != net and n not in grounds}
    resistors = {
        r: nets
        for r, nets in joined.items()
        if PASSIVES.get(design.components[r].lib_id, ("", ""))[0] == "R"
        and len(nets) == 2
    }
    found: tuple[str, str] | None = None
    for middle in sorted(sense):
        upper = [r for r, nets in resistors.items() if set(nets) == {net, middle}]
        lower = [
            r
            for r, nets in resistors.items()
            if middle in nets
            and (set(nets) - {middle}) <= grounds
            and len(set(nets)) == 2
        ]
        if len(upper) == 1 and len(lower) == 1:
            found = (upper[0], lower[0])
            break
    if found is None:
        return _unverified(
            index,
            kind,
            f"{ref} is adjustable and its feedback divider was not found",
            [
                f"{ref}: no single resistor from {net} to its adjust pin with a "
                "single resistor from there to ground"
            ],
            [f"Connect {ref}'s divider, or state its output voltage (v_out)"],
            where,
        )
    upper_ref, lower_ref = found
    reference = part.quantity("v_ref")
    assert reference is not None
    spread = part_spread(design, part, "v_ref_tolerance")
    adjust = part.quantity("i_adj")
    missing: list[str] = []
    resolve: list[str] = []
    inputs: dict[str, Given] = {
        "v_ref": _given(
            "Vref",
            reference.value,
            "V",
            ref=ref,
            tolerance=spread.value if spread else None,
            source=spread.source if spread else "missing",
        )
    }
    for key, resistor in (("r1", upper_ref), ("r2", lower_ref)):
        ohms = resistance(design, resistor)
        item = design.components[resistor]
        tolerance = part_spread(design, item, "tolerance")
        if ohms is None:
            return _unverified(
                index,
                kind,
                f"{resistor}'s value is not a resistance",
                [f"{resistor}: no resistance in its value field"],
                [f"Give {resistor}'s value, for example 240"],
                where,
            )
        inputs[key] = _given(
            resistor,
            ohms.value,
            "ohm",
            ref=resistor,
            tolerance=tolerance.value if tolerance else None,
            source=tolerance.source if tolerance else "missing",
        )
    if adjust is None:
        missing.append(
            f"{ref}: no adjust-pin current; its term Iadj * {lower_ref} is left "
            "out, so the output is higher than worked out"
        )
        resolve.append(
            f"Give {ref}'s largest adjust-pin current (i_adj) from its datasheet"
        )
        inputs["i_adj"] = _given(
            "Iadj", 0.0, "A", ref=ref, tolerance=0.0, source="left out"
        )
    else:
        # The datasheet gives a largest adjust-pin current and no smallest.
        # The current flows out of the pin through the lower resistor, so
        # it is taken to lie between zero and the stated value: half the
        # value, give or take all of it.
        inputs["i_adj"] = _given(
            "Iadj",
            adjust.value / 2.0,
            "A",
            ref=ref,
            tolerance=100.0,
            source=f"{part.source} (zero to the stated {adjust.value * 1e6:g} uA)",
        )
    result = calculate("adjustable_regulator_output", inputs)
    more_missing, more_resolve = _from_result(result)
    missing += more_missing
    resolve += more_resolve
    allowed_low = requirement.nominal.value * (1 - requirement.tolerance.value / 100)
    allowed_high = requirement.nominal.value * (1 + requirement.tolerance.value / 100)
    band = f"allowed is {allowed_low:.4g} V to {allowed_high:.4g} V"
    assert result.value is not None
    if result.case is None or missing:
        return RequirementResult(
            index,
            kind,
            "unverified",
            f"{net} is {result.value.value:.4g} V at nominal from {ref}'s divider "
            f"({upper_ref}, {lower_ref}); its range is not established; {band}",
            tuple(missing),
            tuple(resolve),
            (result,),
            where,
        )
    low, high = result.case.minimum, result.case.maximum
    ok = allowed_low <= low and high <= allowed_high
    return RequirementResult(
        index,
        kind,
        "pass" if ok else "fail",
        f"{net} is {low:.4g} V to {high:.4g} V from {ref}'s reference and its "
        f"divider ({upper_ref}, {lower_ref}); {band}",
        results=(result,),
        where=where,
    )


def _divider(grounded: Grounded, ref: str, net: str) -> tuple[str, str] | None:
    """The feedback divider of an adjustable regulator, upper resistor first.

    One resistor from the output net to a net that one of the regulator's
    other pins is on, and one resistor from that net to ground.
    """
    design = grounded.design
    grounds = {name for name, item in design.nets.items() if item.role == "ground"}
    joined: dict[str, list[str]] = {}
    for pin in grounded.pins:
        joined.setdefault(pin.ref, []).append(pin.net)
    sense = {n for n in joined.get(ref, []) if n != net and n not in grounds}
    resistors = {
        r: nets
        for r, nets in joined.items()
        if PASSIVES.get(design.components[r].lib_id, ("", ""))[0] == "R"
        and len(nets) == 2
    }
    for middle in sorted(sense):
        upper = [r for r, nets in resistors.items() if set(nets) == {net, middle}]
        lower = [
            r
            for r, nets in resistors.items()
            if middle in nets
            and (set(nets) - {middle}) <= grounds
            and len(set(nets)) == 2
        ]
        if len(upper) == 1 and len(lower) == 1:
            return (upper[0], lower[0])
    return None


def _switcher_output(
    grounded: Grounded, ref: str, net: str
) -> tuple[CalcResult | None, list[str], list[str], str]:
    """A switching regulator's output from its reference and feedback divider.

    The feedback pin's own current is taken as nothing, as these parts'
    datasheets do in the formula they give (``Vout = Vref (1 + R2/R3)``,
    TPS54202 datasheet SLVSD26C equation 3, for one), and the result
    says so.

    Returns:
        The calculation, what is missing, what to supply, and the
        divider in words. The calculation is ``None`` if there is no
        divider or a resistor has no value.
    """
    design = grounded.design
    part = design.components[ref]
    found = _divider(grounded, ref, net)
    reference = part.quantity("v_ref")
    if found is None or reference is None:
        return (
            None,
            [f"{ref}: no feedback divider from {net} with a stated reference (v_ref)"],
            [f"Connect {ref}'s divider and give its reference voltage (v_ref)"],
            "",
        )
    spread = part_spread(design, part, "v_ref_tolerance")
    inputs: dict[str, Given] = {
        "v_ref": _given(
            "Vref",
            reference.value,
            "V",
            ref=ref,
            tolerance=spread.value if spread else None,
            source=spread.source if spread else "missing",
        )
    }
    for key, resistor in (("r_top", found[0]), ("r_bottom", found[1])):
        ohms = resistance(design, resistor)
        item = design.components[resistor]
        tolerance = part_spread(design, item, "tolerance")
        if ohms is None:
            return (
                None,
                [f"{resistor}: no resistance in its value field"],
                [f"Give {resistor}'s value, for example 100k"],
                "",
            )
        inputs[key] = _given(
            resistor,
            ohms.value,
            "ohm",
            ref=resistor,
            tolerance=tolerance.value if tolerance else None,
            source=tolerance.source if tolerance else "missing",
        )
    result = calculate("feedback_divider_output", inputs)
    missing, resolve = _from_result(result)
    return result, missing, resolve, f"{found[0]}, {found[1]}"


def _made_voltage(
    grounded: Grounded, tree: PowerTree, regulator: Regulator
) -> tuple[Given, list[str], list[str]]:
    """The voltage a regulator makes, with how far it may be from nominal.

    A stated output tolerance is used as it stands. Without one, a
    switching regulator's range is worked out from its reference and
    divider, and given as the wider of its two sides.
    """
    design = grounded.design
    net = regulator.output_net
    stated = _rail_voltage(design, tree, net)
    part = design.components[regulator.ref]
    if stated.tolerance is not None or part.quantity("v_ref") is None:
        return stated, [], []
    result, missing, resolve, _ = _switcher_output(grounded, regulator.ref, net)
    if result is None or result.value is None or result.case is None or missing:
        return stated, missing, resolve
    middle = result.value.value
    wider = max(result.case.maximum - middle, middle - result.case.minimum)
    return (
        _given(
            f"V({net})",
            middle,
            "V",
            ref=regulator.ref,
            origin=f"net {net}",
            tolerance=wider / middle * 100.0,
            source=f"worked out from {regulator.ref}'s reference and divider",
        ),
        [],
        [],
    )


@dataclass(frozen=True, slots=True)
class _Stage:
    """A buck stage's inductor ripple current, or why it is not known.

    Attributes:
        result: The ripple calculation, if every input was given.
        frequency: The switching frequency used, the datasheet's least.
        missing: What kept it from being established, a line each.
        resolve: What to supply, a line each.
    """

    result: CalcResult | None
    frequency: Given | None
    missing: tuple[str, ...]
    resolve: tuple[str, ...]

    @property
    def greatest(self) -> float | None:
        """The greatest ripple current over the tolerances, if established."""
        if self.result is None or self.result.case is None or self.missing:
            return None
        return self.result.case.maximum


def _buck_stage(grounded: Grounded, tree: PowerTree, regulator: Regulator) -> _Stage:
    """Work out a buck stage's inductor ripple current at its worst.

    The ripple is greatest at the highest input voltage, the lowest
    switching frequency and the lowest inductance, so the frequency must
    be the datasheet's minimum and the inductor must state a tolerance.
    """
    design = grounded.design
    ref = regulator.ref
    assert regulator.inductor is not None
    part = design.components[ref]
    coil = design.components[regulator.inductor]
    missing: list[str] = []
    resolve: list[str] = []
    try:
        henries = parse_quantity(coil.value or "", "H", bare=True)
    except ValueError:
        return _Stage(
            None,
            None,
            (f"{regulator.inductor}: no inductance in its value field",),
            (f"Give {regulator.inductor}'s value, for example 15uH",),
        )
    spread = part_spread(design, coil, "tolerance")
    rate = part.quantity("f_sw")
    if rate is None:
        return _Stage(
            None,
            None,
            (f"{ref}: no switching frequency",),
            (
                f"Give {ref}'s least switching frequency (f_sw) from its datasheet, "
                "and list it under limits as min",
            ),
        )
    is_least = part.limit_side("f_sw") == "min"
    if not is_least:
        missing.append(
            f"{ref}: its stated f_sw is not marked as a minimum, and the ripple is "
            "greatest at the lowest frequency"
        )
        resolve.append(
            f"Give {ref}'s least switching frequency from its datasheet as f_sw, "
            "listed under limits as min"
        )
    frequency = _given(
        "fsw",
        rate.value,
        "Hz",
        ref=ref,
        tolerance=0.0 if is_least else None,
        source=f"{part.source} (a limit, so no further tolerance)"
        if is_least
        else "missing",
    )
    made, more_missing, more_resolve = _made_voltage(grounded, tree, regulator)
    missing += more_missing
    resolve += more_resolve
    inputs = {
        "v_out": made,
        "v_in": _rail_voltage(design, tree, regulator.input_net),
        "f_sw": frequency,
        "l": _given(
            regulator.inductor,
            henries.value,
            "H",
            ref=regulator.inductor,
            tolerance=spread.value if spread else None,
            source=spread.source if spread else "missing",
        ),
    }
    result = calculate("buck_ripple_current", inputs)
    for line, cure in zip(*_from_result(result), strict=False):
        if line not in missing:
            missing.append(line)
            resolve.append(cure)
    return _Stage(result, frequency, tuple(missing), tuple(resolve))


def _stage_of(
    tree: PowerTree, *, inductor: str | None = None, net: str | None = None
) -> Regulator | None:
    """The switching regulator whose stage has this inductor or makes this net."""
    for regulator in tree.regulators:
        if regulator.kind != "switching" or regulator.inductor is None:
            continue
        if regulator.inductor == inductor or regulator.output_net == net:
            return regulator
    return None


def _peak_current(
    grounded: Grounded, tree: PowerTree, regulator: Regulator
) -> tuple[CalcResult | None, _Stage, list[str], list[str]]:
    """The inductor's greatest peak current: the load's most plus half the ripple."""
    stage = _buck_stage(grounded, tree, regulator)
    current = _load_current(grounded.design, tree, regulator.output_net, grounded)
    missing = [*stage.missing, *current.missing]
    resolve = [*stage.resolve, *current.resolve]
    if stage.greatest is None:
        return None, stage, missing, resolve
    result = calculate(
        "buck_peak_switch_current",
        {
            "ripple": _given(
                "dIL",
                stage.greatest,
                "A",
                origin="buck_ripple_current, at its greatest",
                tolerance=0.0,
                source="the greatest ripple current over the stated tolerances",
            ),
            "i_out": current.given,
        },
    )
    for line, cure in zip(*_from_result(result), strict=False):
        if line not in missing:
            missing.append(line)
            resolve.append(cure)
    return result, stage, missing, resolve


def _inductor_current(
    index: int, requirement: MaxInductorCurrent, grounded: Grounded, tree: PowerTree
) -> RequirementResult:
    design = grounded.design
    ref = requirement.component
    where = Where(ref=ref)
    kind = requirement.type
    regulator = _stage_of(tree, inductor=ref)
    if regulator is None:
        return _unverified(
            index,
            kind,
            f"{ref} is not the inductor of a step-down stage Kairo recognises",
            [
                f"{ref}: not an inductor from a switching regulator's switch pin "
                "to the rail it makes"
            ],
            [
                f"Connect {ref} from the regulator's switch pin to its output rail, "
                "and set the regulator's regulator parameter to switching"
            ],
            where,
        )
    rated = design.components[ref].quantity("i_sat")
    if rated is None:
        return _unverified(
            index,
            kind,
            f"{ref}'s peak current cannot be compared with a rating",
            [f"{ref}: no saturation current"],
            [f"Give {ref}'s saturation current (i_sat) from its datasheet"],
            where,
        )
    result, stage, missing, resolve = _peak_current(grounded, tree, regulator)
    results = tuple(r for r in (stage.result, result) if r is not None)
    if result is None or result.case is None or missing:
        return RequirementResult(
            index,
            kind,
            "unverified",
            f"{ref}'s greatest peak current is not established; it is rated to "
            f"{rated.value:g} A before it saturates",
            tuple(missing),
            tuple(resolve),
            results,
            where,
        )
    peak = result.case.maximum
    return RequirementResult(
        index,
        kind,
        "pass" if peak <= rated.value else "fail",
        f"{ref} carries at most {peak:.4g} A at its peak (the load's most plus "
        f"half of {stage.greatest:.4g} A of ripple); it saturates at "
        f"{rated.value:g} A",
        results=results,
        where=where,
    )


def _current_limit(
    regulator: Regulator, grounded: Grounded, tree: PowerTree
) -> RequirementResult:
    """A buck stage's peak current against the regulator's least current limit.

    A converter whose inductor current reaches its switch's current limit
    cannot deliver the load: the most it gives is its least limit less
    half the ripple (SLVA477C, equation 3).
    """
    ref = regulator.ref
    where = Where(ref=ref, net=regulator.output_net)
    kind = "switch_current_limit"
    part = grounded.design.components[ref]
    limit = part.quantity("i_limit")
    if limit is None or part.limit_side("i_limit") != "min":
        return _unverified(
            -1,
            kind,
            f"{ref}'s peak current cannot be compared with its current limit",
            [
                f"{ref}: no least current limit"
                if limit is None
                else f"{ref}: its stated i_limit is not marked as a minimum"
            ],
            [
                f"Give {ref}'s least switch current limit (i_limit) from its "
                "datasheet, listed under limits as min"
            ],
            where,
        )
    result, stage, missing, resolve = _peak_current(grounded, tree, regulator)
    results = tuple(r for r in (stage.result, result) if r is not None)
    if result is None or result.case is None or missing:
        return RequirementResult(
            -1,
            kind,
            "unverified",
            f"{ref}'s greatest peak current is not established; its current "
            f"limit is at least {limit.value:g} A",
            tuple(missing),
            tuple(resolve),
            results,
            where,
        )
    peak = result.case.maximum
    return RequirementResult(
        -1,
        kind,
        "pass" if peak <= limit.value else "fail",
        f"{ref}'s switch carries at most {peak:.4g} A at its peak; its current "
        f"limit is at least {limit.value:g} A",
        results=results,
        where=where,
    )


def _ripple(
    index: int,
    requirement: MaxRipple,
    grounded: Grounded,
    tree: PowerTree,
    store: PartStore | None,
) -> RequirementResult:
    design = grounded.design
    net = requirement.net
    where = Where(net=net)
    kind = requirement.type
    allowed = requirement.value.value
    regulator = _stage_of(tree, net=net)
    if regulator is None:
        return _unverified(
            index,
            kind,
            f"{net} is not made by a step-down stage Kairo recognises",
            [f"{net}: no switching regulator reaches it through an inductor"],
            [
                f"Simulate {net}'s ripple with the regulator's own model, or set "
                "the regulator's regulator parameter to switching"
            ],
            where,
        )
    grounds = {name for name, item in design.nets.items() if item.role == "ground"}
    joined: dict[str, set[str]] = {}
    for pin in grounded.pins:
        joined.setdefault(pin.ref, set()).add(pin.net)
    capacitors = [
        ref
        for ref, nets in joined.items()
        if PASSIVES.get(design.components[ref].lib_id, ("", ""))[0] == "C"
        and net in nets
        and len(nets) == 2
        and (nets - {net}) <= grounds
    ]
    if len(capacitors) != 1:
        return _unverified(
            index,
            kind,
            f"{net}'s ripple is worked out only for one output capacitor; it has "
            f"{len(capacitors)}",
            [
                f"{net}: {len(capacitors)} capacitors to ground, where the formula "
                "is for one capacitance with one ESR"
            ],
            [f"Simulate {net}'s ripple, or use one output capacitor"],
            where,
        )
    ref = capacitors[0]
    part = design.components[ref]
    stage = _buck_stage(grounded, tree, regulator)
    missing = list(stage.missing)
    resolve = list(stage.resolve)
    highest = part.quantity("esr_max")
    if highest is None and part.limit_side("esr") == "max":
        highest = part.quantity("esr")
    if highest is None:
        missing.append(f"{ref}: no greatest ESR")
        resolve.append(f"Give {ref}'s greatest ESR (esr_max) from its datasheet")
    least: float | None = None
    try:
        marked = parse_quantity(part.value or "", "F", bare=True)
    except ValueError:
        marked = None
        missing.append(f"{ref}: no capacitance in its value field")
        resolve.append(f"Give {ref}'s value, for example 47uF")
    extra: list[CalcResult] = []
    if marked is not None:
        tolerance = part_spread(design, part, "tolerance")
        nominal = _given(
            "Cnom",
            marked.value,
            "F",
            ref=ref,
            tolerance=tolerance.value if tolerance else None,
            source=tolerance.source if tolerance else "missing",
        )
        dielectric = part.params.get("dielectric")
        if isinstance(dielectric, str) and dielectric.upper() in STABLE_KINDS:
            least = least_capacitance(nominal)
            if least is None:
                missing.append(f"{ref}: no tolerance on its capacitance")
                resolve.append(f"Give {ref}'s tolerance")
        else:
            mpn = part.params.get("mpn")
            mpn = mpn if isinstance(mpn, str) else None
            curve = None
            if mpn and store is not None:
                try:
                    known = store.get(mpn)
                    curve = known.dc_bias if known else None
                except PartsError as exc:
                    missing.append(
                        f"{ref}: the part file for {mpn} cannot be used: {exc}"
                    )
                    resolve.append(f"Correct the part file for {mpn}")
            effective = mlcc_effective_capacitance(
                nominal,
                _rail_voltage(design, tree, net),
                dielectric=dielectric if isinstance(dielectric, str) else None,
                curve=curve,
                ref=ref,
                mpn=mpn,
            )
            extra.append(effective)
            if effective.value is None or effective.accuracy.status != "verified":
                missing += list(effective.accuracy.unverified_because)
                resolve += list(effective.accuracy.to_resolve)
            else:
                least = effective.value.value
    results = [r for r in (stage.result, *extra) if r is not None]
    if (
        missing
        or stage.greatest is None
        or stage.frequency is None
        or highest is None
        or least is None
    ):
        return RequirementResult(
            index,
            kind,
            "unverified",
            f"{net}'s ripple is not established; {allowed * 1000:g} mV is allowed",
            tuple(missing),
            tuple(resolve),
            tuple(results),
            where,
        )
    result = calculate(
        "buck_output_ripple",
        {
            "ripple": _given(
                "dIL",
                stage.greatest,
                "A",
                origin="buck_ripple_current, at its greatest",
                tolerance=0.0,
                source="the greatest ripple current over the stated tolerances",
            ),
            "esr": _given(
                "ESR",
                highest.value,
                "ohm",
                ref=ref,
                tolerance=0.0,
                source=f"{part.source} (a limit, so no further tolerance)",
            ),
            "f_sw": stage.frequency,
            "c": _given(
                "Cout",
                least,
                "F",
                ref=ref,
                tolerance=0.0,
                source=f"{ref}'s least capacitance in use",
            ),
        },
    )
    results.append(result)
    assert result.case is not None
    most = result.case.maximum
    return RequirementResult(
        index,
        kind,
        "pass" if most <= allowed else "fail",
        f"{net} ripples by at most {most * 1000:.3g} mV peak to peak "
        f"({stage.greatest:.4g} A of inductor ripple into {ref}'s "
        f"{least * 1e6:.3g} uF and {highest.value * 1000:g} mohm); "
        f"{allowed * 1000:g} mV is allowed. Spikes from the capacitor's and the "
        "layout's inductance are not in this figure",
        results=tuple(results),
        where=where,
    )
