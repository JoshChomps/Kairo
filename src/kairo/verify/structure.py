"""Layer L2, electrical structure: is the circuit wired in a way that can work?

These checks need no file and no KiCad: they read the grounded design,
where every pin's electrical type is the library's. Each finding names the
part, pin and net at fault and says what would put it right.

A pin the design leaves unconnected is a fault unless the design lists it
under ``no_connect`` or the library types it as a no-connect pin: an open
pin is either a mistake or a decision, and the design has to say which.
"""

from __future__ import annotations

from dataclasses import dataclass

from kairo.ir import split_pin
from kairo.library.grounding import Grounded
from kairo.library.reader import Pin
from kairo.library.resolve import PinError, pin_label, resolve_pin
from kairo.verify.findings import Finding, Where

#: Pin types that drive a net hard; two of them on one net fight.
DRIVERS = frozenset({"output", "power_out"})


@dataclass(frozen=True, slots=True)
class StructureRules:
    """Which optional structure rules are on.

    Attributes:
        decoupling: Whether every supply pin of an integrated circuit
            needs a capacitor from its net to ground.
        ic_prefixes: Reference prefixes that mark an integrated circuit.
        capacitor_prefixes: Reference prefixes that mark a capacitor.
    """

    decoupling: bool = True
    ic_prefixes: tuple[str, ...] = ("U", "IC")
    capacitor_prefixes: tuple[str, ...] = ("C",)


def check_structure(
    grounded: Grounded, rules: StructureRules | None = None
) -> list[Finding]:
    """Check a grounded design's electrical structure.

    Args:
        grounded: The design with its pins resolved.
        rules: Which optional rules are on; the defaults if not given.

    Returns:
        The findings, errors and warnings, in the order of the design's
        parts and then its nets.
    """
    rules = rules or StructureRules()
    design = grounded.design
    findings: list[Finding] = []
    on_net = {(pin.ref, pin.pin.number): pin.net for pin in grounded.pins}
    open_by_design = _no_connects(grounded, findings)

    for ref, part in design.components.items():
        for pin in grounded.symbols[ref].pins:
            if pin.unit not in (0, part.unit) or pin.body_style not in (0, 1):
                continue
            key = (ref, pin.number)
            name = pin_label(pin)
            if key in on_net:
                if key in open_by_design:
                    findings.append(
                        Finding(
                            "L2",
                            "E_NO_CONNECT_CONNECTED",
                            "error",
                            f"{ref} pin {name} is listed under no_connect and "
                            f"is also on net {on_net[key]}",
                            Where(ref, name, on_net[key]),
                            "remove it from no_connect or from the net",
                        )
                    )
                continue
            if key in open_by_design or pin.electrical_type == "no_connect":
                continue
            findings.append(_open_pin(ref, pin, name))

    for net, role in ((name, design.nets[name].role) for name in design.nets):
        pins = grounded.net_pins(net)
        if len(pins) == 1:
            only = pins[0]
            findings.append(
                Finding(
                    "L2",
                    "E_NET_SINGLE_PIN",
                    "error",
                    f"net {net} has only one pin, {only.ref} pin "
                    f"{pin_label(only.pin)}, so it connects nothing",
                    Where(only.ref, pin_label(only.pin), net),
                    "connect a second pin to the net, or remove the net and "
                    "list the pin under no_connect",
                )
            )
        if not pins:
            findings.append(
                Finding(
                    "L2",
                    "W_NET_EMPTY",
                    "warning",
                    f"net {net} has no pins",
                    Where(net=net),
                    "remove the net or connect pins to it",
                )
            )
        drivers = [p for p in pins if p.pin.electrical_type in DRIVERS]
        if len(drivers) > 1:
            names = ", ".join(f"{p.ref} pin {pin_label(p.pin)}" for p in drivers)
            first = drivers[0]
            findings.append(
                Finding(
                    "L2",
                    "E_OUTPUTS_SHORTED",
                    "error",
                    f"net {net} joins {len(drivers)} outputs: {names}",
                    Where(first.ref, pin_label(first.pin), net),
                    "an output may share a net only with inputs and passive pins",
                )
            )
        if role == "signal":
            for p in pins:
                if p.pin.electrical_type == "power_in":
                    findings.append(
                        Finding(
                            "L2",
                            "E_POWER_PIN_ON_SIGNAL_NET",
                            "error",
                            f"{p.ref} pin {pin_label(p.pin)} is a supply "
                            f"pin, and net {net} has the role signal",
                            Where(p.ref, pin_label(p.pin), net),
                            "connect it to a power or ground net, or give this "
                            "net that role",
                        )
                    )

    if rules.decoupling:
        findings += _decoupling(grounded, rules)
    return findings


def _open_pin(ref: str, pin: Pin, name: str) -> Finding:
    kind = pin.electrical_type
    if kind == "power_in":
        return Finding(
            "L2",
            "E_POWER_PIN_UNCONNECTED",
            "error",
            f"{ref} pin {name} (power_in) is not on any net",
            Where(ref, name),
            f"connect {ref}.{name} to a power or ground net",
        )
    if kind == "input":
        return Finding(
            "L2",
            "E_INPUT_FLOATING",
            "error",
            f"{ref} pin {name} (input) is not on any net, so it floats",
            Where(ref, name),
            f"connect {ref}.{name}, for example through a pull-up or pull-down",
        )
    return Finding(
        "L2",
        "E_PIN_UNCONNECTED",
        "error",
        f"{ref} pin {name} ({kind}) is not on any net",
        Where(ref, name),
        f"connect {ref}.{name}, or list it under no_connect if it is meant "
        "to be left open",
    )


def _no_connects(grounded: Grounded, findings: list[Finding]) -> set[tuple[str, str]]:
    """The pins the design says are left open, resolved to pin numbers."""
    design = grounded.design
    found: set[tuple[str, str]] = set()
    for written in design.no_connect:
        ref, name = split_pin(written, design.components)
        try:
            pins = resolve_pin(
                grounded.symbols[ref], ref, name, unit=design.components[ref].unit
            )
        except PinError as exc:
            findings.append(
                Finding(
                    "L1",
                    exc.code,
                    "error",
                    str(exc).split(": ", 1)[1],
                    Where(ref, name),
                    "use get_symbol_pins for the exact pin names",
                )
            )
            continue
        found |= {(ref, pin.number) for pin in pins}
    return found


def _decoupling(grounded: Grounded, rules: StructureRules) -> list[Finding]:
    """Every supply pin of an integrated circuit needs a capacitor to ground."""
    design = grounded.design
    grounds = {name for name, net in design.nets.items() if net.role == "ground"}
    # The nets each capacitor joins.
    joined: dict[str, set[str]] = {}
    for pin in grounded.pins:
        if pin.ref.rstrip("0123456789").upper() in rules.capacitor_prefixes:
            joined.setdefault(pin.ref, set()).add(pin.net)
    decoupled = {
        net for nets in joined.values() if nets & grounds for net in nets - grounds
    }
    findings: list[Finding] = []
    reported: set[tuple[str, str]] = set()
    for pin in grounded.pins:
        if pin.ref.rstrip("0123456789").upper() not in rules.ic_prefixes:
            continue
        if pin.pin.electrical_type != "power_in" or pin.net in grounds:
            continue
        if design.nets[pin.net].role != "power" or pin.net in decoupled:
            continue
        if (pin.ref, pin.net) in reported:
            continue
        reported.add((pin.ref, pin.net))
        name = pin_label(pin.pin)
        findings.append(
            Finding(
                "L2",
                "W_DECOUPLING_MISSING",
                "warning",
                f"{pin.ref} pin {name} is supplied from {pin.net}, which has no "
                "capacitor to ground",
                Where(pin.ref, name, pin.net),
                f"add a capacitor between {pin.net} and ground, close to {pin.ref}",
            )
        )
    return findings
