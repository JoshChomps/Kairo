"""Layer L3: does KiCad see the circuit the design describes?

The referee is KiCad's own netlist of the written schematic, never
Kairo's idea of what it wrote. Kairo sets every reference itself, so the
two circuits are the same exactly when they have the same parts and each
net holds the same pins; no graph matching is needed.
"""

from __future__ import annotations

from collections.abc import Mapping

from kairo.kicad.netlist import Netlist
from kairo.library.grounding import Grounded
from kairo.verify.findings import Finding, Where


def compare_netlist(
    grounded: Grounded, net_names: Mapping[str, str], netlist: Netlist
) -> list[Finding]:
    """Compare a grounded design with KiCad's netlist of its schematic.

    Args:
        grounded: The design with its pins resolved.
        net_names: The name KiCad gives each of the design's nets.
        netlist: KiCad's netlist of the written schematic.

    Returns:
        One finding per difference; empty when the circuits are the same.
    """
    findings: list[Finding] = []
    design = grounded.design
    wanted_parts = set(design.components)
    got_parts = {component.ref for component in netlist.components}
    for ref in sorted(wanted_parts - got_parts):
        findings.append(
            Finding(
                "L3",
                "E_PART_MISSING",
                "error",
                f"{ref} is in the design but not in KiCad's netlist",
                Where(ref=ref),
            )
        )
    for ref in sorted(got_parts - wanted_parts):
        findings.append(
            Finding(
                "L3",
                "E_PART_EXTRA",
                "error",
                f"{ref} is in KiCad's netlist but not in the design",
                Where(ref=ref),
            )
        )

    wanted_net: dict[tuple[str, str], str] = {
        (pin.ref, pin.pin.number): pin.net for pin in grounded.pins
    }
    got_net: dict[tuple[str, str], str] = {
        (node.ref, node.pin): net.name for net in netlist.nets for node in net.nodes
    }
    for (ref, number), net in sorted(wanted_net.items()):
        got = got_net.get((ref, number))
        if got != net_names[net]:
            findings.append(
                Finding(
                    "L3",
                    "E_PIN_NET_DIFFERS",
                    "error",
                    f"{ref} pin {number} should be on {net} (KiCad's "
                    f"{net_names[net]}); KiCad has it on {got or 'no net'}",
                    Where(ref=ref, pin=number, net=net),
                )
            )
    for (ref, number), got in sorted(got_net.items()):
        if (ref, number) not in wanted_net and ref in wanted_parts:
            # KiCad lists an unconnected pin on a net of its own, named
            # after the pin; that is not a connection the design lacks.
            alone = sum(1 for other in got_net.values() if other == got) == 1
            if not alone:
                findings.append(
                    Finding(
                        "L3",
                        "E_PIN_CONNECTED_UNASKED",
                        "error",
                        f"{ref} pin {number} is on {got} in KiCad's netlist; "
                        "the design connects it to nothing",
                        Where(ref=ref, pin=number, net=got),
                    )
                )
    return findings
