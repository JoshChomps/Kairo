"""Which library symbols are plain parts whose behaviour their value gives.

A resistor, capacitor, inductor or diode is recognised by its symbol, never
by its reference letter: a part called C5 can be anything.
"""

from __future__ import annotations

#: Symbols simulated as an ideal resistor, capacitor or inductor.
PASSIVES: dict[str, tuple[str, str]] = {
    **dict.fromkeys(
        ("Device:R", "Device:R_Small", "Device:R_US", "Device:R_Small_US"),
        ("R", "ohm"),
    ),
    **dict.fromkeys(
        (
            "Device:C",
            "Device:C_Small",
            "Device:C_Polarized",
            "Device:C_Polarized_Small",
            "Device:C_Polarized_US",
        ),
        ("C", "F"),
    ),
    **dict.fromkeys(("Device:L", "Device:L_Small"), ("L", "H")),
}
#: Symbols simulated as a fixed forward drop when the design states one.
DIODES = frozenset(
    {
        "Device:LED",
        "Device:LED_Small",
        "Device:D",
        "Device:D_Small",
        "Device:D_Schottky",
        "Device:D_Schottky_Small",
    }
)
#: Libraries whose symbols are places to connect, with nothing inside.
TERMINAL_LIBRARIES = frozenset({"Connector", "Connector_Generic", "TestPoint"})
