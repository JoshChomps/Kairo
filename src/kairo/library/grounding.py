"""Ground a whole design in the installed libraries.

Every symbol, footprint and pin a design names is looked up before
anything is written. What does not exist comes back as a finding that says
what does, and nothing is written.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping
from dataclasses import dataclass

from kairo.ir import Design, split_pin
from kairo.library.index import LibraryIndex
from kairo.library.reader import Footprint, Pin, Symbol
from kairo.library.resolve import PinError, resolve_pin
from kairo.verify.findings import Finding, Where


@dataclass(frozen=True, slots=True)
class GroundedPin:
    """A pin of a part, on a net.

    Attributes:
        ref: The part's reference.
        pin: The library pin.
        net: The net the design puts it on.
    """

    ref: str
    pin: Pin
    net: str


@dataclass(frozen=True, slots=True)
class Grounded:
    """A design with every name resolved.

    Attributes:
        design: The design.
        symbols: Each part's symbol, by reference.
        pins: Every connected pin, in the design's order.
        open_pins: Pins left open on purpose, each with its name and type,
            where no symbol gives them (a drawn schematic); their net is
            empty.
    """

    design: Design
    symbols: dict[str, Symbol]
    pins: tuple[GroundedPin, ...]
    open_pins: tuple[GroundedPin, ...] = ()

    def net_pins(self, net: str) -> list[GroundedPin]:
        """The pins on one net, in the design's order.

        Args:
            net: The net's name.

        Returns:
            The pins.
        """
        return [pin for pin in self.pins if pin.net == net]


class GroundingError(ValueError):
    """A design names things the libraries do not have.

    Attributes:
        findings: One finding per problem.
    """

    def __init__(self, findings: list[Finding]) -> None:
        """Record the findings.

        Args:
            findings: What did not resolve.
        """
        super().__init__("; ".join(finding.message for finding in findings))
        self.findings = findings


def ground(
    design: Design,
    index: LibraryIndex,
    *,
    aliases: Mapping[str, str] | None = None,
) -> Grounded:
    """Resolve every symbol, footprint and pin of a design.

    Args:
        design: The design.
        index: The library index.
        aliases: Approved other names for pins.

    Returns:
        The design with its symbols and pins.

    Raises:
        GroundingError: If anything does not resolve; every problem found
            is in its findings, not only the first.
    """
    findings: list[Finding] = []
    symbols: dict[str, Symbol] = {}
    unusable: set[str] = set()
    for ref, part in design.components.items():
        symbol = index.symbol(part.lib_id)
        if symbol is None:
            library, _, name = part.lib_id.partition(":")
            near = [
                f"{library}:{n}"
                for n in difflib.get_close_matches(name, index.symbol_names(library), 5)
            ]
            findings.append(
                Finding(
                    "L1",
                    "E_SYMBOL_UNKNOWN",
                    "error",
                    f"{ref}: the libraries have no symbol {part.lib_id!r}. "
                    f"Closest: {near or 'none; use find_symbol to search'}.",
                    Where(ref=ref),
                    "use find_symbol, then give the exact Library:Name",
                )
            )
            continue
        symbols[ref] = symbol
        if part.unit > symbol.unit_count:
            # Its pins cannot be looked up in a unit that does not exist.
            unusable.add(ref)
            findings.append(
                Finding(
                    "L1",
                    "E_UNIT_UNKNOWN",
                    "error",
                    f"{ref}: {part.lib_id} has {symbol.unit_count} unit(s); the "
                    f"design asks for unit {part.unit}.",
                    Where(ref=ref),
                )
            )
        footprint = part.footprint or symbol.footprint
        if not footprint:
            continue
        found_footprint = index.footprint(footprint)
        if found_footprint is None:
            findings.append(
                Finding(
                    "L1",
                    "E_FOOTPRINT_UNKNOWN",
                    "error",
                    f"{ref}: the libraries have no footprint {footprint!r}.",
                    Where(ref=ref),
                    "use find_footprint, then give the exact Library:Name",
                )
            )
            continue
        findings += pin_pad_findings(
            ref, part.lib_id, symbol, footprint, found_footprint
        )

    pins: list[GroundedPin] = []
    taken: dict[tuple[str, str], str] = {}
    for net, names in design.connections.items():
        for written in names:
            ref, name = split_pin(written, design.components)
            if ref not in symbols or ref in unusable:
                continue
            try:
                found = resolve_pin(
                    symbols[ref],
                    ref,
                    name,
                    unit=design.components[ref].unit,
                    aliases=aliases,
                )
            except PinError as exc:
                findings.append(
                    Finding(
                        "L1",
                        exc.code,
                        "error",
                        str(exc).split(": ", 1)[1],
                        Where(ref=ref, pin=name, net=net),
                        "use get_symbol_pins for the exact pin names",
                    )
                )
                continue
            for pin in found:
                other = taken.get((ref, pin.number))
                if other is not None and other != net:
                    findings.append(
                        Finding(
                            "L1",
                            "E_PIN_ON_TWO_NETS",
                            "error",
                            f"{ref} pin {pin.number} ({pin.name or 'no name'}) is "
                            f"on both {other} and {net}.",
                            Where(ref=ref, pin=name, net=net),
                        )
                    )
                    continue
                if other is None:
                    taken[(ref, pin.number)] = net
                    pins.append(GroundedPin(ref, pin, net))
    if findings:
        raise GroundingError(findings)
    return Grounded(design, symbols, tuple(pins))


def pin_pad_findings(
    ref: str, lib_id: str, symbol: Symbol, footprint_id: str, footprint: Footprint
) -> list[Finding]:
    """Check that every pin of a symbol has a pad of its number.

    A pin with no pad of the same number cannot be connected on the board,
    which is what a wrong footprint choice looks like at this stage. Pads
    with no pin are allowed: a tab or shield pad often has none.

    Args:
        ref: The part's reference.
        lib_id: The symbol's library ID.
        symbol: The symbol.
        footprint_id: The footprint's library ID.
        footprint: The footprint.

    Returns:
        One finding if any pin lacks a pad, otherwise nothing.
    """
    pads = {pad.number for pad in footprint.pads if pad.number}
    missing = sorted(
        {pin.number for pin in symbol.pins if pin.number and pin.number not in pads}
    )
    if not missing:
        return []
    return [
        Finding(
            "L1",
            "E_PIN_PAD_MISMATCH",
            "error",
            f"{ref}: {lib_id} has pin(s) {', '.join(missing)} with no pad of "
            f"that number on {footprint_id} (its pads: "
            f"{', '.join(sorted(pads)) or 'none'}).",
            Where(ref=ref),
            "choose a footprint whose pad numbers match the symbol's pins",
        )
    ]
