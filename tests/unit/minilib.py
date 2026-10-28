"""A small hand-written symbol library and a design that uses it.

Written in the layout KiCad 10 uses, cut down to a few symbols, so the
grounding, writer and checks can be tested without KiCad installed. The
tests in ``tests/integration`` run the same code on KiCad's own libraries
and let KiCad judge the result.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kairo.library import Library, LibraryIndex

HEADER = (
    '(version 20251024) (generator "kicad_symbol_editor") (generator_version "10.0")'
)


def prop(name: str, value: str, x: float = 0, y: float = 0, hide: bool = False) -> str:
    hidden = " (hide yes)" if hide else ""
    return (
        f'(property "{name}" "{value}" (at {x} {y} 0) (show_name no) '
        f"(do_not_autoplace no){hidden} (effects (font (size 1.27 1.27))))"
    )


#: The body every test part has unless it is given its own.
SQUARE = (
    "(rectangle (start -2.54 2.54) (end 2.54 -2.54) "
    "(stroke (width 0) (type default)) (fill (type none)))"
)


def part(
    name: str,
    reference: str,
    pins: str,
    extra: str = "",
    power: bool = False,
    body: str = SQUARE,
) -> str:
    flags = (
        "(exclude_from_sim no) (in_bom yes) (on_board yes) (in_pos_files yes) "
        "(duplicate_pin_numbers_are_jumpers no)"
    )
    return (
        f'(symbol "{name}" {"(power global) " if power else ""}{flags} '
        f"{prop('Reference', reference, 2.54, 2.54, hide=power)} "
        f"{prop('Value', name, 2.54, -2.54)} "
        f"{prop('Footprint', '', hide=True)} {prop('Datasheet', '', hide=True)} "
        f"{prop('Description', 'test part', hide=True)} "
        f"{prop('ki_keywords', 'test', hide=True)} {extra}"
        f'(symbol "{name}_0_1" {body}) '
        f'(symbol "{name}_1_1" {pins}) (embedded_fonts no))'
    )


def line(*points: tuple[float, float]) -> str:
    xy = " ".join(f"(xy {x} {y})" for x, y in points)
    return f"(polyline (pts {xy}) (stroke (width 0) (type default)) (fill (type none)))"


def pin(kind: str, x: float, y: float, angle: int, name: str, number: str) -> str:
    return (
        f"(pin {kind} line (at {x} {y} {angle}) (length 2.54) "
        f'(name "{name}" (effects (font (size 1.27 1.27)))) '
        f'(number "{number}" (effects (font (size 1.27 1.27)))))'
    )


DEVICE = (
    part(
        "R",
        "R",
        pin("passive", 0, 3.81, 270, "~", "1") + pin("passive", 0, -3.81, 90, "~", "2"),
    ),
    part(
        "C",
        "C",
        pin("passive", 0, 3.81, 270, "~", "1") + pin("passive", 0, -3.81, 90, "~", "2"),
    ),
    part(
        "L",
        "L",
        pin("passive", 0, 3.81, 270, "1", "1") + pin("passive", 0, -3.81, 90, "2", "2"),
    ),
    part(
        "LED",
        "D",
        pin("passive", -3.81, 0, 0, "K", "1") + pin("passive", 3.81, 0, 180, "A", "2"),
    ),
    part(
        "D_Zener",
        "D",
        pin("passive", -3.81, 0, 0, "K", "1") + pin("passive", 3.81, 0, 180, "A", "2"),
    ),
    part(
        "D_TVS",
        "D",
        pin("passive", -3.81, 0, 0, "A1", "1")
        + pin("passive", 3.81, 0, 180, "A2", "2"),
    ),
    part(
        "Conn",
        "J",
        pin("passive", 5.08, 0, 180, "Pin_1", "1")
        + pin("passive", 5.08, -2.54, 180, "Pin_2", "2"),
    ),
    part(
        "QUAD",
        "U",
        pin("power_in", -7.62, 0, 0, "GND", "1")
        + pin("power_in", 7.62, 0, 180, "GND", "2")
        + pin("input", 0, 7.62, 270, "EN", "3"),
    ),
    # A logic gate as KiCad's 74xGxx library draws one: supply pins with
    # names, signal pins without, and a pin typed as not connected.
    part(
        "GATE",
        "U",
        pin("no_connect", -7.62, 2.54, 0, "NC", "1")
        + pin("input", -7.62, 0, 0, "~", "2")
        + pin("power_in", 0, -7.62, 90, "GND", "3")
        + pin("output", 7.62, 0, 180, "~", "4")
        + pin("power_in", 0, 7.62, 270, "VCC", "5"),
    ),
)
REGULATOR = (
    part(
        "REG",
        "U",
        pin("power_in", 0, -7.62, 90, "GND", "1")
        + pin("power_out", 7.62, 0, 180, "VO", "2")
        + pin("power_in", -7.62, 0, 0, "VI", "3"),
    ),
    '(symbol "REG-3.3" (extends "REG") '
    + " ".join(
        [
            prop("Reference", "U", 2.54, 2.54),
            prop("Value", "REG-3.3", 2.54, -2.54),
            prop("Footprint", "Pkg:SOT-223", hide=True),
            prop("Datasheet", "", hide=True),
            prop("Description", "3.3 V test regulator", hide=True),
        ]
    )
    + " (embedded_fonts no))",
    # A charger that sets its currents from resistors on two pins, as TI's
    # BQ24075 does.
    part(
        "CHG",
        "U",
        pin("power_in", -7.62, 2.54, 0, "IN", "1")
        + pin("power_out", 7.62, 2.54, 180, "BAT", "2")
        + pin("passive", -7.62, -2.54, 0, "ISET", "3")
        + pin("passive", 7.62, -2.54, 180, "ILIM", "4")
        + pin("power_in", 0, -7.62, 90, "GND", "5"),
    ),
)
# A step-down converter with the pins of a six-pin part such as TI's
# TPS54202: the switch pin drives, the feedback pin only listens.
SWITCHER = (
    part(
        "BUCK",
        "U",
        pin("power_in", 0, -7.62, 90, "GND", "1")
        + pin("power_out", 7.62, 2.54, 180, "SW", "2")
        + pin("power_in", -7.62, 2.54, 0, "VIN", "3")
        + pin("input", 7.62, -2.54, 180, "FB", "4")
        + pin("input", -7.62, -2.54, 0, "EN", "5")
        + pin("passive", 7.62, 0, 180, "BOOT", "6"),
    ),
    # The same converter as KiCad draws TI's TPS54360: its switch pin typed
    # output rather than power output, beside an open-drain power-good flag.
    part(
        "BUCK_OUT",
        "U",
        pin("power_in", 0, -7.62, 90, "GND", "1")
        + pin("output", 7.62, 2.54, 180, "SW", "2")
        + pin("power_in", -7.62, 2.54, 0, "VIN", "3")
        + pin("input", 7.62, -2.54, 180, "FB", "4")
        + pin("input", -7.62, -2.54, 0, "EN", "5")
        + pin("passive", 7.62, 0, 180, "BOOT", "6")
        + pin("open_collector", 7.62, -5.08, 180, "PG", "7"),
    ),
)
# A battery monitor's pins as TI's BQ76952 types them, cut down, and an
# N-channel FET as KiCad's Transistor_FET library draws one.
MONITOR = (
    part(
        "MON",
        "U",
        pin("power_in", 0, -7.62, 90, "VSS", "1")
        + pin("power_in", -7.62, 2.54, 0, "BAT", "2")
        + pin("input", -7.62, -2.54, 0, "PACK", "3")
        + pin("output", 7.62, 2.54, 180, "CHG", "4")
        + pin("bidirectional", 7.62, -2.54, 180, "TS", "5"),
    ),
    part(
        "NMOS",
        "Q",
        pin("input", -5.08, 0, 0, "G", "1")
        + pin("passive", 2.54, 5.08, 270, "D", "2")
        + pin("passive", 2.54, -5.08, 90, "S", "3"),
    ),
)
# A Schottky diode as KiCad's Diode library draws one, in a three-pin
# package with its middle pin not connected (Diode:BAT54W).
DIODE = (
    part(
        "BAT54W",
        "D",
        pin("passive", -3.81, 0, 0, "A", "1")
        + pin("no_connect", 0, -3.81, 90, "NC", "2")
        + pin("passive", 3.81, 0, 180, "K", "3"),
    ),
)
# A connector in the library KiCad keeps its own in, which tells Kairo
# the net leaves the board.
CONNECTOR = (
    # KiCad keeps its test point here too, which connects nothing.
    part("TestPoint", "TP", pin("passive", 0, 0, 90, "1", "1")),
    part(
        "Conn_01x02",
        "J",
        pin("passive", 5.08, 0, 180, "Pin_1", "1")
        + pin("passive", 5.08, -2.54, 180, "Pin_2", "2"),
    ),
)
POWER = (
    part("GND", "#PWR", pin("power_in", 0, 0, 270, "~", "1"), power=True),
    part("+3V3", "#PWR", pin("power_in", 0, 0, 90, "~", "1"), power=True),
    part("PWR_FLAG", "#FLG", pin("power_out", 0, 0, 90, "~", "1"), power=True),
    # Two drawn the way KiCad's own are: a ground that hangs below its pin
    # and a supply that stands above it.
    part(
        "GNDA",
        "#PWR",
        pin("power_in", 0, 0, 270, "~", "1"),
        power=True,
        body=line((0, 0), (0, -1.27), (1.27, -1.27), (0, -2.54), (-1.27, -1.27)),
    ),
    part(
        "+5V",
        "#PWR",
        pin("power_in", 0, 0, 90, "~", "1"),
        power=True,
        body=line((-0.762, 1.27), (0, 2.54), (0.762, 1.27)) + line((0, 0), (0, 2.54)),
    ),
)
FOOTPRINT = (
    '(footprint "{name}" (version 20260206) (generator "pcbnew") '
    '(generator_version "10.0") (layer "F.Cu") (descr "test") (tags "test") '
    "{pads})"
)
PAD = '(pad "{number}" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))'
# The SOT-223 has a tab that shares pad 2's number, as KiCad's own does.
PADS = {"SOT-223": "1 2 3 2", "R_0603": "1 2", "C_0805": "1 2"}


def make_index(folder: Path) -> LibraryIndex:
    """Index the small libraries, written into ``folder``."""
    libraries: list[Library] = []
    for nickname, symbols in (
        ("Device", DEVICE),
        ("Reg", REGULATOR),
        ("Sw", SWITCHER),
        ("Mon", MONITOR),
        ("Diode", DIODE),
        ("Connector", CONNECTOR),
        ("power", POWER),
    ):
        path = folder / f"{nickname}.kicad_sym"
        path.write_text(
            f"(kicad_symbol_lib {HEADER} {' '.join(symbols)})", encoding="utf-8"
        )
        libraries.append(Library("symbol", nickname, path))
    pretty = folder / "Pkg.pretty"
    pretty.mkdir()
    for name, numbers in PADS.items():
        pads = " ".join(PAD.format(number=number) for number in numbers.split())
        (pretty / f"{name}.kicad_mod").write_text(
            FOOTPRINT.format(name=name, pads=pads), encoding="utf-8"
        )
    libraries.append(Library("footprint", "Pkg", pretty))
    index = LibraryIndex()
    report = index.update(libraries, workers=1)
    assert not report.problems, report.problems
    return index


def ldo() -> dict[str, Any]:
    """The plan's regulator example, on the small libraries."""
    return {
        "design": "ldo_3v3",
        # Only what the design states: these tests are of what a missing
        # figure does. Kairo's default margins have tests of their own.
        "policy": {"margins": "stated"},
        "nets": {
            "VIN_5V": {"role": "power", "voltage": "5V"},
            "+3V3": {"role": "power", "voltage": "3.3V"},
            "GND": {"role": "ground"},
            "LED_A": {"role": "signal"},
        },
        "components": {
            "J1": {"lib_id": "Device:Conn"},
            "U1": {
                "lib_id": "Reg:REG-3.3",
                "footprint": "Pkg:SOT-223",
                "params": {
                    "regulator": "linear",
                    "i_out_max": "1A",
                    "v_dropout": "1.3V",
                    "theta_ja": "90degC/W",
                    "source": "user-stated",
                },
            },
            "C1": {"lib_id": "Device:C", "value": "10uF", "footprint": "Pkg:C_0805"},
            "C2": {"lib_id": "Device:C", "value": "22uF", "footprint": "Pkg:C_0805"},
            "R1": {
                "lib_id": "Device:R",
                "value": "1k",
                "params": {"p_rating": "0.125W", "tolerance": "1%"},
                "footprint": "Pkg:R_0603",
            },
            "D1": {"lib_id": "Device:LED", "params": {"v_f": "2.0V"}},
        },
        "connections": {
            "VIN_5V": ["J1.Pin_1", "U1.VI", "C1.1"],
            "+3V3": ["U1.VO", "C2.1", "R1.1"],
            "LED_A": ["R1.2", "D1.A"],
            "GND": ["J1.Pin_2", "U1.GND", "C1.2", "C2.2", "D1.K"],
        },
        "loads": [{"net": "+3V3", "current": "300mA", "note": "MCU budget"}],
        "requirements": [
            {
                "type": "max_junction_temp",
                "component": "U1",
                "ambient": "40degC",
                "limit": "125degC",
            }
        ],
        "blocks": {
            "power_in": ["J1", "C1"],
            "regulator": ["U1", "C2"],
            "indicator": ["R1", "D1"],
        },
        "placement": {"order": ["power_in", "regulator", "indicator"]},
    }


def buck() -> dict[str, Any]:
    """A 12 V to 5 V step-down stage, with the TPS54202 datasheet's values.

    The reference is 0.596 V within 2.52%, the least switching frequency
    390 kHz and the least current limit 2.5 A (SLVSD26C, page 5); the
    divider is the datasheet's own 100k over 13.3k.
    """
    return {
        "design": "buck",
        "policy": {"margins": "stated"},
        "nets": {
            "VIN": {"role": "power", "voltage": "12V", "tolerance": "5%"},
            "+5V": {"role": "power", "voltage": "5V"},
            "GND": {"role": "ground"},
            "SW": {"role": "signal"},
            "BOOT": {"role": "signal"},
            "FB": {"role": "signal"},
        },
        "components": {
            "U1": {
                "lib_id": "Sw:BUCK",
                "params": {
                    "regulator": "switching",
                    "v_ref": "0.596V",
                    "v_ref_tolerance": "2.52%",
                    "f_sw": "390kHz",
                    "i_limit": "2.5A",
                    "i_out_max": "2A",
                },
                "source": "test values after TI SLVSD26C",
                "limits": {"f_sw": "min", "i_limit": "min"},
            },
            "C1": {"lib_id": "Device:C", "value": "10uF"},
            "C2": {"lib_id": "Device:C", "value": "100nF"},
            "L1": {
                "lib_id": "Device:L",
                "value": "15uH",
                "params": {"tolerance": "20%", "i_sat": "3.5A"},
            },
            "C3": {
                "lib_id": "Device:C",
                "value": "47uF",
                "params": {
                    "tolerance": "20%",
                    "dielectric": "polymer",
                    "esr_max": "40mohm",
                },
            },
            "R1": {
                "lib_id": "Device:R",
                "value": "100k",
                "params": {"tolerance": "1%"},
            },
            "R2": {
                "lib_id": "Device:R",
                "value": "13.3k",
                "params": {"tolerance": "1%"},
            },
        },
        "connections": {
            "VIN": ["U1.VIN", "C1.1"],
            "SW": ["U1.SW", "L1.1", "C2.2"],
            "BOOT": ["U1.BOOT", "C2.1"],
            "+5V": ["L1.2", "C3.1", "R1.1"],
            "FB": ["R1.2", "R2.1", "U1.FB"],
            "GND": ["U1.GND", "C1.2", "C3.2", "R2.2"],
        },
        "no_connect": ["U1.EN"],
        "loads": [{"net": "+5V", "current": "1.5A", "note": "the load"}],
        "requirements": [
            {"type": "rail_voltage", "net": "+5V", "nominal": "5V", "tolerance": "7%"},
            {"type": "max_inductor_current", "component": "L1"},
            {"type": "max_ripple", "net": "+5V", "value": "50mV"},
        ],
    }
