"""Kairo's own reading of a design: what is wrong, and what could be better.

These rules look only at what a netlist holds: each pin's net and
electrical type, each part's symbol, and the roles the design gives its
nets. So they read a design Kairo built and a schematic a person drew
alike, and say nothing about how either is drawn.

A remark is one of two kinds, and neither blocks a write:

- a **fault** is something that does not work as drawn, whoever drew it:
  an input on a net with nothing else, a rail that feeds chips with no
  capacitor on it and no part that could be one, two UART transmit
  outputs wired together or two receive inputs with nothing sending;
- a **suggestion** is where the design may well be sound and Kairo would
  do it another way: one decoupling capacitor for several supply pins,
  an open output or an I2C line with no pull-up drawn, an LED's symbol
  straight across a rail, an outside-facing data line with no
  protection drawn, a supply from a connector with no larger
  capacitor where it comes in, a reset or enable input with nothing
  drawn to set it, and on a design whose policy marks it a prototype,
  supply and ground rails with no test point, regulated rails with no
  indicator LED, regulators that feed their loads with no link to open,
  and a chip's spare I/O left open.

A rule that cannot be sure stays silent or suggests; it never calls a
fault on a guess. The rules were first written with more faults, then
run on the twelve real boards in ``tests/fixtures/real``, which showed
how a sound design can look like a fault: boards draw capacitors and
resistors with symbols of their own, an open output often sinks an LED
or relies on a pull-up inside the chip it drives, and one board draws a
protection diode with the LED symbol. So a part Kairo does not recognise
is taken as possibly the cure, a line that leaves through a connector
may have its pull-up elsewhere, and what was a fault on a guess is a
suggestion. Rules that need to know which nets are supplies are not run
until the design states net roles, and the review says how many were
left out.

Sources, cited by each rule:

- NXP UM10204, "I2C-bus specification and user manual", section 3.1.1:
  SDA and SCL are "connected to a positive supply voltage via a
  current-source or pull-up resistor".
- Texas Instruments SCBA004, "Implications of Slow or Floating CMOS
  Inputs": an input left open drifts into the region where both of its
  transistors conduct.
- Analog Devices MT-101, "Decoupling Techniques": a local capacitor at
  each supply pin, as close to it as can be.
- IEC 61000-4-2, the electrostatic discharge immunity test that a port
  a person can touch is held to.
- Texas Instruments TPS54202 datasheet SLVSD26C, equation 3: an
  adjustable regulator's output is ``Vref (1 + R2 / R3)``, so with no
  divider (R2 of nothing) it is the reference itself.
- Infineon IPT015N10N5 datasheet Rev 2.4, p5 (reverse diode): a power
  MOSFET carries current from source to drain through its body diode
  whatever its gate does, so two in series back to back block both ways
  only while nothing else conducts around them; Texas Instruments
  BQ76952 datasheet SLUSE13B, Figure 16-1, puts its predischarge FET
  across the discharge FETs alone for that reason.
- Kirchhoff's current law: a current divides between parallel paths, so
  a sense resistor measures only its own share.
- Analog Devices MT-101, "Decoupling Techniques": low-frequency noise
  needs larger electrolytic capacitors, which act as a charge reservoir
  for transient currents, beside the small ceramic ones at each pin.
  What counts as larger (`_BULK_FARADS`) is Kairo's own line (A205).

The UART and prototype rules (`R_UART_TX_TO_TX`, `R_UART_RX_TO_RX`,
`S_RAIL_NO_TEST_POINT`, `S_RAIL_NO_INDICATOR`, `S_RAIL_NOT_ISOLATED`,
`S_SPARE_IO_UNREACHED`) follow mistakes 4, 5, 8, 9 and 10 of a talk on
prototype boards (cited as
`PROTOTYPE`); the UART rules go by the pins' types as
well as their names, since a CAN transceiver's TXD is an input that the
controller's transmit output rightly drives.

Two of the rules (`S_SUPPLY_IN_NO_BULK`, `S_CONTROL_INPUT_UNSET`) follow
an idea read in Konnect's documentation; their design and sources are
Kairo's own (docs/research.md). The second was first written for any
input with only capacitors beside it; on the real boards all ten it
named were sound (soft-start pins, inputs coupled through a capacitor,
crystal inputs), so it was narrowed to the pins where a level matters.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from kairo.library.grounding import Grounded, GroundedPin
from kairo.library.kinds import DIODES, PASSIVES, TERMINAL_LIBRARIES
from kairo.units import QuantityError, parse_quantity

I2C = "NXP UM10204, I2C-bus specification, section 3.1.1"
FLOATING = "TI SCBA004, Implications of Slow or Floating CMOS Inputs"
DECOUPLING = "Analog Devices MT-101, Decoupling Techniques"
ESD = "IEC 61000-4-2, electrostatic discharge immunity"
OPEN_COLLECTOR = (
    "the pin's type in KiCad: an open-collector or open-drain output only pulls low"
)
LED = "Ohm's law: a diode across a supply has nothing to set its current"
FEEDBACK = "TI TPS54202 datasheet SLVSD26C, equation 3: Vout = Vref (1 + R2 / R3)"
BODY_DIODE = (
    "Infineon IPT015N10N5 datasheet Rev 2.4 p5: a MOSFET's body diode conducts "
    "whatever its gate does; TI BQ76952 datasheet SLUSE13B Figure 16-1"
)
SHARED_CURRENT = "Kirchhoff's current law: current divides between parallel paths"
BULK = (
    "Analog Devices MT-101, Decoupling Techniques: larger capacitors act as a "
    "charge reservoir for transient currents"
)
PROTOTYPE = (
    'Cosplay Light and Sound, "Top Fifteen Mistakes People Make When Designing '
    'Prototype PCBs" (YouTube hkSad4n76Lc), mistakes 4 (no test points), 5 (no '
    "indicator LEDs), 8 (no isolation jumpers), 9 (unused GPIOs not broken out) "
    "and 10 (UART mixups)"
)
TWO_OUTPUTS = (
    "the pins' types in KiCad: two outputs on one net drive it against each other"
)

#: Pin names that mark an I2C line. Matched whole, without regard to case.
_I2C_NAMES = frozenset({"SDA", "SCL"})
#: Pin names that mark a regulator's feedback input. Matched whole.
_FEEDBACK_NAMES = frozenset({"FB", "VFB", "ADJ", "FEEDBACK"})
#: Pin names that mark a USB data line.
_USB_DATA = frozenset({"D+", "D-", "DP", "DM", "DN"})
#: KiCad libraries of parts made to absorb a discharge.
_PROTECTION_LIBRARIES = frozenset({"Power_Protection"})
_PROTECTION_SYMBOLS = frozenset({"Device:D_TVS", "Device:D_TVS_Dual_AAC"})
#: A word of a pin's name that marks a UART's transmit or receive line:
#: TX, TXD, U0TXD, TXD1 (and RX alike), the word split off at any mark.
_UART = re.compile(r"^U?\d*(TX|RX)D?\d*$")
#: Rules that need the design to say which nets are supplies.
_NEED_ROLES = 8
#: A capacitor of at least this many farads is taken as bulk (A205).
_BULK_FARADS = 10e-6
#: Libraries whose parts bring a supply onto the board. Test points are
#: places to probe, not to feed, and KiCad keeps its own in the Connector
#: library (Connector:TestPoint), so they are told apart by name.
_INLETS = frozenset({"Connector", "Connector_Generic"})
#: Pin names that hold a chip in reset or turn it on, matched whole after
#: the marks KiCad and makers put on an active-low name are taken off.
_CONTROL_NAMES = frozenset(
    {"RESET", "RST", "NRST", "MCLR", "EN", "ENABLE", "CE", "SHDN", "SD", "PD"}
)
#: Pin types that set a net's level, or may.
_DRIVERS = frozenset(
    {
        "output",
        "bidirectional",
        "tri_state",
        "open_collector",
        "open_emitter",
        "power_out",
        "power_in",
        "unspecified",
        "free",
    }
)
#: Words in a FET symbol's description or keywords that give its channel.
_N_CHANNEL = ("N-CHANNEL", "N-MOS", "NMOS")
_P_CHANNEL = ("P-CHANNEL", "P-MOS", "PMOS")
#: A path around a switch with less resistance than this carries real
#: current, a fault; one with more is a leak, which is a suggestion.
_BYPASS_OHMS = 1000.0
#: A plain resistor of this value or less is taken as a current sense.
_SENSE_OHMS = 0.1
#: Symbols whose pins are all one conductor: fuses, ferrites, shunts.
_CONDUCTORS = ("Device:Fuse", "Device:Polyfuse", "Device:Ferrite", "Device:R_Shunt")


@dataclass(frozen=True, slots=True)
class Remark:
    """One thing the review has to say.

    Attributes:
        kind: ``fault`` or ``suggestion``.
        code: A stable code such as ``R_INPUT_FLOATING``.
        where: The part, pin or net.
        message: What was found, and for a suggestion what Kairo would do.
        source: What the rule rests on.
    """

    kind: Literal["fault", "suggestion"]
    code: str
    where: str
    message: str
    source: str

    def as_dict(self) -> dict[str, Any]:
        """The remark as plain data."""
        return {
            "code": self.code,
            "where": self.where,
            "message": self.message,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class Review:
    """Everything the review found.

    Attributes:
        remarks: Faults first, then suggestions, each in the design's order.
        skipped: How many rules were not run because the design states no
            supply nets.
    """

    remarks: tuple[Remark, ...]
    skipped: int

    @property
    def faults(self) -> tuple[Remark, ...]:
        """The remarks that are faults."""
        return tuple(r for r in self.remarks if r.kind == "fault")

    @property
    def suggestions(self) -> tuple[Remark, ...]:
        """The remarks that are suggestions."""
        return tuple(r for r in self.remarks if r.kind == "suggestion")


def review_design(grounded: Grounded) -> Review:
    """Read a design against Kairo's rules.

    Args:
        grounded: The design with every pin on its net.

    Returns:
        The faults and suggestions. A design that draws nothing wrong and
        nothing Kairo would do otherwise gets none.
    """
    design = grounded.design
    on_net: dict[str, list[GroundedPin]] = {}
    nets_of: dict[str, list[str]] = {}
    pins_of: dict[str, list[GroundedPin]] = {}
    for pin in grounded.pins:
        on_net.setdefault(pin.net, []).append(pin)
        nets_of.setdefault(pin.ref, []).append(pin.net)
        pins_of.setdefault(pin.ref, []).append(pin)
    supplies = {name for name, net in design.nets.items() if net.role == "power"}
    grounds = {name for name, net in design.nets.items() if net.role == "ground"}

    def kind(ref: str) -> str:
        return PASSIVES.get(design.components[ref].lib_id, ("", ""))[0]

    def library(ref: str) -> str:
        return design.components[ref].lib_id.partition(":")[0]

    def could_pull(net: str) -> bool:
        # Any two-pin part but a capacitor may lead to a supply: a
        # resistor, a resistor under a symbol Kairo does not know, or an
        # LED the output sinks.
        return any(
            len(nets_of[pin.ref]) == 2 and kind(pin.ref) != "C" for pin in on_net[net]
        )

    def may_be_a_capacitor(ref: str) -> bool:
        # A board may draw its capacitors with symbols of its own.
        return (
            len(nets_of[ref]) == 2
            and not kind(ref)
            and design.components[ref].lib_id not in DIODES
            and library(ref) not in TERMINAL_LIBRARIES
        )

    def leaves_the_board(net: str) -> bool:
        return any(library(pin.ref) in TERMINAL_LIBRARIES for pin in on_net[net])

    faults: list[Remark] = []
    suggestions: list[Remark] = []

    for net, pins in on_net.items():
        # A connector's pin may be named from either side of the cable.
        on_chips = [p for p in pins if library(p.ref) not in TERMINAL_LIBRARIES]
        senders = sorted(
            {
                f"{p.ref}.{p.pin.name}"
                for p in on_chips
                if _uart(p.pin.name) == "TX"
                and p.pin.electrical_type in ("output", "tri_state")
            }
        )
        if len({s.partition(".")[0] for s in senders}) >= 2:
            faults.append(
                Remark(
                    "fault",
                    "R_UART_TX_TO_TX",
                    net,
                    f"{net} joins two transmit outputs ({', '.join(senders[:4])}), "
                    "so they drive it against each other and nothing receives. "
                    "Cross the UART: each side's TX to the other's RX",
                    f"{TWO_OUTPUTS}; {PROTOTYPE}",
                )
            )
        listeners = sorted(
            {
                f"{p.ref}.{p.pin.name}"
                for p in on_chips
                if _uart(p.pin.name) == "RX" and p.pin.electrical_type == "input"
            }
        )
        if len({s.partition(".")[0] for s in listeners}) >= 2 and all(
            p.pin.electrical_type == "input" for p in pins
        ):
            faults.append(
                Remark(
                    "fault",
                    "R_UART_RX_TO_RX",
                    net,
                    f"{net} joins two receive inputs ({', '.join(listeners[:4])}) "
                    "and nothing that sends, so its level is not set. Cross the "
                    "UART: each side's TX to the other's RX",
                    f"{FLOATING}; {PROTOTYPE}",
                )
            )

    for pins in on_net.values():
        if len(pins) == 1 and pins[0].pin.electrical_type == "input":
            pin = pins[0]
            name = pin.pin.name or pin.pin.number
            faults.append(
                Remark(
                    "fault",
                    "R_INPUT_FLOATING",
                    f"{pin.ref}.{name}",
                    f"{pin.ref}'s input {name} is on a net with nothing else, so "
                    "its level is not set. Tie it high or low, or mark it "
                    "not connected if its datasheet allows that",
                    FLOATING,
                )
            )

    for net, pins in on_net.items():
        if net in supplies or net in grounds or len(pins) < 2:
            continue
        inputs = [
            p
            for p in pins
            if p.pin.electrical_type == "input"
            and library(p.ref) not in TERMINAL_LIBRARIES
            and _control(p.pin.name)
        ]
        # Only inputs and capacitors: nothing drives the net or pulls it.
        # A pin that is passive belongs to a capacitor here or the net is
        # left alone, since a passive pin of any other part may set it.
        if not inputs or any(
            p.pin.electrical_type in _DRIVERS
            or (p.pin.electrical_type == "passive" and kind(p.ref) != "C")
            for p in pins
        ):
            continue
        capacitors = sorted({p.ref for p in pins if kind(p.ref) == "C"})
        first = inputs[0]
        name = first.pin.name or first.pin.number
        beside = (
            f"only {', '.join(capacitors)} beside it"
            if capacitors
            else "only other inputs on it"
        )
        suggestions.append(
            Remark(
                "suggestion",
                "S_CONTROL_INPUT_UNSET",
                f"{first.ref}.{name}",
                f"{first.ref}'s {name} input is on {net} with {beside}, so "
                "nothing drawn sets its level. It is sound only if a pull "
                f"inside {first.ref} sets it, which its datasheet says; Kairo "
                "would draw a resistor to the level it should rest at",
                FLOATING,
            )
        )

    if supplies:
        for net, pins in on_net.items():
            if net in supplies or net in grounds or len(pins) < 2:
                continue
            if could_pull(net) or leaves_the_board(net):
                continue
            open_outputs = [
                p
                for p in pins
                if p.pin.electrical_type in ("open_collector", "open_emitter")
            ]
            buses = sorted(
                {p.pin.name.upper() for p in pins if p.pin.name.upper() in _I2C_NAMES}
            )
            if buses:
                suggestions.append(
                    Remark(
                        "suggestion",
                        "S_I2C_NO_PULLUP_DRAWN",
                        net,
                        f"{net} carries I2C {'/'.join(buses)} with no pull-up "
                        "drawn, so it relies on one inside a chip, which is "
                        "often too weak for the bus's rise time. Kairo would "
                        "draw one to the supply: calc i2c_pullup_max gives the "
                        "largest that will do",
                        I2C,
                    )
                )
            elif open_outputs:
                first = open_outputs[0]
                suggestions.append(
                    Remark(
                        "suggestion",
                        "S_OPEN_OUTPUT_NO_PULL_DRAWN",
                        net,
                        f"{net} is driven by an open output "
                        f"({first.ref}.{first.pin.name or first.pin.number}) with "
                        "nothing drawn to set its other level, so it relies on a "
                        "pull inside a chip on the net. Kairo would draw a "
                        "resistor to the supply",
                        OPEN_COLLECTOR,
                    )
                )

        for net in sorted(supplies):
            on_rail = on_net.get(net, [])
            # A chip to decouple is fed from the rail and returns to
            # ground. A connector's supply pin is where power comes in or
            # goes out.
            supply_pins = [
                p
                for p in on_rail
                if p.pin.electrical_type == "power_in"
                and library(p.ref) not in TERMINAL_LIBRARIES
                and any(
                    other.net in grounds and other.pin.electrical_type == "power_in"
                    for other in pins_of[p.ref]
                )
            ]
            fed = sorted({p.ref for p in supply_pins})
            to_ground = {
                p.ref for p in on_rail if any(n in grounds for n in nets_of[p.ref])
            }
            capacitors = sorted(ref for ref in to_ground if kind(ref) == "C")
            if not fed or any(may_be_a_capacitor(ref) for ref in to_ground):
                continue
            inlets = sorted(
                {
                    p.ref
                    for p in on_rail
                    if library(p.ref) in _INLETS
                    and "TestPoint" not in design.components[p.ref].lib_id
                }
            )
            made_here = any(
                p.pin.electrical_type == "power_out"
                and library(p.ref) not in TERMINAL_LIBRARIES
                for p in on_rail
            )
            sizes = [_farads(grounded, ref) for ref in capacitors]
            if (
                capacitors
                and inlets
                and not made_here
                and None not in sizes
                and max(s for s in sizes if s is not None) < _BULK_FARADS
            ):
                suggestions.append(
                    Remark(
                        "suggestion",
                        "S_SUPPLY_IN_NO_BULK",
                        net,
                        f"{net} comes in through {', '.join(inlets[:3])} and "
                        f"feeds {', '.join(fed[:4])}"
                        f"{' and others' if len(fed) > 4 else ''}, and its "
                        f"largest capacitor is {_shown(max(s for s in sizes if s))}. "
                        "A long or plugged-in supply lead cannot answer a sudden "
                        "load at once; Kairo would put a bulk capacitor of "
                        f"{_shown(_BULK_FARADS)} or more where it comes in",
                        BULK,
                    )
                )
            if not capacitors:
                faults.append(
                    Remark(
                        "fault",
                        "R_RAIL_NO_DECOUPLING",
                        net,
                        f"{net} supplies {', '.join(fed[:4])}"
                        f"{' and others' if len(fed) > 4 else ''} and has no "
                        "capacitor to ground drawn on it at all",
                        DECOUPLING,
                    )
                )
            elif len(capacitors) < len(supply_pins):
                suggestions.append(
                    Remark(
                        "suggestion",
                        "S_DECOUPLING_SHARED",
                        net,
                        f"{net} has {len(capacitors)} capacitor(s) to ground for "
                        f"{len(supply_pins)} supply pins. That can be sound; Kairo "
                        "would give each supply pin its own, placed beside it",
                        DECOUPLING,
                    )
                )

        for pin in (p for net in sorted(supplies) for p in on_net.get(net, [])):
            if pin.pin.name.upper() not in _FEEDBACK_NAMES:
                continue
            part = design.components[pin.ref]
            rail = design.nets[pin.net].voltage
            reference = part.quantity("v_ref")
            where = f"{pin.ref}.{pin.pin.name}"
            if reference is None:
                suggestions.append(
                    Remark(
                        "suggestion",
                        "S_FEEDBACK_ON_RAIL",
                        where,
                        f"{where} sits straight on {pin.net} with no divider, so "
                        f"{pin.ref} holds {pin.net} at its reference voltage. That "
                        f"is right only if {pin.net} is meant to be at it; give "
                        f"{pin.ref}'s v_ref and Kairo will say",
                        FEEDBACK,
                    )
                )
            elif rail is not None and not _close(rail.value, reference.value):
                faults.append(
                    Remark(
                        "fault",
                        "R_FEEDBACK_NO_DIVIDER",
                        where,
                        f"{where} sits straight on {pin.net} with no divider, so "
                        f"{pin.ref} holds {pin.net} at its {reference.value:g} V "
                        f"reference, not the {rail.value:g} V the design gives it. "
                        "Feed it from a divider of the output",
                        FEEDBACK,
                    )
                )

        for ref, part in design.components.items():
            nets = nets_of.get(ref, [])
            if (
                part.lib_id in DIODES
                and "LED" in part.lib_id
                and len(nets) == 2
                and any(n in supplies for n in nets)
                and any(n in grounds for n in nets)
            ):
                # Not a fault: a real board draws a 5 V protection diode
                # with the LED symbol, straight across its rail.
                suggestions.append(
                    Remark(
                        "suggestion",
                        "S_LED_SYMBOL_ACROSS_RAIL",
                        ref,
                        f"{ref} has an LED's symbol and sits straight across "
                        f"{next(n for n in nets if n in supplies)} and ground. If "
                        "it is an LED, it needs a resistor in series to limit "
                        "its current; if it is a protection diode, Kairo would "
                        "draw it with that symbol",
                        LED,
                    )
                )

    for net, pins in on_net.items():
        exposed = [
            p
            for p in pins
            if library(p.ref) in TERMINAL_LIBRARIES
            and p.pin.name.upper() in _USB_DATA
            and "USB" in design.components[p.ref].lib_id.upper()
        ]
        protected = any(
            library(p.ref) in _PROTECTION_LIBRARIES
            or design.components[p.ref].lib_id in _PROTECTION_SYMBOLS
            for p in pins
        )
        if exposed and not protected and len(pins) > 1:
            suggestions.append(
                Remark(
                    "suggestion",
                    "S_USB_DATA_UNPROTECTED",
                    net,
                    f"{net} runs from {exposed[0].ref}, a connector a person can "
                    "touch, to the circuit with no protection part drawn on it. "
                    "The chip it reaches may have enough of its own; Kairo would "
                    "put an ESD protection part at the connector",
                    ESD,
                )
            )

    if supplies:
        for remark in _bypassed_switches(grounded, pins_of, grounds):
            (faults if remark.kind == "fault" else suggestions).append(remark)
        suggestions += _shared_sense_paths(grounded, on_net, grounds)
    if supplies and design.policy.prototype:
        untested = [
            net
            for net in sorted(supplies | grounds)
            if len(on_net.get(net, [])) >= 2
            and not any(
                "TestPoint" in design.components[p.ref].lib_id for p in on_net[net]
            )
        ]
        if untested:
            suggestions.append(
                Remark(
                    "suggestion",
                    "S_RAIL_NO_TEST_POINT",
                    ", ".join(untested),
                    f"{', '.join(untested[:6])}"
                    f"{' and others' if len(untested) > 6 else ''} "
                    "have no test point, so a first board can be probed only at "
                    "a part's pins. Kairo would give each supply and ground a "
                    "test point (Connector:TestPoint) to clip a probe to",
                    PROTOTYPE,
                )
            )
        # Rails made on the board: a regulator's output is on them.
        made = [
            net
            for net in sorted(supplies)
            if any(
                p.pin.electrical_type == "power_out"
                and library(p.ref) not in TERMINAL_LIBRARIES
                for p in on_net.get(net, [])
            )
        ]

        def lit(net: str) -> bool:
            # An LED on the rail, or on a net a resistor joins to it.
            near = {net} | {
                other
                for p in on_net[net]
                if kind(p.ref) == "R" and len(nets_of[p.ref]) == 2
                for other in nets_of[p.ref]
            }
            return any(
                "LED" in design.components[p.ref].lib_id
                and design.components[p.ref].lib_id in DIODES
                for n in near
                for p in on_net.get(n, [])
            )

        dark = [net for net in made if not lit(net)]
        if dark:
            suggestions.append(
                Remark(
                    "suggestion",
                    "S_RAIL_NO_INDICATOR",
                    ", ".join(dark),
                    f"{', '.join(dark[:6])}{' and others' if len(dark) > 6 else ''} "
                    "light no LED, so a first board shows nothing when one of "
                    "them is missing. Kairo would give each an LED with its "
                    "resistor, sized with calc led_current",
                    PROTOTYPE,
                )
            )
        tied = [
            net
            for net in made
            if any(
                p.pin.electrical_type == "power_in"
                and library(p.ref) not in TERMINAL_LIBRARIES
                for p in on_net[net]
            )
        ]
        if tied:
            suggestions.append(
                Remark(
                    "suggestion",
                    "S_RAIL_NOT_ISOLATED",
                    ", ".join(tied),
                    f"{', '.join(tied[:6])}{' and others' if len(tied) > 6 else ''} "
                    "feed chips straight from the regulator's output, so a "
                    "rail cannot be proven before it powers them. Kairo would "
                    "put a 0 ohm link or a solder jumper between the output "
                    "and the loads, to fit once the rail measures right",
                    PROTOTYPE,
                )
            )
    if design.policy.prototype:
        spare = _spare_io(grounded, kind, library)
        if spare:
            suggestions.append(
                Remark(
                    "suggestion",
                    "S_SPARE_IO_UNREACHED",
                    ", ".join(spare),
                    f"{', '.join(spare[:8])}{' and others' if len(spare) > 8 else ''} "
                    "are spare I/O left open, so a fix on a first board needs a "
                    "new board. Kairo would bring them to a header or test "
                    "points, so it needs a wire instead",
                    PROTOTYPE,
                )
            )
    # The prototype rules wait for the roles too, on a prototype.
    skipped = _NEED_ROLES + 3 * design.policy.prototype
    return Review((*faults, *suggestions), 0 if supplies else skipped)


@dataclass(frozen=True, slots=True)
class _Step:
    """One way current can pass from one net to another with every switch off.

    Attributes:
        to: The net it reaches.
        ref: The part it passes through.
        how: What in the part conducts, for the message.
        ohms: The part's resistance, 0 for a diode or a conductor, ``None``
            if its value cannot be read.
    """

    to: str
    ref: str
    how: str
    ohms: float | None


def _channel(grounded: Grounded, ref: str) -> Literal["n", "p"] | None:
    """A MOSFET's channel, from its symbol's description and keywords.

    A drawn schematic is read without its library symbols, so there only
    the symbol's name can tell, as in ``Q_NMOS_GDS``; a part it does not
    tell about is left out.
    """
    symbol = grounded.symbols.get(ref)
    words = (symbol.description, symbol.keywords) if symbol is not None else ()
    text = " ".join((grounded.design.components[ref].lib_id, *words)).upper()
    n = any(word in text for word in _N_CHANNEL)
    p = any(word in text for word in _P_CHANNEL)
    if n == p:
        return None
    return "n" if n else "p"


def _spare_io(
    grounded: Grounded,
    kind: Callable[[str], str],
    library: Callable[[str], str],
) -> list[str]:
    """A chip's bidirectional pins the design leaves open, as ``REF.NAME``."""
    spare: list[str] = []
    # A drawn schematic gives its open pins' names and types itself.
    for open_pin in grounded.open_pins:
        if kind(open_pin.ref) or library(open_pin.ref) in TERMINAL_LIBRARIES:
            continue
        if open_pin.pin.electrical_type == "bidirectional":
            spare.append(f"{open_pin.ref}.{open_pin.pin.name or open_pin.pin.number}")
    for entry in grounded.design.no_connect:
        if grounded.open_pins:
            break
        ref, _, number = entry.partition(".")
        symbol = grounded.symbols.get(ref)
        if symbol is None or kind(ref) or library(ref) in TERMINAL_LIBRARIES:
            continue
        pin = next(
            (p for p in symbol.pins if number in (p.number, p.name)),
            None,
        )
        if pin is not None and pin.electrical_type == "bidirectional":
            spare.append(f"{ref}.{pin.name or pin.number}")
    return spare


def _uart(name: str) -> str | None:
    """``TX`` or ``RX`` for a UART line's pin name, else None."""
    words = [w for w in re.split(r"[^A-Z0-9]+", name.upper()) if w]
    found = _UART.match(words[-1]) if words else None
    return found.group(1) if found else None


def _control(name: str) -> bool:
    """Whether a pin name is a reset or enable: RESET, ~{RESET}, nRST, EN_N."""
    bare = name.upper().replace("~{", "").replace("}", "").strip("/#!~*")
    for suffix in ("_N", "_B", "#", "N"):
        if bare.endswith(suffix) and bare[: -len(suffix)] in _CONTROL_NAMES:
            return True
    if bare.startswith("N") and bare[1:] in _CONTROL_NAMES:
        return True
    return bare in _CONTROL_NAMES


def _farads(grounded: Grounded, ref: str) -> float | None:
    value = grounded.design.components[ref].value
    try:
        return parse_quantity(value or "", "F", bare=True).value
    except QuantityError:
        return None


def _shown(farads: float | None) -> str:
    if farads is None:
        return "not known"
    for scale, unit in ((1e-3, "mF"), (1e-6, "uF"), (1e-9, "nF")):
        if farads >= scale:
            return f"{farads / scale:g} {unit}"
    return f"{farads / 1e-12:g} pF"


def _ohms(grounded: Grounded, ref: str) -> float | None:
    value = grounded.design.components[ref].value
    try:
        return parse_quantity(value or "", "ohm", bare=True).value
    except QuantityError:
        return None


def _conduction(
    grounded: Grounded, pins_of: dict[str, list[GroundedPin]], skip: set[str]
) -> dict[str, list[_Step]]:
    """Every way current passes between nets while every switch is off.

    A resistor, inductor, fuse, ferrite or shunt conducts both ways; a
    diode from its anode to its cathode, a Zener and a TVS only forward
    (their breakdown is taken to be above the circuit's voltage); a
    MOSFET through its body diode, source to drain for an N-channel part
    and drain to source for a P-channel one. Chips, transistors other
    than MOSFETs and capacitors do not conduct here.
    """
    design = grounded.design
    steps: dict[str, list[_Step]] = {}

    def add(a: str, b: str, ref: str, how: str, ohms: float | None) -> None:
        if a != b:
            steps.setdefault(a, []).append(_Step(b, ref, how, ohms))

    for ref, pins in pins_of.items():
        if ref in skip:
            continue
        lib_id = design.components[ref].lib_id
        kind = PASSIVES.get(lib_id, ("", ""))[0]
        names = {pin.pin.name.upper(): pin.net for pin in pins}
        nets = sorted({pin.net for pin in pins})
        if kind in ("R", "L") or lib_id.startswith(_CONDUCTORS):
            ohms = _ohms(grounded, ref) if kind == "R" else 0.0
            for a in nets:
                for b in nets:
                    add(a, b, ref, ref, ohms)
        elif {"G", "S", "D"} <= set(names):
            channel = _channel(grounded, ref)
            if channel == "n":
                add(names["S"], names["D"], ref, f"{ref} (its body diode)", 0.0)
            elif channel == "p":
                add(names["D"], names["S"], ref, f"{ref} (its body diode)", 0.0)
        elif {p.pin.name.upper() for p in pins} <= {"A", "K"} and len(nets) > 1:
            for anode in (p.net for p in pins if p.pin.name.upper() == "A"):
                for cathode in (p.net for p in pins if p.pin.name.upper() == "K"):
                    add(anode, cathode, ref, ref, 0.0)
    return steps


def _bypassed_switches(
    grounded: Grounded, pins_of: dict[str, list[GroundedPin]], grounds: set[str]
) -> list[Remark]:
    """Back-to-back MOSFET switches with a path around them.

    Two banks of MOSFETs of one channel in series, sharing their drains
    (or their sources), block current both ways when both are off: each
    bank's body diode faces the other's. A path from one outer net to the
    other that conducts with every switch off, through resistors, diodes
    or other FETs' body diodes, carries current the switch is there to
    stop. Paths through a ground net are not followed: they would need a
    ground above a supply.
    """
    banks: dict[tuple[str, str, str], list[str]] = {}
    for ref, pins in pins_of.items():
        names = {pin.pin.name.upper(): pin.net for pin in pins}
        if not {"G", "S", "D"} <= set(names):
            continue
        channel = _channel(grounded, ref)
        if channel is not None:
            banks.setdefault((channel, names["S"], names["D"]), []).append(ref)
    remarks: list[Remark] = []
    keys = sorted(banks)
    for i, first in enumerate(keys):
        for second in keys[i + 1 :]:
            if first[0] != second[0]:
                continue
            # (outer net, middle net, whether its body diode runs outer to middle)
            n = first[0] == "n"
            if first[2] == second[2] and first[1] != second[1]:
                ends = ((first[1], first[2], n), (second[1], second[2], n))
            elif first[1] == second[1] and first[2] != second[2]:
                ends = ((first[2], first[1], not n), (second[2], second[1], not n))
            else:
                continue
            if any(outer in grounds for outer, _, _ in ends):
                continue
            pair = banks[first] + banks[second]
            steps = _conduction(grounded, pins_of, set(pair))
            for (start, _, _), (end, _, toward_middle) in (ends, ends[::-1]):
                path = _path(steps, start, end, grounds)
                if path is None:
                    continue
                # The bank at the end blocks current arriving there if its
                # body diode runs toward the middle; else the start's bank.
                at_end = end == ends[0][0]
                blocker = banks[first if at_end == toward_middle else second]
                remarks.append(_bypass_remark(start, end, path, blocker))
    return remarks


def _path(
    steps: dict[str, list[_Step]], start: str, end: str, grounds: set[str]
) -> list[_Step] | None:
    """The path of fewest parts from one net to another, or ``None``."""
    came: dict[str, _Step | None] = {start: None}
    before: dict[str, str] = {}
    queue = deque([start])
    while queue:
        net = queue.popleft()
        if net == end:
            path: list[_Step] = []
            while net != start:
                step = came[net]
                assert step is not None
                path.append(step)
                net = before[net]
            return path[::-1]
        for step in steps.get(net, []):
            if step.to in came or step.to in grounds:
                continue
            came[step.to] = step
            before[step.to] = net
            queue.append(step.to)
    return None


def _bypass_remark(
    start: str, end: str, path: list[_Step], blocker: list[str]
) -> Remark:
    ohms = [step.ohms for step in path]
    known = [o for o in ohms if o is not None]
    total = sum(known) if len(known) == len(ohms) else None
    route = " -> ".join([start, *(f"{s.how} -> {s.to}" for s in path)])
    switch = ("The switch " if len(blocker) > 1 else "") + ", ".join(blocker)
    resistance = "an unknown resistance" if total is None else f"{total:g} ohm"
    if total is not None and total < _BYPASS_OHMS:
        return Remark(
            "fault",
            "R_SWITCH_BYPASSED",
            ", ".join(blocker),
            f"{switch} is meant to stop current from {start} to {end} when "
            "off, "
            f"but it can flow around them through {resistance}: {route}. Move "
            "that path's end to the switch's own side, or block it with a "
            "diode",
            BODY_DIODE,
        )
    return Remark(
        "suggestion",
        "S_SWITCH_LEAKS",
        ", ".join(blocker),
        f"{switch} is meant to stop current from {start} to {end} when "
        "off; a "
        f"path through {resistance} still conducts that way: {route}. That is "
        "a leak rather than a bypass if its resistance is high enough for the "
        "current that may flow",
        BODY_DIODE,
    )


def _natural(ref: str) -> tuple[str, int]:
    """A reference sorted as people read it: J2 before J10."""
    head = ref.rstrip("0123456789")
    return (head, int(ref[len(head) :] or 0))


def _shared_sense_paths(
    grounded: Grounded, on_net: dict[str, list[GroundedPin]], grounds: set[str]
) -> list[Remark]:
    """Current-sense resistors in a return, with wiring off the board on both sides.

    A sense resistor measures the current through itself. If a connector
    on each side leads to wiring that is joined again off the board, that
    wiring is a parallel path whose current is not measured. Only a
    resistor with a ground on one side is asked about: returns are what
    gets joined off a board, while a supply sensed on its way out (a real
    booster board's 0.1 ohm before its output) is meant to go on to other
    boards. The far ends are not in the design, so this can only ask.
    """
    design = grounded.design
    remarks: list[Remark] = []

    def connectors(net: str) -> list[str]:
        found = {
            pin.ref
            for pin in on_net.get(net, [])
            if design.components[pin.ref].lib_id.partition(":")[0] in TERMINAL_LIBRARIES
            and "TestPoint" not in design.components[pin.ref].lib_id
        }
        return sorted(found, key=_natural)

    for ref, part in design.components.items():
        pins = [p for p in grounded.pins if p.ref == ref]
        if part.lib_id.startswith("Device:R_Shunt"):
            force = sorted({p.net for p in pins if p.pin.number in ("1", "4")})
        elif PASSIVES.get(part.lib_id, ("", ""))[0] == "R":
            ohms = _ohms(grounded, ref)
            if ohms is None or ohms > _SENSE_OHMS:
                continue
            force = sorted({p.net for p in pins})
        else:
            continue
        if len(force) != 2 or not any(net in grounds for net in force):
            continue
        sides = [connectors(net) for net in force]
        if not all(sides) or len(sides[0]) + len(sides[1]) < 3:
            continue
        remarks.append(
            Remark(
                "suggestion",
                "S_SENSE_RESISTOR_SHARED",
                ref,
                f"{ref} senses current between {force[0]} and {force[1]}, and "
                f"wiring leaves the board on both sides: {force[0]} through "
                f"{', '.join(sides[0])}, {force[1]} through {', '.join(sides[1])}. "
                "Any two of those joined off the board, a ground bonded at both "
                f"ends for example, carry current around {ref} that it does not "
                "measure. Kairo would keep every off-board return on one side, "
                "or confirm each far end",
                SHARED_CURRENT,
            )
        )
    return remarks


def _close(a: float, b: float) -> bool:
    """Whether a rail's stated voltage is its regulator's reference, to 5%."""
    return abs(a - b) <= 0.05 * abs(b)
