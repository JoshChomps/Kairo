"""How much current each supply rail carries, and what each regulator does with it.

The tree is read from the grounded design. A regulator is a part with a
supply input pin on one power net and a supply output pin on another. A
rail's current is the sum of:

- the loads the design states for it;
- each resistor from the rail to ground, by Ohm's law;
- each resistor from the rail through a diode with a stated forward
  voltage to ground (an indicator LED), when nothing else is on the net
  between them;
- each part's stated supply current (``i_supply``);
- the input current of each regulator fed from it: output current plus
  quiescent current for a linear regulator, and output power over
  efficiency and input voltage for a switching one.

What cannot be worked out is never left out silently. A part on the rail
whose current is not known makes the rail's total a lower bound and is
named, with what to supply.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kairo.calc.catalogue import CATALOGUE
from kairo.ir import Design
from kairo.library.grounding import Grounded
from kairo.library.kinds import PASSIVES, TERMINAL_LIBRARIES
from kairo.units import Quantity, QuantityError, parse_quantity


@dataclass(frozen=True, slots=True)
class Draw:
    """One contribution to a rail's current.

    Attributes:
        what: What draws it, such as ``load: MCU budget`` or ``R1 and D1``.
        current: How much, in A.
        refs: The parts involved.
        kind: ``load`` for a stated load, ``resistor`` for a resistor to
            ground, ``indicator`` for a resistor through a diode to
            ground, ``divider`` for two resistors in series to ground
            with only input pins between them, ``supply`` for a part's
            stated supply current,
            ``quiescent`` for a regulator's own current, ``passed_on``
            for what a linear regulator draws to feed its output rail,
            ``switched`` for what a switching regulator draws to feed
            its output rail, and ``other`` for the rest.
        through: For ``passed_on`` and ``switched``, the rail the current
            is passed on to.
    """

    what: str
    current: float
    refs: tuple[str, ...] = ()
    kind: str = "other"
    through: str = ""


@dataclass(frozen=True, slots=True)
class Unknown:
    """A part on a rail whose current could not be worked out.

    Attributes:
        ref: The part.
        why: What is not known.
        resolve: What to supply.
    """

    ref: str
    why: str
    resolve: str


@dataclass(slots=True)
class Rail:
    """A power net and what it carries.

    Attributes:
        net: The net's name.
        voltage: Its nominal voltage, in V.
        draws: Each known contribution.
        unknown: Parts whose current is not known; while there are any,
            the total is a lower bound.
        source: The regulator that makes this rail, if one does.
    """

    net: str
    voltage: float
    draws: list[Draw] = field(default_factory=list[Draw])
    unknown: list[Unknown] = field(default_factory=list[Unknown])
    source: str | None = None

    @property
    def current(self) -> float:
        """The sum of the known contributions, in A."""
        return sum(draw.current for draw in self.draws)

    @property
    def is_bound(self) -> bool:
        """True if the total is a lower bound, because something is unknown."""
        return bool(self.unknown)


@dataclass(frozen=True, slots=True)
class Regulator:
    """A regulator in the tree.

    Attributes:
        ref: Its reference.
        kind: ``linear`` or ``switching``, or ``None`` if the design does
            not say.
        input_net: The rail it is fed from.
        output_net: The rail it makes.
        inductor: For a switching regulator whose switch pin reaches
            its output rail through an inductor, that inductor.
        switch_net: The net its switch pin is on, in that case.
    """

    ref: str
    kind: str | None
    input_net: str
    output_net: str
    inductor: str | None = None
    switch_net: str | None = None


@dataclass(frozen=True, slots=True)
class PowerTree:
    """Every rail and regulator of a design.

    Attributes:
        rails: Each power net by name.
        regulators: The regulators, in the order their inputs are fed.
    """

    rails: dict[str, Rail]
    regulators: tuple[Regulator, ...]

    def regulator(self, ref: str) -> Regulator | None:
        """The regulator with a reference, if it is one.

        Args:
            ref: The part's reference.

        Returns:
            The regulator, or ``None``.
        """
        return next((r for r in self.regulators if r.ref == ref), None)


def resistance(design: Design, ref: str) -> Quantity | None:
    """A resistor's value from its ``value`` field.

    Args:
        design: The design.
        ref: The part's reference.

    Returns:
        The resistance, or ``None`` if the part's symbol is not a
        resistor's, or the value is absent or is not a resistance such
        as ``1k`` or ``4.7kohm``.
    """
    part = design.components[ref]
    # A value with no unit says nothing of what the part is: "100n" on a
    # capacitor would read as a hundred nanoohms.
    if PASSIVES.get(part.lib_id, ("", ""))[0] != "R":
        return None
    value = part.value
    if value is None:
        return None
    try:
        return parse_quantity(value, "ohm", bare=True)
    except QuantityError:
        return None


def build_tree(grounded: Grounded) -> PowerTree:
    """Work out every rail's current.

    Args:
        grounded: The design with its pins resolved.

    Returns:
        The tree. Rails whose total is incomplete say what is missing.
    """
    design = grounded.design
    grounds = {name for name, net in design.nets.items() if net.role == "ground"}
    rails = {
        name: Rail(name, net.voltage.value)
        for name, net in design.nets.items()
        if net.role == "power" and net.voltage is not None
    }
    nets_of: dict[str, dict[str, str]] = {}
    types_of: dict[str, dict[str, str]] = {}
    pins_on: dict[str, int] = {}
    for pin in grounded.pins:
        pins_on[pin.net] = pins_on.get(pin.net, 0) + 1
        nets_of.setdefault(pin.ref, {})[pin.pin.number] = pin.net
        types_of.setdefault(pin.ref, {})[pin.pin.number] = pin.pin.electrical_type

    regulators: list[Regulator] = []
    for ref in design.components:
        inputs = {
            nets_of[ref][n]
            for n, kind in types_of.get(ref, {}).items()
            if kind == "power_in" and nets_of[ref][n] in rails
        }
        outputs = {
            nets_of[ref][n]
            for n, kind in types_of.get(ref, {}).items()
            if kind == "power_out" and nets_of[ref][n] in rails
        }
        kind = design.components[ref].params.get("regulator")
        inductor: str | None = None
        switch_net: str | None = None
        if kind == "switching" and len(inputs) == 1 and not outputs:
            # A step-down converter drives a switch node, not its rail:
            # the rail is on the far side of one inductor. Libraries type
            # the switch pin either way (KiCad's TPS54202 power output, its
            # TPS54360 output); the inductor is what marks the stage.
            stages = [
                (other, switched, far)
                for n, pin_kind in types_of.get(ref, {}).items()
                if pin_kind in ("power_out", "output")
                for switched in [nets_of[ref][n]]
                for other, joined in nets_of.items()
                if PASSIVES.get(design.components[other].lib_id, ("", ""))[0] == "L"
                and len(joined) == 2
                and switched in joined.values()
                for far in set(joined.values()) - {switched}
                if far in rails and far not in inputs
            ]
            if len(stages) == 1:
                inductor, switch_net, far = stages[0]
                outputs = {far}
        if len(inputs) == 1 and len(outputs) == 1 and inputs != outputs:
            regulators.append(
                Regulator(
                    ref,
                    kind if isinstance(kind, str) else None,
                    next(iter(inputs)),
                    next(iter(outputs)),
                    inductor,
                    switch_net,
                )
            )
    regulator_refs = {r.ref for r in regulators}
    for regulator in regulators:
        rails[regulator.output_net].source = regulator.ref

    for load in design.loads:
        if load.net in rails:
            rails[load.net].draws.append(
                Draw(
                    f"load: {load.note or 'stated in the design'}",
                    load.current.value,
                    kind="load",
                )
            )

    # Parts with two pins, by the two nets they join.
    two_pin = {
        ref: tuple(pins.values())
        for ref, pins in nets_of.items()
        if len(pins) == 2
        and ref not in regulator_refs
        # A stage's inductor carries the regulator's own output, which
        # the loads on the rail already account for.
        and ref not in {r.inductor for r in regulators}
    }
    types_on: dict[str, list[tuple[str, str]]] = {}
    for pin in grounded.pins:
        types_on.setdefault(pin.net, []).append((pin.ref, pin.pin.electrical_type))
    counted: set[str] = set()
    for ref, (first, second) in two_pin.items():
        ohms = resistance(design, ref)
        if ohms is None or ohms.value <= 0:
            _other_two_pin(design, ref, (first, second), types_of[ref], rails, counted)
            continue
        for rail_net, other in ((first, second), (second, first)):
            if rail_net not in rails:
                continue
            rail = rails[rail_net]
            if other in grounds:
                rail.draws.append(
                    Draw(
                        f"{ref} to ground",
                        _through(rail.voltage, 0.0, ohms.value),
                        (ref,),
                        "resistor",
                    )
                )
                counted.add(ref)
            elif other not in rails:
                # Any third pin on the net between them could carry
                # current of its own, so the pair is solved only alone.
                diode = (
                    _diode_to_ground(design, two_pin, other, grounds, ref)
                    if pins_on[other] == 2
                    else None
                )
                if diode is not None:
                    forward = design.components[diode].quantity("v_f")
                    assert forward is not None
                    if rail.voltage > forward.value:
                        rail.draws.append(
                            Draw(
                                f"{ref} and {diode}",
                                _through(rail.voltage, forward.value, ohms.value),
                                (ref, diode),
                                "indicator",
                            )
                        )
                        counted.update((ref, diode))
                elif (
                    lower := _lower_of_divider(
                        design, two_pin, types_on, other, grounds, ref
                    )
                ) is not None:
                    below = resistance(design, lower)
                    assert below is not None
                    rail.draws.append(
                        Draw(
                            f"{ref} and {lower} to ground",
                            float(
                                CATALOGUE["series_resistors_current"].formula(
                                    {
                                        "v": rail.voltage,
                                        "r1": ohms.value,
                                        "r2": below.value,
                                    }
                                )
                            ),
                            (ref, lower),
                            "divider",
                        )
                    )
                    counted.update((ref, lower))
                else:
                    # A pull-up, a filter, a loaded divider: what flows
                    # depends on what else is on the far net, which this
                    # tree does not solve. Leaving it out silently would
                    # make the rail's total look complete.
                    rail.unknown.append(_unsolved(ref, rail_net, other))
            else:
                rail.unknown.append(_unsolved(ref, rail_net, other))

    for ref, part in design.components.items():
        if ref in regulator_refs or ref in counted:
            continue
        supply = part.quantity("i_supply")
        fed_from = {
            nets_of[ref][n]
            for n, kind in types_of.get(ref, {}).items()
            if kind == "power_in" and nets_of[ref][n] in rails
        }
        for net in sorted(fed_from):
            if supply is not None:
                rails[net].draws.append(
                    Draw(f"{ref} supply", supply.value, (ref,), "supply")
                )
            else:
                rails[net].unknown.append(
                    Unknown(
                        ref,
                        f"{ref} is supplied from {net} and its supply current "
                        "is not given",
                        f"Give {ref}'s supply current (i_supply) from its datasheet",
                    )
                )

    # A regulator's input current depends on its output rail's total, so
    # rails are settled from the loads back toward the source.
    ordered = _from_loads_back(regulators)
    for regulator in ordered:
        out, feed = rails[regulator.output_net], rails[regulator.input_net]
        part = design.components[regulator.ref]
        for item in out.unknown:
            feed.unknown.append(item)
        if regulator.kind == "linear":
            quiescent = part.quantity("i_q")
            feed.draws.append(
                Draw(
                    f"{regulator.ref} output current",
                    out.current,
                    (regulator.ref,),
                    "passed_on",
                    regulator.output_net,
                )
            )
            if quiescent is not None:
                feed.draws.append(
                    Draw(
                        f"{regulator.ref} quiescent",
                        quiescent.value,
                        (regulator.ref,),
                        "quiescent",
                    )
                )
            else:
                feed.unknown.append(
                    Unknown(
                        regulator.ref,
                        f"{regulator.ref}'s quiescent current is not given",
                        f"Give {regulator.ref}'s quiescent current (i_q) from its "
                        "datasheet, with the page",
                    )
                )
        elif regulator.kind == "switching":
            efficiency = part.quantity("efficiency")
            if efficiency is not None and efficiency.value > 0:
                feed.draws.append(
                    Draw(
                        f"{regulator.ref} input current",
                        float(
                            CATALOGUE["switcher_input_current"].formula(
                                {
                                    "v_out": out.voltage,
                                    "i_out": out.current,
                                    "efficiency": efficiency.value / 100.0,
                                    "v_in": feed.voltage,
                                }
                            )
                        ),
                        (regulator.ref,),
                        "switched",
                        regulator.output_net,
                    )
                )
            else:
                feed.unknown.append(
                    Unknown(
                        regulator.ref,
                        f"{regulator.ref}'s efficiency is not given",
                        f"Give {regulator.ref}'s efficiency at this load from "
                        "its datasheet",
                    )
                )
        else:
            feed.unknown.append(
                Unknown(
                    regulator.ref,
                    f"{regulator.ref} is not marked linear or switching",
                    f"Set {regulator.ref}'s regulator parameter to linear or switching",
                )
            )
    return PowerTree(rails, tuple(ordered))


def _lower_of_divider(
    design: Design,
    two_pin: dict[str, tuple[str, ...]],
    types_on: dict[str, list[tuple[str, str]]],
    middle: str,
    grounds: set[str],
    upper: str,
) -> str | None:
    """The resistor that completes a divider to ground, if that is all there is.

    The net between the two resistors may also hold input pins, such as
    a regulator's feedback pin, which are taken to draw nothing: that
    is what makes the pair a divider and not a loaded one. Any other
    pin there leaves the current unknown.
    """
    others = [(ref, kind) for ref, kind in types_on.get(middle, []) if ref != upper]
    lowers = [
        ref
        for ref, _ in others
        if ref in two_pin
        and resistance(design, ref) is not None
        and set(two_pin[ref]) - {middle} <= grounds
        and len(set(two_pin[ref])) == 2
    ]
    if len(lowers) != 1:
        return None
    rest = [kind for ref, kind in others if ref != lowers[0]]
    if any(kind != "input" for kind in rest):
        return None
    return lowers[0]


def _other_two_pin(
    design: Design,
    ref: str,
    nets: tuple[str, ...],
    types: dict[str, str],
    rails: dict[str, Rail],
    counted: set[str],
) -> None:
    """Account for a two-pin part on a rail that is not a solvable resistor.

    A capacitor passes no steady current and a connector is where current
    enters or leaves, which the loads state. Anything else (a bead, a
    fuse, a diode, a symbol Kairo does not know) may carry current, so it
    is counted at its stated ``i_supply`` or named as unknown. A part
    with a supply pin is left to the supply-current pass.
    """
    part = design.components[ref]
    kind = PASSIVES.get(part.lib_id, ("", ""))[0]
    library = part.lib_id.partition(":")[0]
    if kind == "C" or library in TERMINAL_LIBRARIES or "power_in" in types.values():
        return
    first, second = nets
    for rail_net, other in ((first, second), (second, first)):
        if rail_net not in rails or first == second:
            continue
        supply = part.quantity("i_supply")
        if supply is not None:
            rails[rail_net].draws.append(
                Draw(f"{ref} current", supply.value, (ref,), "supply")
            )
        else:
            rails[rail_net].unknown.append(
                Unknown(
                    ref,
                    f"{ref} ({part.lib_id}) joins {rail_net} to {other}, and the "
                    "current through it is not known",
                    f"Give the current {ref} carries from {rail_net} as its "
                    "i_supply (0A if none), or run simulate with an op analysis",
                )
            )
        counted.add(ref)


def _unsolved(ref: str, rail: str, other: str) -> Unknown:
    """A resistor from a rail to a net whose voltage the tree does not solve."""
    return Unknown(
        ref,
        f"{ref} joins {rail} to {other}, and the current through it is not worked out",
        f"Run simulate with an op analysis for the current through {ref}",
    )


def _through(supply: float, drop: float, ohms: float) -> float:
    """The current through a resistor from a supply, after a fixed drop."""
    return float(
        CATALOGUE["led_current"].formula({"v_supply": supply, "v_f": drop, "r": ohms})
    )


def _diode_to_ground(
    design: Design,
    two_pin: dict[str, tuple[str, ...]],
    net: str,
    grounds: set[str],
    resistor: str,
) -> str | None:
    """Find the diode a resistor feeds, if the net between them has only it.

    The diode must have a stated forward voltage and its other pin on
    ground.
    """
    on_net = [ref for ref, nets in two_pin.items() if net in nets and ref != resistor]
    if len(on_net) != 1:
        return None
    ref = on_net[0]
    others = [n for n in two_pin[ref] if n != net]
    if len(others) != 1 or others[0] not in grounds:
        return None
    return ref if design.components[ref].quantity("v_f") is not None else None


def _from_loads_back(regulators: list[Regulator]) -> list[Regulator]:
    """Regulators ordered so each comes after those it feeds."""
    remaining = list(regulators)
    ordered: list[Regulator] = []
    while remaining:
        ready = [
            r
            for r in remaining
            if not any(other.input_net == r.output_net for other in remaining)
        ]
        if not ready:
            # A loop of regulators feeding each other; take them as given.
            ready = remaining[:1]
        for regulator in ready:
            ordered.append(regulator)
            remaining.remove(regulator)
    return ordered
