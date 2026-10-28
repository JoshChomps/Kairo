"""A circuit to simulate, built from a design, and its text for each simulator.

Every connected pin of every part reaches its net through a 0 V source of
its own, named after the part and the pin. That gives three things at
once: the current into any pin can be read from any simulator in the same
way; the netlist's connections can be read back and compared with the
design's whatever model a part uses; and a part with no model is simply
left open behind its pins.

What stands in for each part is decided by its library symbol and its
stated parameters, never guessed: a part Kairo has no model for is listed
in `Circuit.unmodelled` and anything simulated without it is not a pass.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from kairo.calc.capacitor import BIAS_FREE, TEMPERATURE_LOSS
from kairo.defaults import net_spread, part_spread
from kairo.ir.models import Component, Design
from kairo.library.grounding import Grounded, GroundedPin
from kairo.library.kinds import DIODES, PASSIVES, TERMINAL_LIBRARIES
from kairo.parts.store import PartsError, PartStore
from kairo.power.tree import PowerTree, Regulator, build_tree, resistance
from kairo.spice.registry import GROUND_NODE, ModelEntry, ModelRegistry
from kairo.spice.request import SimRequest, Stimulus
from kairo.units import QuantityError, parse_quantity

Engine = Literal["ngspice", "xyce"]

#: The node every simulator takes as 0 V.
GROUND = "0"
#: The diode behind a fixed forward drop. An emission coefficient of 0.01
#: makes its own drop a few millivolts (0.01 * 25.9 mV * ln(I / 1 pA), which
#: is 5.4 mV at 1 mA and 6.5 mV at 100 mA), so the stated drop in series
#: with it sets the voltage, as the calculations assume.
DROP_MODEL = "KAIRO_DROP"
DROP_MODEL_LINE = f".model {DROP_MODEL} D(IS=1e-12 N=0.01)"
#: The width, in volts, over which the regulator model's two corners (0 V
#: and the start of dropout) are rounded; see `_soft_positive`.
KNEE = 1e-2
#: How long a step stays at its final level: far longer than any run.
_HOLD = "1e6"


class CircuitError(ValueError):
    """A design cannot be turned into a circuit as asked."""


@dataclass(frozen=True, slots=True)
class Element:
    """One line of a netlist.

    Attributes:
        name: Its name; the first letter is its SPICE kind.
        nodes: The nodes it joins, in SPICE order.
        value: A value, a source's waveform, or a model's name.
        expression: For a behavioural source (``B``), the voltage it sets.
        control: For a current-controlled source (``F``), the 0 V source
            whose current it follows.
    """

    name: str
    nodes: tuple[str, ...]
    value: str
    expression: str | None = None
    control: str | None = None


@dataclass(frozen=True, slots=True)
class ModelNote:
    """What stands in for one part.

    Attributes:
        ref: The part, or a label such as ``load 1`` or ``source VIN``.
        fidelity: Its class: ``primitive``, ``bias-adjusted``,
            ``behavioural``, ``terminal``, ``vendor`` or
            ``vendor-converted``.
        source: Where the model comes from.
        note: What it does and does not represent.
        sha256: For a registered model file, its hash.
        unproven: Why nothing this model produces counts as checked yet;
            empty when its results can count.
    """

    ref: str
    fidelity: str
    source: str
    note: str
    sha256: str = ""
    unproven: str = ""

    def as_dict(self) -> dict[str, str]:
        """The note as plain data."""
        out = {
            "ref": self.ref,
            "fidelity": self.fidelity,
            "source": self.source,
            "note": self.note,
        }
        if self.sha256:
            out["sha256"] = self.sha256
        return out


@dataclass(frozen=True, slots=True)
class Unmodelled:
    """A part left open because Kairo has no model for it.

    Attributes:
        ref: The part.
        why: What is missing.
        resolve: What to supply.
    """

    ref: str
    why: str
    resolve: str


@dataclass(frozen=True, slots=True)
class Circuit:
    """A design as a simulator sees it.

    Attributes:
        title: The netlist's first line.
        elements: Every element, in a fixed order.
        nodes: Each net's node name.
        pins: Each pin's 0 V source, as ``(ref, pin number)`` by name.
        models: What stands in for each part.
        unmodelled: The parts left open.
        sources: Each driven net's source element, by net.
        regulated: For each net a modelled regulator makes, the 0 V
            source at the regulator's output pin.
        uses_drop_model: True if a fixed-drop diode is present.
        includes: The registered model files the netlist reads.
        pspice: True if a model needs PSpice compatibility.
        engines: The simulators every model in the circuit runs on.
        xyce_includes: The files Xyce reads in their place, in the same
            order; a PSpice model is rewritten for it. Empty means the
            same files as `includes`.
    """

    title: str
    elements: tuple[Element, ...]
    nodes: dict[str, str]
    pins: dict[str, tuple[str, str]]
    models: tuple[ModelNote, ...]
    unmodelled: tuple[Unmodelled, ...]
    sources: dict[str, str]
    regulated: dict[str, str]
    uses_drop_model: bool
    includes: tuple[str, ...] = ()
    pspice: bool = False
    engines: tuple[str, ...] = ("ngspice", "xyce")
    xyce_includes: tuple[str, ...] = ()

    def pin_source(self, ref: str, number: str) -> str:
        """The 0 V source in series with one pin.

        Args:
            ref: The part.
            number: The pin's number.

        Returns:
            The source's name. Its current is the current flowing from
            the net into the pin.

        Raises:
            KeyError: If the pin is not connected in the design.
        """
        for name, pin in self.pins.items():
            if pin == (ref, number):
                return name
        raise KeyError(f"{ref}.{number}")


def _slug(text: str) -> str:
    """A name a simulator accepts, different for different inputs."""
    return "".join(
        c.lower() if c.isascii() and c.isalnum() else f"_{ord(c):x}_" for c in text
    )


def _number(value: float) -> str:
    """A number as netlist text, exact to the float."""
    return repr(float(value))


def build_circuit(
    grounded: Grounded,
    request: SimRequest,
    *,
    store: PartStore | None = None,
    registry: ModelRegistry | None = None,
) -> Circuit:
    """Turn a design into a circuit for one analysis.

    A part with a registered model uses it, whatever else Kairo could
    have put in its place.

    Args:
        grounded: The design with its pins resolved.
        request: The analysis, which decides how sources and loads are
            written.
        store: Datasheet facts, for capacitor DC-bias curves.
        registry: Registered model files, if a models folder is set.

    Returns:
        The circuit.

    Raises:
        CircuitError: If a stimulus names a net the design does not have,
            or the design has no ground net.
        RegistryError: If a registered model's file is missing or has
            changed since it was registered.
    """
    design = grounded.design
    grounds = [name for name, net in design.nets.items() if net.role == "ground"]
    if not grounds:
        raise CircuitError("the design has no ground net to measure voltages from")
    nodes: dict[str, str] = {}
    for name in design.nets:
        nodes[name] = GROUND if name == grounds[0] else f"n_{_slug(name)}"
    for stimulus in request.stimuli:
        if stimulus.net not in design.nets:
            raise CircuitError(f"stimulus net {stimulus.net!r} is not in the design")
        if nodes[stimulus.net] == GROUND:
            raise CircuitError(f"{stimulus.net} is the ground net; it takes no source")

    elements: list[Element] = []
    pins: dict[str, tuple[str, str]] = {}
    inner: dict[tuple[str, str], str] = {}
    by_ref: dict[str, list[GroundedPin]] = {}
    for pin in grounded.pins:
        by_ref.setdefault(pin.ref, []).append(pin)
        name = f"V_{_slug(pin.ref)}_{_slug(pin.pin.number)}"
        node = f"{_slug(pin.ref)}_{_slug(pin.pin.number)}"
        pins[name] = (pin.ref, pin.pin.number)
        inner[(pin.ref, pin.pin.number)] = node
        elements.append(Element(name, (nodes[pin.net], node), "0"))

    tree = build_tree(grounded)
    regulators = {r.ref: r for r in tree.regulators}
    notes: list[ModelNote] = []
    unmodelled: list[Unmodelled] = []
    uses_drop = False
    regulated: dict[str, str] = {}
    includes: list[str] = []
    xyce_includes: list[str] = []
    pspice = False
    engines = {"ngspice", "xyce"}
    for ref, part in design.components.items():
        connected = by_ref.get(ref, [])
        library = part.lib_id.split(":", 1)[0]
        number = part.params.get("mpn")
        entry = (
            registry.find(
                mpn=number if isinstance(number, str) else None, lib_id=part.lib_id
            )
            if registry is not None
            else None
        )
        if entry is not None and registry is not None:
            edits: tuple[str, ...] = ()
            xyce_path = registry.path(entry)
            if "xyce" in entry.engines:
                xyce_path, edits = registry.for_xyce(entry)
            made = _registered(ref, entry, connected, inner, grounds, edits)
            if not isinstance(made, Unmodelled):
                path = registry.path(entry).as_posix()
                if path not in includes:
                    includes.append(path)
                    xyce_includes.append(xyce_path.as_posix())
                pspice = pspice or entry.dialect == "pspice"
                engines &= set(entry.engines)
                if ref in regulators:
                    out_pin = next(
                        (
                            p
                            for p in connected
                            if p.net == regulators[ref].output_net
                            and p.pin.electrical_type == "power_out"
                        ),
                        None,
                    )
                    if out_pin is not None:
                        regulated[regulators[ref].output_net] = (
                            f"V_{_slug(ref)}_{_slug(out_pin.pin.number)}"
                        )
        elif ref in regulators:
            made = _regulator(
                design,
                regulators[ref],
                connected,
                inner,
                grounds,
                request.analysis.kind in ("op", "dc"),
            )
            if not isinstance(made, Unmodelled):
                regulated[regulators[ref].output_net] = made[0][1].control or ""
        elif part.lib_id in PASSIVES:
            made = _passive(grounded, ref, connected, inner, tree, store)
        elif part.lib_id in DIODES:
            made = _diode(design, ref, connected, inner)
            uses_drop = uses_drop or not isinstance(made, Unmodelled)
        elif library in TERMINAL_LIBRARIES:
            nothing: list[Element] = []
            made = (
                nothing,
                ModelNote(ref, "terminal", "kairo", "a place to connect; open"),
            )
        else:
            made = Unmodelled(
                ref,
                f"Kairo has no simulation model for {ref} ({part.lib_id})",
                f"Register a model for {part.lib_id}, or leave {ref} out of "
                "what the simulation is asked to show",
            )
        if isinstance(made, Unmodelled):
            unmodelled.append(made)
        else:
            elements.extend(made[0])
            notes.append(made[1])

    sources: dict[str, str] = {}
    driven = {s.net for s in request.stimuli}
    for net in sorted(driven & set(regulated)):
        raise CircuitError(
            f"{net} is made by {tree.rails[net].source}; a stimulus there would "
            "put two sources on one net. Drive the regulator's input instead"
        )
    for index, item in enumerate(unmodelled):
        made_by = [
            net
            for net, rail in tree.rails.items()
            if rail.source == item.ref and net not in driven
        ]
        if made_by:
            # Nothing else is put in the regulator's place: a source at
            # the nominal voltage would hide that the part is missing.
            unmodelled[index] = Unmodelled(
                item.ref,
                f"{item.why}, so nothing drives {', '.join(made_by)}",
                item.resolve,
            )
    for index, stimulus in enumerate(request.stimuli, start=1):
        made_elements, note = _source(stimulus, index, nodes[stimulus.net])
        elements.extend(made_elements)
        notes.append(note)
        sources[stimulus.net] = made_elements[0].name
    for net, rail in tree.rails.items():
        if rail.source is None and net not in driven:
            name = f"Vsrc_{_slug(net)}"
            elements.append(
                Element(name, (nodes[net], GROUND), f"DC {_number(rail.voltage)}")
            )
            sources[net] = name
            notes.append(
                ModelNote(
                    f"source {net}",
                    "primitive",
                    "kairo",
                    f"an ideal {rail.voltage:g} V source: the net's nominal "
                    "voltage, with no resistance or inductance",
                )
            )

    for index, load in enumerate(design.loads, start=1):
        if load.net not in nodes:
            continue
        node = nodes[load.net]
        rail = tree.rails.get(load.net)
        if request.analysis.kind == "tran" and rail is not None and rail.voltage > 0:
            # A constant current would keep flowing with the rail at 0 V,
            # which no real load does, so in time the load is the resistor
            # that draws the stated current at the rail's nominal voltage.
            ohms = rail.voltage / load.current.value
            elements.append(Element(f"Rload_{index}", (node, GROUND), _number(ohms)))
            notes.append(
                ModelNote(
                    f"load {index} on {load.net}",
                    "primitive",
                    "kairo",
                    f"a {ohms:.4g} ohm resistor ({rail.voltage:g} V / "
                    f"{load.current.value:g} A). The design gives the load's "
                    "current, not how it behaves below nominal voltage",
                )
            )
        else:
            elements.append(
                Element(
                    f"Iload_{index}",
                    (node, GROUND),
                    f"DC {_number(load.current.value)}",
                )
            )
            notes.append(
                ModelNote(
                    f"load {index} on {load.net}",
                    "primitive",
                    "kairo",
                    f"a constant {load.current.value:g} A, as the design states",
                )
            )
    _hold_floating(grounded, elements, nodes, unmodelled, notes)
    return Circuit(
        f"Kairo: {design.design}",
        tuple(elements),
        nodes,
        pins,
        tuple(notes),
        tuple(unmodelled),
        sources,
        regulated,
        uses_drop,
        tuple(includes),
        pspice,
        tuple(sorted(engines)),
        tuple(xyce_includes) if xyce_includes != includes else (),
    )


#: The element kinds that join their first two nodes at DC. A capacitor
#: and a current source do not; a controlled source joins only its
#: output pair; a subcircuit is taken to join all its nodes, since what
#: it holds is not read here.
_DC_PAIR = frozenset("RLVDBEH")


#: The resistance a net with no path to ground is held by: high enough to
#: draw nothing that shows, as SPICE's own ``rshunt`` option is used.
HOLD_OHMS = 1e12


def _hold_floating(
    grounded: Grounded,
    elements: list[Element],
    nodes: Mapping[str, str],
    unmodelled: Sequence[Unmodelled],
    notes: list[ModelNote],
) -> None:
    """Hold each net that nothing ties to ground at DC, and say so.

    Every simulator works out a steady state first, and a node with no
    path to ground there leaves the equations without a solution. Some
    runs then fail with a singular matrix and some settle by the
    simulator's own aids, differently in each. Holding the net through
    `HOLD_OHMS` gives every simulator the same solvable circuit, and the
    note makes every number of the run unverified, naming the net and
    the parts left open on it, since the held net's voltage means
    nothing.
    """
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        while parent.get(node, node) != node:
            node = parent[node]
        return node

    def join(a: str, b: str) -> None:
        parent[find(a)] = find(b)

    for element in elements:
        kind = element.name[0].upper()
        if kind == "X":
            for node in element.nodes[1:]:
                join(element.nodes[0], node)
        elif kind in _DC_PAIR and len(element.nodes) >= 2:
            join(element.nodes[0], element.nodes[1])
    earth = find(GROUND)
    open_parts = {u.ref for u in unmodelled}
    pieces: dict[str, list[str]] = {}
    for net, node in sorted(nodes.items()):
        if find(node) != earth:
            pieces.setdefault(find(node), []).append(net)
    for nets in pieces.values():
        # One hold per separate piece: the rest of the piece follows it.
        node = nodes[nets[0]]
        elements.append(
            Element(f"Rhold_{_slug(nets[0])}", (node, GROUND), _number(HOLD_OHMS))
        )
        here = sorted({p.ref for p in grounded.pins if p.net in nets} & open_parts)
        named = ", ".join(nets)
        left = (
            f": {', '.join(here)} on {'it' if len(nets) == 1 else 'them'} "
            f"{'has' if len(here) == 1 else 'have'} no model and "
            f"{'is' if len(here) == 1 else 'are'} left open"
            if here
            else ""
        )
        notes.append(
            ModelNote(
                f"net {named}",
                "primitive",
                "kairo",
                f"held to ground through {HOLD_OHMS:g} ohm at {nets[0]}, since "
                f"nothing in the circuit sets the voltage of {named}{left}",
                unproven=f"nothing in the circuit sets the voltage of {named}{left}; "
                "give a stimulus, or a model for the part that drives it",
            )
        )


def _source(
    stimulus: Stimulus, index: int, node: str
) -> tuple[list[Element], ModelNote]:
    """The source for a stimulus, with its series resistance and inductance."""
    if stimulus.kind == "ac":
        wave, what = "DC 0 AC 1", "a 1 V sweep input, so every result is a gain"
    elif stimulus.kind == "dc":
        assert stimulus.value is not None
        wave = f"DC {_number(stimulus.value.value)}"
        what = f"a steady {stimulus.value.value:g} V"
    elif stimulus.kind == "sine":
        assert stimulus.value is not None
        assert stimulus.amplitude is not None and stimulus.frequency is not None
        level, amplitude = stimulus.value.value, stimulus.amplitude.value
        wave = (
            f"SIN({_number(level)} {_number(amplitude)} "
            f"{_number(stimulus.frequency.value)})"
        )
        what = (
            f"{level:g} V with a {amplitude:g} V sine at "
            f"{stimulus.frequency.value:g} Hz on it"
        )
    else:
        assert stimulus.value is not None and stimulus.rise is not None
        start = stimulus.start.value if stimulus.start is not None else 0.0
        delay = stimulus.delay.value if stimulus.delay is not None else 0.0
        rise = _number(stimulus.rise.value)
        wave = (
            f"PULSE({_number(start)} {_number(stimulus.value.value)} "
            f"{_number(delay)} {rise} {rise} {_HOLD} 2e6)"
        )
        what = (
            f"a step from {start:g} V to {stimulus.value.value:g} V in "
            f"{stimulus.rise.value:g} s"
        )
    made: list[Element] = []
    here = f"src_{index}"
    made.append(Element(f"Vsrc_{index}", (here, GROUND), wave))
    series: list[str] = []
    if stimulus.resistance is not None:
        after = f"src_{index}_r"
        made.append(
            Element(f"Rsrc_{index}", (here, after), _number(stimulus.resistance.value))
        )
        here = after
        series.append(f"{stimulus.resistance.value:g} ohm")
    if stimulus.inductance is not None:
        after = f"src_{index}_l"
        made.append(
            Element(f"Lsrc_{index}", (here, after), _number(stimulus.inductance.value))
        )
        here = after
        series.append(f"{stimulus.inductance.value:g} H")
    # A 0 V link joins the source to the net, so the chain above needs no
    # special last element.
    made.append(Element(f"Vlink_{index}", (here, node), "0"))
    behind = f" behind {' and '.join(series)}" if series else ", with no impedance"
    return made, ModelNote(
        f"source {stimulus.net}", "primitive", "user-stated", what + behind
    )


def _registered(
    ref: str,
    entry: ModelEntry,
    connected: list[GroundedPin],
    inner: dict[tuple[str, str], str],
    grounds: list[str],
    xyce_edits: tuple[str, ...] = (),
) -> tuple[list[Element], ModelNote] | Unmodelled:
    """A part as an instance of its registered subcircuit.

    Each subcircuit node goes to the connected pin with the name the
    registry gives it. A pin name can appear more than once in a model
    (two ground nodes, say); each use goes to the same pin. The edits
    made for Xyce, if any, are named in the part's note.
    """
    by_name: dict[str, GroundedPin] = {}
    for pin in connected:
        by_name.setdefault(pin.pin.name, pin)
    nodes: list[str] = []
    for name in entry.nodes:
        if name == GROUND_NODE:
            nodes.append(GROUND)
            continue
        pin = by_name.get(name)
        if pin is None:
            return Unmodelled(
                ref,
                f"{ref}'s model ({entry.file}) needs its pin {name} connected",
                f"Connect {ref}.{name}, or correct the model's pin names in the "
                "registry",
            )
        nodes.append(inner[(ref, pin.pin.number)])
    del grounds
    unproven = ""
    if entry.fidelity == "vendor-converted" and not entry.conversion_checked:
        unproven = (
            f"{ref}'s model ({entry.manufacturer}, {entry.file}) was written for "
            "another simulator and has not been compared with it"
        )
    return (
        [Element(f"X_{_slug(ref)}", tuple(nodes), entry.subckt)],
        ModelNote(
            ref,
            entry.fidelity,
            entry.cite(),
            f"subcircuit {entry.subckt} from {entry.file}; the maker says it "
            f"covers: {entry.covers}"
            + (
                ". Xyce reads a copy changed only in spelling: " + "; ".join(xyce_edits)
                if xyce_edits
                else ""
            ),
            entry.sha256,
            unproven,
        ),
    )


def _passive(
    grounded: Grounded,
    ref: str,
    connected: list[GroundedPin],
    inner: dict[tuple[str, str], str],
    tree: PowerTree,
    store: PartStore | None,
) -> tuple[list[Element], ModelNote] | Unmodelled:
    """An ideal resistor, capacitor or inductor from the part's value."""
    design = grounded.design
    part = design.components[ref]
    letter, unit = PASSIVES[part.lib_id]
    if len(connected) != 2:
        return Unmodelled(
            ref,
            f"{ref} has {len(connected)} connected pins; a two-pin part needs both",
            f"Connect both pins of {ref}",
        )
    if letter == "R":
        quantity = resistance(design, ref)
    else:
        try:
            quantity = (
                parse_quantity(part.value, unit, bare=True) if part.value else None
            )
        except QuantityError:
            quantity = None
    if quantity is None or quantity.value <= 0:
        return Unmodelled(
            ref,
            f"{ref}'s value {part.value!r} is not a value in {unit}",
            f"Give {ref} a value such as 10k, 100nF or 4.7uH",
        )
    first, second = (inner[(ref, pin.pin.number)] for pin in connected)
    value = quantity.value
    fidelity, note = "primitive", f"an ideal {value:g} {unit}"
    elements: list[Element] = []
    if letter == "C":
        dielectric = part.params.get("dielectric")
        mpn = part.params.get("mpn")
        volts = _dc_across(design, tree, connected)
        fraction: float | None = None
        unusable = ""
        if store is not None and isinstance(mpn, str) and volts is not None:
            try:
                known = store.get(mpn)
                if known is not None and known.dc_bias is not None:
                    fraction = known.dc_bias.fraction_at(volts)
            except PartsError as exc:
                unusable = f" ({mpn}'s data cannot be used: {exc})"
        if isinstance(dielectric, str) and dielectric.upper() in BIAS_FREE:
            note += f"; {dielectric} does not lose capacitance with DC bias"
        elif fraction is not None and volts is not None:
            value *= fraction
            fidelity = "bias-adjusted"
            note = (
                f"{value:g} F: {fraction:.3g} of nominal at {volts:g} V from "
                f"{mpn}'s DC-bias curve (typical)"
            )
        else:
            note += (
                f"; nominal (no DC-bias data){unusable}, so in use it is likely less"
            )
        esr = part.quantity("esr")
        if esr is not None and esr.value > 0:
            middle = f"{_slug(ref)}_esr"
            elements.append(
                Element(f"Resr_{_slug(ref)}", (first, middle), _number(esr.value))
            )
            first = middle
            note += f"; in series with its stated ESR of {esr.value:g} ohm"
        else:
            note += "; no ESR stated, so none is modelled"
    elements.append(Element(f"{letter}_{_slug(ref)}", (first, second), _number(value)))
    return elements, ModelNote(ref, fidelity, "kairo", note)


def _dc_across(
    design: Design, tree: PowerTree, connected: list[GroundedPin]
) -> float | None:
    """The nominal DC voltage across a two-pin part, if both nets have one."""
    volts: list[float] = []
    for pin in connected:
        if design.nets[pin.net].role == "ground":
            volts.append(0.0)
        elif pin.net in tree.rails:
            volts.append(tree.rails[pin.net].voltage)
        else:
            return None
    return abs(volts[0] - volts[1])


def _diode(
    design: Design,
    ref: str,
    connected: list[GroundedPin],
    inner: dict[tuple[str, str], str],
) -> tuple[list[Element], ModelNote] | Unmodelled:
    """A fixed forward drop: an almost ideal diode in series with ``v_f``."""
    forward = design.components[ref].quantity("v_f")
    if forward is None:
        return Unmodelled(
            ref,
            f"{ref}'s forward voltage is not given",
            f"Give {ref}'s forward voltage (v_f) from its datasheet",
        )
    by_name = {pin.pin.name.upper(): pin for pin in connected}
    if len(connected) != 2 or set(by_name) != {"A", "K"}:
        return Unmodelled(
            ref,
            f"{ref}'s connected pins are not one named A and one named K",
            f"Connect {ref}'s anode and cathode",
        )
    anode = inner[(ref, by_name["A"].pin.number)]
    cathode = inner[(ref, by_name["K"].pin.number)]
    middle = f"{_slug(ref)}_drop"
    return (
        [
            Element(f"D_{_slug(ref)}", (anode, middle), DROP_MODEL),
            Element(f"Vf_{_slug(ref)}", (middle, cathode), _number(forward.value)),
        ],
        ModelNote(
            ref,
            "behavioural",
            "kairo:fixed_drop",
            f"conducts above its stated forward voltage of {forward.value:g} V "
            "and drops that at any current; a real diode's drop rises with "
            "current, so currents near turn-on are not represented",
        ),
    )


def _regulator(
    design: Design,
    regulator: Regulator,
    connected: list[GroundedPin],
    inner: dict[tuple[str, str], str],
    grounds: list[str],
    steady: bool,
) -> tuple[list[Element], ModelNote] | Unmodelled:
    """A linear regulator as a source limited by its dropout.

    The output is the nominal voltage, or the input less the dropout when
    that is lower, and never below zero. For the steady state (``steady``)
    that is written exactly; for a run in time or frequency both corners
    are rounded over `KNEE`, because a sharp corner stops the simulators'
    step control. The input draws what the output
    supplies, plus the quiescent current when it is stated. Current limit,
    start-up, loop response and supply rejection are not represented.
    """
    ref = regulator.ref
    part = design.components[ref]
    if regulator.kind != "linear":
        return Unmodelled(
            ref,
            f"Kairo has no simulation model for a {regulator.kind or 'regulator'} "
            f"that is not marked linear ({ref})",
            f"Register a model for {ref}; a switching regulator needs its maker's",
        )
    dropout = part.quantity("v_dropout")
    if dropout is None:
        return Unmodelled(
            ref,
            f"{ref}'s dropout voltage is not given",
            f"Give {ref}'s dropout voltage (v_dropout) from its datasheet",
        )
    stated = part.quantity("v_out")
    net_voltage = design.nets[regulator.output_net].voltage
    assert net_voltage is not None
    nominal = stated.value if stated is not None else net_voltage.value

    def first(net: str, kind: str | None) -> GroundedPin | None:
        return next(
            (
                p
                for p in connected
                if p.net == net and (kind is None or p.pin.electrical_type == kind)
            ),
            None,
        )

    pin_in = first(regulator.input_net, "power_in")
    pin_out = first(regulator.output_net, "power_out")
    pin_ground = next((p for p in connected if p.net in grounds), None)
    if pin_in is None or pin_out is None or pin_ground is None:
        return Unmodelled(
            ref,
            f"{ref} needs an input, an output and a ground pin connected",
            f"Connect {ref}'s input, output and ground",
        )
    node_in = inner[(ref, pin_in.pin.number)]
    node_out = inner[(ref, pin_out.pin.number)]
    node_ground = inner[(ref, pin_ground.pin.number)]
    slug = _slug(ref)
    sense = f"V_{slug}_{_slug(pin_out.pin.number)}"
    headroom = f"(V({node_in},{node_ground}) - {_number(dropout.value)})"
    if steady:
        expression = f"max(0, min({_number(nominal)}, {headroom}))"
    else:
        # min(nominal, available) with its corner rounded the same way,
        # written so that no two nearly equal numbers are subtracted.
        available = _soft_positive(headroom)
        gap = f"({_number(nominal)} - {available})"
        expression = (
            f"0.5 * ({_number(nominal)} + {available} - "
            f"sqrt({gap} * {gap} + {KNEE!r} * {KNEE!r}))"
        )
    elements = [
        Element(f"B_{slug}", (node_out, node_ground), "", expression=expression),
        # The pin's 0 V source carries current from the net into the pin,
        # which is negative while the regulator supplies, so a gain of -1
        # draws the same current in at the input.
        Element(f"F_{slug}", (node_in, node_ground), "-1", control=sense),
    ]
    quiescent = part.quantity("i_q")
    extra = "its quiescent current is not given, so none is drawn"
    if quiescent is not None:
        elements.append(
            Element(
                f"Iq_{slug}", (node_in, node_ground), f"DC {_number(quiescent.value)}"
            )
        )
        extra = f"plus its stated quiescent current of {quiescent.value:g} A"
    return elements, ModelNote(
        ref,
        "behavioural",
        "kairo:ldo_macro",
        f"holds {nominal:g} V, or the input less its stated dropout of "
        f"{dropout.value:g} V when that is lower; the input draws the output "
        f"current, {extra}. Current limit, start-up, loop response and supply "
        "rejection are not represented",
    )


def _soft_positive(x: str) -> str:
    """An expression for ``max(0, x)`` with its corner rounded.

    ``(x + sqrt(x^2 + w^2)) / 2`` differs from ``max(0, x)`` by at most
    ``w / 2``, at the corner, and by about ``w^2 / (4 |x|)`` away from it.
    A sharp corner stops both simulators' time-step control. Measured on
    the 3.3 V regulator example with a 100 us supply edge: at 1 mV Xyce
    7.10 gives up at the corner, at 3 mV it takes 1.8 million steps, and
    at 10 mV 190 thousand.
    """
    return f"(0.5 * ({x} + sqrt({x} * {x} + {KNEE!r} * {KNEE!r})))"


@dataclass(frozen=True, slots=True)
class Varied:
    """A value in a circuit that has a tolerance, or should have one.

    Attributes:
        element: The netlist element that carries the value.
        label: What it is, for a reader: ``R1`` or ``the source on VIN``.
        tolerance: Its tolerance in percent, or ``None`` if the design
            does not give one.
        resolve: What to supply when the tolerance is missing.
        lacks: What is missing when ``tolerance`` is ``None``, in words
            that follow "which has".
    """

    element: str
    label: str
    tolerance: float | None
    resolve: str
    lacks: str = "no tolerance"


#: A temperature coefficient as designs write one: 100ppm/degC, with or
#: without a plus-or-minus sign, per degree Celsius or per kelvin.
_TEMPCO = re.compile(
    r"\s*(?:±|\+/-|\+-)?\s*(\d+(?:\.\d+)?)\s*ppm\s*/\s*(?:degC|°C|K|C)\s*"
)
#: A class I ceramic's temperature coefficient: EIA-198 gives C0G (NP0)
#: as 0 plus or minus 30 ppm per degree Celsius.
C0G_PPM = 30.0


def thermal_widening(part: Component, letter: str, away: float) -> float | None:
    """How far a part's value may move with temperature, in percent.

    A stated ``tempco`` is a limit either way, as datasheets give one
    (plus or minus so many ppm per degree), so over a difference in
    temperature it widens the tolerance by that much. A ceramic
    capacitor with a dielectric code and no ``tempco`` is widened by
    the code's limit from EIA-198: 30 ppm per degree for C0G and NP0,
    and the class II codes' whole band (15% for X7R) at any
    temperature, since the code bounds the change over its range and
    says nothing of where in the range it happens.

    Args:
        part: The part.
        letter: ``R``, ``C`` or ``L``.
        away: How far the temperature is from the nominal, in degrees.

    Returns:
        The widening in percent, or ``None`` if the part states
        nothing it can be worked out from.
    """
    stated = part.params.get("tempco")
    if isinstance(stated, str):
        found = _TEMPCO.fullmatch(stated)
        if found is not None:
            return float(found.group(1)) * away * 1e-4
        return None
    if letter != "C":
        return None
    code = part.params.get("dielectric")
    if not isinstance(code, str):
        return None
    if code.upper() in BIAS_FREE:
        return C0G_PPM * away * 1e-4
    loss = TEMPERATURE_LOSS.get(code.upper())
    return None if loss is None else loss * 100.0


def _esr_spread(part: Component) -> float | None:
    """The spread about a stated ESR, in percent, that holds its range.

    ``None`` if no range is stated, or if the spread would reach 100%,
    which would take the low end through zero: an ESR stated at the
    range's middle always gives less.
    """
    esr, low, high = (part.quantity(n) for n in ("esr", "esr_min", "esr_max"))
    if esr is None or low is None or high is None or esr.value <= 0:
        return None
    spread = 100.0 * max(high.value - esr.value, esr.value - low.value, 0.0)
    spread /= esr.value
    return spread if spread < 100.0 else None


def varied_values(
    grounded: Grounded, circuit: Circuit, away: float = 0.0
) -> list[Varied]:
    """Every value in a circuit that a tolerance applies to.

    These are the resistors, capacitors and inductors, a capacitor's
    stated ESR, the forward voltage of each fixed-drop diode, and each
    source. An ESR varies over its stated range (``esr_min`` to
    ``esr_max``), by the least spread about ``esr`` that holds the whole
    range; Kairo has no default for one, so with no range stated a limit
    the ESR moves is left unverified. A stated load and anything inside
    a registered model are not varied.

    Args:
        grounded: The design.
        circuit: Its circuit.
        away: How far the run's temperature is from the nominal, in
            degrees. Above zero, each part's tolerance is widened by
            what it states of its change with temperature, and a part
            that states nothing has no tolerance there.

    Returns:
        The values, in the circuit's order.
    """
    design = grounded.design
    names = {element.name for element in circuit.elements}
    out: list[Varied] = []
    for ref, part in design.components.items():
        kind = PASSIVES.get(part.lib_id)
        slug = _slug(ref)
        if kind is not None and f"{kind[0]}_{slug}" in names:
            stated = part_spread(design, part, "tolerance")
            widening = thermal_widening(part, kind[0], away) if away else 0.0
            if stated is not None and widening is None:
                out.append(
                    Varied(
                        f"{kind[0]}_{slug}",
                        ref,
                        None,
                        f"Give {ref}'s temperature coefficient (tempco, such "
                        "as 100ppm/degC)"
                        + (
                            " or its dielectric code (dielectric)"
                            if kind[0] == "C"
                            else ""
                        ),
                        "no stated change with temperature",
                    )
                )
                continue
            out.append(
                Varied(
                    f"{kind[0]}_{slug}",
                    ref,
                    stated.value + (widening or 0.0) if stated is not None else None,
                    f"Give {ref}'s tolerance",
                )
            )
            if f"Resr_{slug}" in names:
                # ESR moves a lot with temperature and age, so it is varied
                # only over a range the part states, never by a spread
                # Kairo would have to guess.
                out.append(
                    Varied(
                        f"Resr_{slug}",
                        f"{ref}'s ESR",
                        None if away else _esr_spread(part),
                        f"Give the lowest and highest ESR {ref} can have "
                        "(esr_min, esr_max) from its datasheet, and esr at "
                        "their middle",
                        "no stated range"
                        if not away
                        else "no stated change with temperature",
                    )
                )
        elif f"Vf_{slug}" in names:
            stated = part_spread(design, part, "v_f_tolerance")
            if away and stated is not None:
                # A forward voltage falls as the junction warms, and no
                # parameter states by how much for this part.
                out.append(
                    Varied(
                        f"Vf_{slug}",
                        f"{ref}'s forward voltage",
                        None,
                        f"Give {ref}'s forward voltage at that temperature "
                        "in a design of its own; a design has no parameter "
                        "for its change with temperature yet",
                        "no stated change with temperature",
                    )
                )
                continue
            out.append(
                Varied(
                    f"Vf_{slug}",
                    f"{ref}'s forward voltage",
                    stated.value if stated is not None else None,
                    f"Give the tolerance of {ref}'s forward voltage (v_f_tolerance)",
                )
            )
    for net, source in circuit.sources.items():
        stated = net_spread(design, net)
        out.append(
            Varied(
                source,
                f"the source on {net}",
                stated.value if stated is not None else None,
                f"Give {net}'s tolerance (tolerance on the net)",
            )
        )
    return out


def scaled(circuit: Circuit, factors: dict[str, float]) -> Circuit:
    """The same circuit with some values multiplied.

    Args:
        circuit: The circuit.
        factors: The multiplier for each element named, as `Varied`
            names them. A source's levels are all multiplied.

    Returns:
        The circuit with those elements' values changed.

    Raises:
        CircuitError: If a named element is not in the circuit or does
            not carry a plain value.
    """
    if not factors:
        return circuit
    left = dict(factors)
    elements: list[Element] = []
    for element in circuit.elements:
        factor = left.pop(element.name, None)
        if factor is None:
            elements.append(element)
            continue
        value = element.value
        try:
            if value.startswith("DC "):
                changed = f"DC {_number(float(value[3:]) * factor)}"
            elif value.startswith(("PULSE(", "SIN(")):
                # A step's two levels, or a sine's level and amplitude.
                head, fields = (
                    value.split("(", 1)[0],
                    value.split("(", 1)[1][:-1].split(),
                )
                fields[0] = _number(float(fields[0]) * factor)
                fields[1] = _number(float(fields[1]) * factor)
                changed = f"{head}({' '.join(fields)})"
            else:
                changed = _number(float(value) * factor)
        except ValueError as exc:
            raise CircuitError(f"{element.name} has no plain value to vary") from exc
        elements.append(
            Element(
                element.name,
                element.nodes,
                changed,
                element.expression,
                element.control,
            )
        )
    if left:
        raise CircuitError(f"not in the circuit: {', '.join(sorted(left))}")
    return dataclasses.replace(circuit, elements=tuple(elements))


def _line(element: Element, engine: Engine) -> str:
    """One element as a netlist line."""
    nodes = " ".join(element.nodes)
    if element.expression is not None:
        text = element.expression
        # Xyce reads an expression only inside braces; ngspice reads it bare.
        setting = f"V={{{text}}}" if engine == "xyce" else f"V = {text}"
        return f"{element.name} {nodes} {setting}"
    if element.control is not None:
        return f"{element.name} {nodes} {element.control} {element.value}"
    return f"{element.name} {nodes} {element.value}"


def netlist(circuit: Circuit, engine: Engine, commands: list[str]) -> str:
    """The circuit as a netlist for one simulator.

    Args:
        circuit: The circuit.
        engine: Whose dialect to write.
        commands: The analysis and option lines, already in that dialect.

    Returns:
        The netlist text. Comment lines record each net's node and each
        pin's source, so the text can be read back with `connections`.
    """
    lines = [circuit.title]
    lines += [f"* net {node} {net}" for net, node in circuit.nodes.items()]
    lines += [
        f"* pin {name} {ref} {number}" for name, (ref, number) in circuit.pins.items()
    ]
    files = (
        circuit.xyce_includes
        if engine == "xyce" and circuit.xyce_includes
        else circuit.includes
    )
    lines += [f'.include "{path}"' for path in files]
    lines += [_line(element, engine) for element in circuit.elements]
    if circuit.uses_drop_model:
        lines.append(DROP_MODEL_LINE)
    lines += commands
    lines.append(".end")
    return "\n".join(lines) + "\n"


_NET = re.compile(r"^\* net (\S+) (.+)$")
_PIN = re.compile(r"^\* pin (\S+) (\S+) (.*)$")


def connections(text: str) -> dict[str, set[tuple[str, str]]]:
    """Read a netlist's connections back: the pins on each net.

    The names come from the comment lines `netlist` writes; which node
    each pin is on comes from the element lines themselves, so a writer
    that put a pin on the wrong node is caught.

    Args:
        text: A netlist written by `netlist`.

    Returns:
        ``(ref, pin number)`` pairs by net name, for every net with a pin.

    Raises:
        CircuitError: If a pin's source is missing or is on an unnamed node.
    """
    nets: dict[str, str] = {}
    pins: dict[str, tuple[str, str]] = {}
    lines = text.splitlines()
    for line in lines:
        net = _NET.match(line)
        if net is not None:
            nets[net[1]] = net[2]
        pin = _PIN.match(line)
        if pin is not None:
            pins[pin[1]] = (pin[2], pin[3])
    found: dict[str, set[tuple[str, str]]] = {}
    seen: set[str] = set()
    for line in lines[1:]:
        fields = line.split()
        if len(fields) >= 3 and fields[0] in pins:
            if fields[1] not in nets:
                raise CircuitError(f"{fields[0]} is on {fields[1]}, which is not a net")
            found.setdefault(nets[fields[1]], set()).add(pins[fields[0]])
            seen.add(fields[0])
    missing = sorted(set(pins) - seen)
    if missing:
        raise CircuitError(f"the netlist has no source for {', '.join(missing)}")
    return found
