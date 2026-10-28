"""How far each net's voltage can go, for checks of pins' absolute maximum ratings.

An absolute maximum rating is a limit a pin must never pass, in a
transient as much as at rest. So a net is checked with the widest range
it can reach: its stated range and surges, a rail's range over its
tolerance, or, for a net the design states nothing about, what the parts
around it allow.

The last is worked out only where it can be shown. A group of nets
joined by resistors and diodes, where every other pin on them is a chip's
input and every capacitor goes to ground, stays between the lowest and
highest of the nets that feed it. By Kirchhoff's current law a node of a
network of positive conductances sits at a weighted mean of its
neighbours, so no node of the group is above its highest feeder or below
its lowest; a diode conducts only from its higher end to its lower, which
keeps that true. A Zener, TVS or plain diode between a net and a known
net then holds it within its clamping or forward voltage of that net,
provided it carries the current, which the result says. Anything else on
the group can drive it anywhere: a chip's output or supply pin, a
connector, an inductor, or a capacitor to a net that moves (a step on
that net is passed straight through). The group's range is then left
unknown, naming the part.

A net that leaves the board through a connector can carry whatever the
outside puts on it, so its ends are known for this check only if the
design states its surges (``surge_max``, ``surge_min``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from kairo.calc import Given
from kairo.defaults import part_spread
from kairo.ir import Component, Design
from kairo.library.grounding import Grounded, GroundedPin
from kairo.library.kinds import DIODES, PASSIVES

#: Symbols that clamp, by kind: ``diode`` conducts from A to K above its
#: forward voltage, ``zener`` also from K to A above its clamping voltage,
#: and ``tvs`` (two-way, pins A1 and A2) either way above it.
CLAMPS: dict[str, str] = {
    **dict.fromkeys(DIODES, "diode"),
    "Device:D_Zener": "zener",
    "Device:D_Zener_Small": "zener",
    "Device:D_TVS": "tvs",
    "Device:D_TVS_Small": "tvs",
}
#: Libraries whose parts take a net off the board.
_OFF_BOARD = frozenset({"Connector", "Connector_Generic"})
#: Libraries whose parts connect nothing beyond a pad to probe.
_PADS = frozenset({"TestPoint"})
#: Symbol names of pads to probe. KiCad keeps its own in the Connector
#: library (Connector:TestPoint), which would otherwise read as off the board.
_PAD_NAMES = ("TestPoint",)


@dataclass(frozen=True, slots=True)
class End:
    """One end of a net's range.

    Attributes:
        value: The voltage, or ``None`` if it is not known.
        reached: True if the net is shown to reach it (a stated value, a
            rail's corner, or a single feeder it follows); False if it is
            only a bound the net stays within.
        basis: How it is known, or why not.
    """

    value: float | None
    reached: bool
    basis: str


@dataclass(frozen=True, slots=True)
class Reach:
    """The range a net can reach.

    Attributes:
        low: Its lowest end.
        high: Its highest end.
    """

    low: End
    high: End


@dataclass(frozen=True, slots=True)
class Clamp:
    """A clamping part between two nets.

    Attributes:
        ref: The part.
        kind: ``diode``, ``zener`` or ``tvs``.
        anode: The net on its anode (A1 for a TVS).
        cathode: The net on its cathode (A2 for a TVS).
        forward: Its greatest forward voltage, if stated.
        clamp: Its greatest clamping voltage, if stated.
    """

    ref: str
    kind: str
    anode: str
    cathode: str
    forward: float | None
    clamp: float | None

    def above(self, net: str) -> tuple[float | None, str]:
        """How far above the other net this part lets ``net`` go."""
        if self.kind == "tvs" or (self.kind == "zener" and net == self.cathode):
            return self.clamp, f"{self.ref}'s clamping voltage"
        if net == self.anode:
            return self.forward, f"{self.ref}'s forward voltage"
        return None, ""

    def below(self, net: str) -> tuple[float | None, str]:
        """How far below the other net this part lets ``net`` go."""
        if self.kind == "tvs" or (self.kind == "zener" and net == self.anode):
            return self.clamp, f"{self.ref}'s clamping voltage"
        if net == self.cathode:
            return self.forward, f"{self.ref}'s forward voltage"
        return None, ""

    def other(self, net: str) -> str:
        """The net on the part's other side."""
        return self.cathode if net == self.anode else self.anode


def followers(grounded: Grounded) -> dict[str, tuple[str, str]]:
    """Nets held at another net's voltage through one resistor that carries nothing.

    A net whose only other connections are chips' inputs, capacitors to
    ground and test pads sits at the far end of its one resistor while no
    current flows, and an input draws none at rest. So a clamp on the far
    net holds this one too, as a gate Zener before a gate resistor holds
    the gate. A current into an input, such as a gate being charged,
    moves it by that current times the resistance; the bound is for rest.

    Returns:
        Each such net, with the net it follows and the resistor between.
    """
    design = grounded.design
    on_net = _pins_by_net(grounded)
    pins = _pins_by_part(grounded)
    found: dict[str, tuple[str, str]] = {}
    for net, on in on_net.items():
        through: list[tuple[str, str]] = []
        sound = True
        for pin in on:
            part = design.components[pin.ref]
            others = [p.net for p in pins[pin.ref] if p.net != net]
            if _is_pad(design, pin.ref):
                continue
            if len(pins[pin.ref]) == 2 and _kind(part) == "R" and others:
                through.append((others[0], pin.ref))
            elif len(pins[pin.ref]) == 2 and _kind(part) == "C" and others:
                sound = sound and design.nets[others[0]].role == "ground"
            elif pin.pin.electrical_type != "input" or _leaves(design, pin.ref):
                sound = False
        if sound and len(through) == 1:
            found[net] = through[0]
    return found


def clamps_of(grounded: Grounded) -> list[Clamp]:
    """Every clamping part with its two nets and stated voltages."""
    design = grounded.design
    pins = _pins_by_part(grounded)
    found: list[Clamp] = []
    for ref, part in design.components.items():
        on = pins.get(ref, [])
        kind = CLAMPS.get(part.lib_id) or _diode_by_pins(part, on)
        if kind is None or len(on) != 2:
            continue
        names = {p.pin.name: p.net for p in on}
        anode = names.get("A1" if kind == "tvs" else "A")
        cathode = names.get("A2" if kind == "tvs" else "K")
        if anode is None or cathode is None or anode == cathode:
            continue
        found.append(
            Clamp(ref, kind, anode, cathode, _forward(design, part), _clamp(part))
        )
    return found


def _diode_by_pins(part: Component, on: list[GroundedPin]) -> str | None:
    """A diode drawn with a symbol of its own, known by its connected pins.

    KiCad's Diode library draws each part under its own name (BAT54W,
    1N4148W), with pins named A and K; any not-connected pin of its
    package is not on a net. Such a part with a stated clamping voltage
    is taken as a Zener, otherwise as a plain diode.
    """
    if sorted(p.pin.name for p in on) != ["A", "K"]:
        return None
    return "zener" if part.params.get("v_clamp") is not None else "diode"


def net_reach(
    grounded: Grounded, rails: Mapping[str, Given | None]
) -> dict[str, Reach]:
    """The range every connected net can reach.

    Args:
        grounded: The design with its pins on their nets.
        rails: Each power net's voltage with its tolerance, as the power
            tree gives it; ``None`` or a missing tolerance leaves the
            rail's range unknown.

    Returns:
        Each net's reach. Nothing is left out: a net whose range cannot
        be shown has ends of ``None``, each saying why.
    """
    design = grounded.design
    on_net = _pins_by_net(grounded)
    pins = _pins_by_part(grounded)
    leaves = {
        net: next((p.ref for p in on if _leaves(design, p.ref)), None)
        for net, on in on_net.items()
    }
    reach: dict[str, Reach] = {}
    open_nets: list[str] = []
    for net in on_net:
        stated = _stated(design, net, rails.get(net), leaves[net])
        if stated is None:
            open_nets.append(net)
        else:
            reach[net] = stated

    clamps = clamps_of(grounded)
    groups = _groups(design, pins, open_nets)
    for group in groups:
        for net, found in _group_reach(design, group, on_net, pins, reach).items():
            reach[net] = found
    for clamp in clamps:
        for net in (clamp.anode, clamp.cathode):
            if net in open_nets:
                reach[net] = _tightened(reach[net], net, clamp, reach)
    return reach


def _stated(
    design: Design, net: str, rail: Given | None, off_board: str | None
) -> Reach | None:
    """A net's reach from what the design states, or ``None`` to work it out."""
    given = design.nets.get(net)
    if given is None:
        return None
    if given.role == "ground":
        zero = End(0.0, True, "ground")
        return Reach(zero, zero)
    if given.min is not None and given.max is not None:
        low = End(given.min.value, True, f"{net}'s stated min")
        high = End(given.max.value, True, f"{net}'s stated max")
    elif given.role == "power":
        if rail is None or rail.tolerance is None:
            gone = End(None, False, f"{net}'s tolerance is not known")
            return Reach(gone, gone)
        spread = rail.tolerance / 100.0
        ends = sorted((rail.number * (1 - spread), rail.number * (1 + spread)))
        where = f"{net} at {rail.number:g} V +/-{rail.tolerance:.3g}%"
        low = End(ends[0], True, where)
        high = End(ends[1], True, where)
    else:
        return None
    if given.surge_max is not None:
        high = End(given.surge_max.value, True, f"{net}'s stated surge_max")
    elif off_board is not None:
        high = End(
            None, False, f"{net} leaves the board through {off_board}, no surge_max"
        )
    if given.surge_min is not None:
        low = End(given.surge_min.value, True, f"{net}'s stated surge_min")
    elif off_board is not None:
        low = End(
            None, False, f"{net} leaves the board through {off_board}, no surge_min"
        )
    return Reach(low, high)


def _groups(
    design: Design, pins: Mapping[str, list[GroundedPin]], open_nets: list[str]
) -> list[list[str]]:
    """Nets with no stated range, joined where a resistor or diode links two."""
    parent = {net: net for net in open_nets}

    def root(net: str) -> str:
        while parent[net] != net:
            parent[net] = parent[parent[net]]
            net = parent[net]
        return net

    for ref, part in design.components.items():
        on = pins.get(ref, [])
        if len(on) != 2 or not _conducts(part, on):
            continue
        a, b = on[0].net, on[1].net
        if a in parent and b in parent:
            parent[root(a)] = root(b)
    grouped: dict[str, list[str]] = {}
    for net in open_nets:
        grouped.setdefault(root(net), []).append(net)
    return list(grouped.values())


def _group_reach(
    design: Design,
    group: list[str],
    on_net: Mapping[str, list[GroundedPin]],
    pins: Mapping[str, list[GroundedPin]],
    known: Mapping[str, Reach],
) -> dict[str, Reach]:
    """The reach shared by a group of nets, from the known nets that feed it."""
    members = set(group)
    blockers: list[str] = []
    feeders: dict[str, str] = {}
    through_resistors: set[str] = set()
    for net in group:
        for pin in on_net[net]:
            part = design.components[pin.ref]
            on = pins[pin.ref]
            others = [p.net for p in on if p.net != net]
            if _is_pad(design, pin.ref):
                continue
            if _leaves(design, pin.ref):
                blockers.append(f"{net} leaves the board through {pin.ref}")
            elif len(on) == 2 and _conducts(part, on):
                outside = [n for n in others if n not in members]
                for other in outside:
                    feeders[other] = pin.ref
                    if _kind(part) == "R":
                        through_resistors.add(other)
            elif len(on) == 2 and _kind(part) == "C":
                if any(design.nets[n].role != "ground" for n in others):
                    blockers.append(
                        f"{pin.ref} couples {net} to {others[0]}, which can move"
                    )
            elif len(on) == 2 and _kind(part) == "L":
                blockers.append(f"{pin.ref} is an inductor on {net}")
            elif pin.pin.electrical_type != "input":
                name = pin.pin.name or pin.pin.number
                blockers.append(
                    f"{pin.ref}.{name} ({pin.pin.electrical_type}) can drive {net}"
                )
    if blockers:
        why = f"{blockers[0]}; state its range (min and max)"
        gone = End(None, False, why)
        return dict.fromkeys(group, Reach(gone, gone))
    if not feeders:
        gone = End(None, False, "nothing on it sets its voltage")
        return dict.fromkeys(group, Reach(gone, gone))
    # A group fed through one resistor and nothing else that conducts at
    # rest follows that net, so it reaches that net's ends; with several
    # feeders it lies between them and its ends are only bounds.
    follows = next(iter(through_resistors)) if len(through_resistors) == 1 else None
    low = _outer([(known[n].low, n) for n in feeders], follows, highest=False)
    high = _outer([(known[n].high, n) for n in feeders], follows, highest=True)
    return dict.fromkeys(group, Reach(low, high))


def _outer(ends: list[tuple[End, str]], follows: str | None, *, highest: bool) -> End:
    """The outermost of the feeders' ends, or unknown if any is."""
    for end, net in ends:
        if end.value is None:
            return End(None, False, f"fed from {net}: {end.basis}")
    known = [(end.value, end, net) for end, net in ends if end.value is not None]
    pick = max if highest else min
    value, end, net = pick(known, key=lambda item: item[0])
    if net == follows:
        return End(value, end.reached, f"follows {net} ({end.basis})")
    return End(value, False, f"no further than {net} ({end.basis})")


def _tightened(
    reach: Reach, net: str, clamp: Clamp, known: Mapping[str, Reach]
) -> Reach:
    """A net's reach with one clamp to a known net taken into account."""
    other = known.get(clamp.other(net))
    if other is None:
        return reach
    low, high = reach.low, reach.high
    rise, why_rise = clamp.above(net)
    if rise is not None and other.high.value is not None:
        cap = other.high.value + rise
        if high.value is None or cap < high.value:
            reached = high.value is not None and high.reached
            high = End(cap, reached, f"held by {why_rise} above {clamp.other(net)}")
    fall, why_fall = clamp.below(net)
    if fall is not None and other.low.value is not None:
        floor = other.low.value - fall
        if low.value is None or floor > low.value:
            reached = low.value is not None and low.reached
            low = End(floor, reached, f"held by {why_fall} below {clamp.other(net)}")
    return Reach(low, high)


def _conducts(part: Component, on: list[GroundedPin]) -> bool:
    """Whether a two-pin part joins its nets for the bound: a resistor or diode."""
    return (
        _kind(part) == "R"
        or part.lib_id in CLAMPS
        or _diode_by_pins(part, on) is not None
    )


def _kind(part: Component) -> str:
    return PASSIVES.get(part.lib_id, ("", ""))[0]


def _forward(design: Design, part: Component) -> float | None:
    """A diode's greatest forward voltage: its stated one plus its spread."""
    v_f = part.quantity("v_f")
    if v_f is None:
        return None
    spread = part_spread(design, part, "v_f_tolerance")
    if spread is None:
        return None
    return v_f.value * (1 + spread.value / 100.0)


def _clamp(part: Component) -> float | None:
    """A Zener's or TVS's greatest clamping voltage, as stated."""
    v_clamp = part.quantity("v_clamp")
    return None if v_clamp is None else v_clamp.value


def _is_pad(design: Design, ref: str) -> bool:
    """Whether a part is a pad to probe, connecting nothing."""
    library, _, name = design.components[ref].lib_id.partition(":")
    return library in _PADS or name.startswith(_PAD_NAMES)


def _leaves(design: Design, ref: str) -> bool:
    """Whether a part takes its nets off the board: a connector, not a pad."""
    return _library(design, ref) in _OFF_BOARD and not _is_pad(design, ref)


def _library(design: Design, ref: str) -> str:
    return design.components[ref].lib_id.partition(":")[0]


def _pins_by_net(grounded: Grounded) -> dict[str, list[GroundedPin]]:
    on_net: dict[str, list[GroundedPin]] = {}
    for pin in grounded.pins:
        on_net.setdefault(pin.net, []).append(pin)
    return on_net


def _pins_by_part(grounded: Grounded) -> dict[str, list[GroundedPin]]:
    pins: dict[str, list[GroundedPin]] = {}
    for pin in grounded.pins:
        pins.setdefault(pin.ref, []).append(pin)
    return pins
