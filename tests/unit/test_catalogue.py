"""The parts catalogue: reading a snapshot, finding parts, filling a design.

The snapshot here is a few rows written by hand in the layout of the
jlcparts snapshot fetched on 2026-10-11, with listings copied from it.
No test fetches anything.
"""

# These tests change plain JSON data, which a strict type checker cannot follow.
# pyright: reportUnknownLambdaType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

import json
import sqlite3
import struct
import sys
import zlib
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from minilib import ldo, make_index

from kairo.design_tools import DesignTools
from kairo.kicad.cli import KicadCli
from kairo.kicad.drawn import part_numbers, read_drawn
from kairo.kicad.netlist import Component, Net, Netlist, Node
from kairo.parts.catalogue import (
    FILE_NAME,
    Catalogue,
    CatalogueError,
    build,
    quantity,
    read_listing,
    unpack,
)
from kairo.parts.fill import fill
from kairo.server import Settings, Workspace

FAKE = Path(__file__).resolve().parent.parent / "fakes" / "fake_kicad_cli.py"
RESISTORS = "Chip Resistor - Surface Mount"
MLCC = "Multilayer Ceramic Capacitors MLCC - SMD/SMT"
LDO = "Voltage Regulators - Linear, Low Drop Out (LDO) Regulators"
# (lcsc, part number, maker, subcategory, package, attributes, stock, library)
LISTINGS: list[tuple[int, str, str, str, str, dict[str, str], int, str]] = [
    (
        7334661,
        "RC0603FR-0710K2P",
        "YAGEO",
        RESISTORS,
        "0603",
        {
            "Power(Watts)": "100mW",
            "Resistance": "10.2kΩ",
            "Temperature Coefficient": "±100ppm/℃",
            "Tolerance": "±1%",
        },
        0,
        "expand",
    ),
    (
        20144536,
        "AT0603FRD0710KL",
        "YAGEO",
        RESISTORS,
        "0603",
        {
            "Power(Watts)": "100mW",
            "Resistance": "10kΩ",
            "Temperature Coefficient": "±25ppm/℃",
            "Tolerance": "±1%",
        },
        17805,
        "expand",
    ),
    (
        25804,
        "0603WAF1002T5E",
        "UNI-ROYAL",
        RESISTORS,
        "0603",
        {"Power(Watts)": "100mW", "Resistance": "10kΩ", "Tolerance": "±1%"},
        900000,
        "base",
    ),
    (
        99001,
        "RC0603JR-0710KL",
        "YAGEO",
        RESISTORS,
        "0603",
        {"Power(Watts)": "100mW", "Resistance": "10kΩ", "Tolerance": "±5%"},
        5000,
        "expand",
    ),
    # The same part number listed twice: the one with more stock is used.
    (
        99002,
        "RC0603JR-0710KL",
        "YAGEO",
        RESISTORS,
        "0603",
        {"Resistance": "10kΩ"},
        10,
        "expand",
    ),
    (
        16194178,
        "CL10B104KB8SFNC",
        "Samsung Electro Mechanics",
        MLCC,
        "0603",
        {
            "Capacitance": "100nF",
            "Temperature Coefficient": "X7R",
            "Tolerance": "±10%",
            "Voltage Rating": "50V",
        },
        19296,
        "expand",
    ),
    # A tolerance that is not one figure for both sides is not read as one.
    (
        99003,
        "Y5V-EXAMPLE",
        "Example",
        MLCC,
        "0603",
        {
            "Capacitance": "100nF",
            "Temperature Coefficient": "Y5V",
            "Tolerance": "-20%~+80%",
            "Voltage Rating": "16V",
        },
        100,
        "expand",
    ),
    (
        6387914,
        "CA45-B016K476T",
        "Xiangyee",
        "Tantalum Capacitors",
        "CASE-B",
        {
            "Capacitance": "47uF",
            "Equivalent Series Resistance(ESR)": "2.5Ω@100kHz",
            "Tolerance": "±10%",
            "Voltage Rating": "16V",
        },
        3000,
        "expand",
    ),
    (
        6539828,
        "DFE21CCNR33MELL",
        "Murata",
        "Power Inductors",
        "0805",
        {
            "Current - Saturation(Isat)": "5.4A",
            "Current Rating": "4.6A",
            "DC Resistance(DCR)": "23mΩ",
            "Inductance": "330nH",
            "Tolerance": "±20%",
        },
        4000,
        "expand",
    ),
    (
        6390388,
        "150060VS55040",
        "Wurth",
        "LED Indication - Discrete",
        "0603",
        {"Test Current": "20mA", "Voltage - Forward(Vf)": "2V~2.4V"},
        2000,
        "expand",
    ),
    # A listing with no attributes gives no value and is left out.
    (99004, "NO-FIGURES", "Example", RESISTORS, "0603", {}, 50, "expand"),
    # A kind Kairo keeps no figures for.
    (
        99005,
        "LM358",
        "Example",
        "Operational Amplifier",
        "SOP-8",
        {"Number of Channels": "2"},
        9,
        "expand",
    ),
    # Regulators, with listings copied from the snapshot of 2026-10-11.
    (
        30001,
        "RT9013-33GB-MS",
        "MSKSEMI",
        LDO,
        "SOT-23-5",
        {
            "Output Configuration": "Positive",
            "Output Current": "500mA",
            "Output Type": "Fixed",
            "Output Voltage": "3.3V",
            "Voltage - Supply": "5.5V",
            "Voltage Dropout": "300mV@(200mA), 100mV@(100mA)",
            "standby current": "60uA",
        },
        117515,
        "expand",
    ),
    (
        30002,
        "AMS1117-3.3",
        "TECH PUBLIC",
        LDO,
        "SOT-223",
        {
            "Output Configuration": "Positive",
            "Output Current": "1A",
            "Output Type": "Fixed",
            "Output Voltage": "3.3V",
            "Voltage - Supply": "12V",
            "Voltage Dropout": "1.3V@(1A)",
            "standby current": "4mA",
        },
        107131,
        "expand",
    ),
    # A negative regulator is not kept.
    (
        30003,
        "NEG-EXAMPLE",
        "Example",
        LDO,
        "SOT-223",
        {"Output Configuration": "Negative", "Output Voltage": "5V"},
        5,
        "expand",
    ),
    (
        30004,
        "TPS562242DRLR",
        "Texas Instruments",
        "DC-DC Converters",
        "SOT-563-6",
        {
            "Frequency - Switching": "1.4MHz",
            "Output Current": "2A",
            "Output Type": "Adjustable",
            "Output Voltage": "800mV~10V",
            "Quiescent Current": "100uA",
            "Topology": "Buck",
            "Voltage - Supply": "3V~17V",
        },
        36897,
        "expand",
    ),
    (
        30005,
        "HT7183SPER",
        "Jiaxing Heroic Elec Tech",
        "DC-DC Converters",
        "ESOP-8",
        {
            "Frequency - Switching": "800kHz",
            "Output Current": "4.5A",
            "Output Voltage": "600mV~16V",
            "Quiescent Current": "1mA, 100uA",
            "Topology": "Boost",
            "Voltage - Supply": "2.6V~5.5V",
        },
        3063,
        "expand",
    ),
    # A topology Kairo has no check for is not kept.
    (
        30006,
        "FLYBACK-EXAMPLE",
        "Example",
        "DC-DC Converters",
        "SOP-8",
        {"Output Voltage": "5V", "Topology": "Flyback"},
        7,
        "expand",
    ),
]


def snapshot(folder: Path) -> Path:
    path = folder / "cache.sqlite3"
    db = sqlite3.connect(path)
    db.execute("create table meta (key text primary key, value text not null)")
    db.execute("insert into meta values ('format', 'source-db-v2')")
    db.execute(
        "create table jlc_components (lcsc integer primary key, fetched_at integer, "
        "category text, subcategory text, mfr text, package text, manufacturer text, "
        "library_type text, datasheet text, stock integer, attributes text, "
        "preferred integer)"
    )
    for lcsc, mpn, maker, subcategory, package, attributes, stock, library in LISTINGS:
        db.execute(
            "insert into jlc_components values (?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                lcsc,
                1791676800,
                subcategory,
                mpn,
                package,
                maker,
                library,
                f"https://example.invalid/{mpn}.pdf",
                stock,
                json.dumps(attributes, ensure_ascii=False),
                # One part is preferred without being basic.
                1 if mpn == "AT0603FRD0710KL" else 0,
            ),
        )
    db.commit()
    db.close()
    return path


@pytest.fixture
def catalogue(tmp_path: Path) -> Catalogue:
    target = tmp_path / "catalogue.sqlite3"
    built = build(snapshot(tmp_path), target)
    assert built.kept == {
        "resistor": 5,
        "capacitor": 3,
        "inductor": 1,
        "led": 1,
        "regulator": 4,
    }
    # The resistor with no attributes, and the two regulators not kept.
    assert built.without_figures == 3
    return Catalogue(target)


def test_a_listed_figure_is_read_or_left_out_never_guessed() -> None:
    assert quantity("10.2kΩ", "Ω") == pytest.approx(10200.0)
    assert quantity("100mW", "W") == pytest.approx(0.1)
    assert quantity("330nH", "H") == pytest.approx(330e-9)
    # What follows the figure is left aside; another unit is not read.
    assert quantity("2.5Ω@100kHz", "Ω") == pytest.approx(2.5)
    assert quantity("50V", "A") is None and quantity(None, "V") is None
    assert quantity("about 5V", "V") is None

    resistor = read_listing(RESISTORS, LISTINGS[0][5])
    assert resistor == {
        "kind": "resistor",
        "tolerance": 1.0,
        "value": pytest.approx(10200.0),
        "p_rating": pytest.approx(0.1),
        "tempco": 100.0,
    }
    one_sided = read_listing(MLCC, LISTINGS[6][5])
    assert one_sided is not None
    assert one_sided["tolerance"] is None and one_sided["dielectric"] == "Y5V"
    tantalum = read_listing("Tantalum Capacitors", LISTINGS[7][5])
    assert tantalum is not None
    assert tantalum["dielectric"] == "tantalum" and tantalum["esr"] == 2.5
    led = read_listing("LED Indication - Discrete", LISTINGS[9][5])
    # 2 V to 2.4 V is 2.2 V within 9.09%.
    assert led is not None and led["v_f"] == pytest.approx(2.2)
    assert led["v_f_tolerance"] == pytest.approx(9.091)
    assert read_listing("Operational Amplifier", {"Number of Channels": "2"}) is None


def test_the_split_archive_is_unpacked_and_checked(tmp_path: Path) -> None:
    content = bytes(range(256)) * 4000
    packer = zlib.compressobj(9, zlib.DEFLATED, -15)
    packed = packer.compress(content) + packer.flush()
    name = b"cache.sqlite3"

    def archive(crc: int) -> bytes:
        header = struct.pack(
            "<HHHHHIIIHH", 20, 0, 8, 0, 0, crc, len(packed), len(content), len(name), 0
        )
        return b"PK\x07\x08PK\x03\x04" + header + name + packed + b"PK\x01\x02rest"

    def pieces(data: bytes) -> list[Path]:
        third = len(data) // 3
        paths = [tmp_path / n for n in ("a.z01", "a.z02", "a.zip")]
        for path, part in zip(
            paths,
            (data[:third], data[third : 2 * third], data[2 * third :]),
            strict=True,
        ):
            path.write_bytes(part)
        return paths

    target = tmp_path / "out" / "cache.sqlite3"
    assert unpack(pieces(archive(zlib.crc32(content))), target) == len(content)
    assert target.read_bytes() == content
    # A wrong checksum, a missing piece and a file that is no archive.
    with pytest.raises(CatalogueError, match="checksum"):
        unpack(pieces(archive(1)), target)
    assert not target.exists()
    with pytest.raises(CatalogueError, match="a piece is missing"):
        unpack(pieces(archive(zlib.crc32(content)))[:1], target)
    with pytest.raises(CatalogueError, match="not a zip archive"):
        unpack([FAKE], target)


def test_a_part_is_found_by_its_number_whatever_the_case(catalogue: Catalogue) -> None:
    assert catalogue.date == "2026-10-11"
    part = catalogue.lookup("cl10b104kb8sfnc")
    assert part is not None and part.lcsc == 16194178
    assert (part.value, part.tolerance, part.v_rating, part.dielectric) == (
        pytest.approx(100e-9),
        10.0,
        50.0,
        "X7R",
    )
    assert catalogue.lookup("C16194178") == part
    # Listed twice: the listing with more stock.
    twice = catalogue.lookup("RC0603JR-0710KL")
    assert twice is not None and twice.lcsc == 99001
    assert catalogue.lookup("NO-SUCH-PART") is None
    assert catalogue.lookup("NO-FIGURES") is None
    with pytest.raises(CatalogueError, match="not a parts catalogue"):
        Catalogue(FAKE)


def test_parts_of_one_value_are_found_basic_first_then_by_stock(
    catalogue: Catalogue,
) -> None:
    found, total = catalogue.find("resistor", 10e3, package="0603")
    assert total == 4
    assert [p.mpn for p in found] == [
        "0603WAF1002T5E",
        "AT0603FRD0710KL",
        "RC0603JR-0710KL",
        "RC0603JR-0710KL",
    ]
    # A basic part and a preferred one both count; the rest do not.
    assert [p.basic for p in found] == [True, True, False, False]
    # A widest tolerance leaves out wider parts, and parts that give none.
    tight, total = catalogue.find("resistor", 10e3, tolerance=1.0)
    assert total == 2 and {p.mpn for p in tight} == {
        "0603WAF1002T5E",
        "AT0603FRD0710KL",
    }
    # 10.2 kilohms is another value.
    assert catalogue.find("resistor", 10.2e3)[1] == 1
    rated, _ = catalogue.find("capacitor", 100e-9, minimums={"v_rating": 25.0})
    assert [p.mpn for p in rated] == ["CL10B104KB8SFNC"]
    assert catalogue.find("capacitor", 100e-9, dielectric="y5v")[1] == 1
    assert catalogue.find("inductor", 330e-9, minimums={"i_sat": 6.0})[1] == 0
    leds, _ = catalogue.find("led", 2.0)
    assert [p.mpn for p in leds] == ["150060VS55040"]
    assert len(catalogue.find("resistor", 10e3, limit=1)[0]) == 1
    with pytest.raises(CatalogueError, match="not a rating"):
        catalogue.find("resistor", 10e3, minimums={"stock": 1.0})


def design() -> dict[str, Any]:
    data = ldo()
    data["policy"] = {}
    data["components"]["R1"] = {
        "lib_id": "Device:R",
        "value": "10k",
        "params": {"mpn": "AT0603FRD0710KL"},
        "footprint": "Pkg:R_0603",
    }
    data["components"]["C2"]["params"] = {"mpn": "CL10B104KB8SFNC"}
    data["components"]["C2"]["value"] = "100nF"
    data["components"]["D1"]["params"]["mpn"] = "150060VS55040"
    return data


def test_a_named_part_takes_the_catalogues_figures_where_none_are_stated(
    catalogue: Catalogue,
) -> None:
    before = design()
    filled, notes = fill(before, catalogue)
    assert before == design(), "the design given is not changed"
    resistor = filled["components"]["R1"]
    assert resistor["params"] == {
        "mpn": "AT0603FRD0710KL",
        "tolerance": "1%",
        "p_rating": "0.1W",
        "tempco": "25ppm/degC",
    }
    assert resistor["source"] == (
        "user-stated; LCSC's catalogue entry C20144536 for AT0603FRD0710KL (read "
        "2026-10-11, not checked against the datasheet): tolerance, p_rating, tempco"
    )
    capacitor = filled["components"]["C2"]["params"]
    assert (capacitor["tolerance"], capacitor["v_rating"], capacitor["dielectric"]) == (
        "10%",
        "50V",
        "X7R",
    )
    # The LED states its forward voltage, which is kept; its spread is taken.
    led = filled["components"]["D1"]["params"]
    assert led["v_f"] == "2.0V" and led["v_f_tolerance"] == "9.09%"
    assert notes.filled == [
        "C2 (CL10B104KB8SFNC): tolerance, v_rating, dielectric",
        "R1 (AT0603FRD0710KL): tolerance, p_rating, tempco",
        "D1 (150060VS55040): v_f_tolerance",
    ]
    assert notes.not_found == [] and notes.mismatched == []

    # A stated figure is never replaced.
    stated = design()
    stated["components"]["R1"]["params"]["tolerance"] = "0.5%"
    kept, _ = fill(stated, catalogue)
    assert kept["components"]["R1"]["params"]["tolerance"] == "0.5%"
    # A part with no value takes the catalogue's.
    bare = design()
    del bare["components"]["R1"]["value"]
    valued, notes = fill(bare, catalogue)
    assert valued["components"]["R1"]["value"] == "10000ohm"
    assert notes.filled[1].endswith("value, tolerance, p_rating, tempco")


def test_a_part_number_that_is_another_part_is_reported_and_gives_nothing(
    catalogue: Catalogue,
) -> None:
    wrong = design()
    # Marked 10k, with the part number of a 10.2k part.
    wrong["components"]["R1"]["params"]["mpn"] = "RC0603FR-0710K2P"
    # A capacitor with a resistor's part number.
    wrong["components"]["C2"]["params"]["mpn"] = "0603WAF1002T5E"
    wrong["components"]["D1"]["params"]["mpn"] = "NOT-IN-THE-CATALOGUE"
    filled, notes = fill(wrong, catalogue)
    assert notes.mismatched == [
        "C2 is drawn as a capacitor and its part number 0603WAF1002T5E is a "
        "resistor in the catalogue; nothing was taken from it",
        "R1 is marked 10k and its part number RC0603FR-0710K2P is a 10200ohm part "
        "in the catalogue; nothing was taken from it",
    ]
    assert notes.not_found == ["D1: NOT-IN-THE-CATALOGUE"]
    assert notes.filled == []
    assert filled["components"]["R1"]["params"] == {"mpn": "RC0603FR-0710K2P"}
    assert next(iter(notes.as_dict())) == "mismatched"
    # A regulator's part number is not looked up: the catalogue has no chips.
    chip = design()
    chip["components"]["U1"]["params"]["mpn"] = "0603WAF1002T5E"
    assert fill(chip, catalogue)[1].mismatched == []


def tools_with(tmp_path: Path, with_catalogue: bool) -> DesignTools:
    libraries = tmp_path / "libraries"
    libraries.mkdir(parents=True)
    root = tmp_path / "root"
    root.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    if with_catalogue:
        build(snapshot(tmp_path), cache / FILE_NAME)
    settings = Settings((root,), cache, tier="write")
    cli = KicadCli((sys.executable, str(FAKE), "silent"), temp_root=tmp_path)
    return DesignTools(Workspace(settings, cli=cli, index=make_index(libraries)))


def test_find_part_gives_a_short_list_a_model_can_choose_from(tmp_path: Path) -> None:
    tools = tools_with(tmp_path, with_catalogue=True)
    found = tools.find_part("resistor", "10k", package="0603", tolerance="1%")
    assert (found["matched"], found["listed"], found["read_on"]) == (2, 2, "2026-10-11")
    first = found["parts"][0]
    assert first == {
        "mpn": "0603WAF1002T5E",
        "lcsc": "C25804",
        "manufacturer": "UNI-ROYAL",
        "package": "0603",
        "value": "10000 ohm",
        "tolerance": "1%",
        "p_rating": "0.1 W",
        "stock": 900000,
        "basic_part": True,
    }
    coil = tools.find_part("inductor", "330nH", i_sat="5A")["parts"][0]
    assert (coil["i_sat"], coil["dcr"]) == ("5.4 A", "0.023 ohm")
    nothing = tools.find_part("capacitor", "1uF")
    assert nothing["parts"] == [] and "Nothing matched" in nothing["notes"][1]
    for arguments, says in (
        (("transistor", "10k"), "kind is one of"),
        (("resistor", "ten"), "value"),
        (("resistor", "10V"), "value"),
    ):
        with pytest.raises(ToolError, match=f"E_PART_QUERY.*{says}"):
            tools.find_part(*arguments)
    with pytest.raises(ToolError, match="E_PART_QUERY: limit"):
        tools.find_part("resistor", "10k", limit=50)


def test_a_design_that_names_its_parts_rests_on_their_figures_not_on_defaults(
    tmp_path: Path,
) -> None:
    tools = tools_with(tmp_path, with_catalogue=True)
    data = design()
    data["requirements"] = [
        {"type": "max_component_power", "component": "R1", "derating": "50%"}
    ]
    report = tools.accuracy_report(data)
    assert report["catalogue"]["filled"][1] == (
        "R1 (AT0603FRD0710KL): tolerance, p_rating, tempco"
    )
    row = next(t for t in report["tolerances"] if t["ref"] == "R1")
    assert (
        row["tolerance"] == "1%" and "LCSC's catalogue entry C20144536" in row["source"]
    )
    # R1's tolerance came from its listing, so it is no default; the
    # regulator's output, which nothing lists, still is.
    used = " ".join(report["policies"])
    assert "R1's tolerance" not in used and "U1's v_out_tolerance: 5%" in used

    # With no catalogue on the computer nothing is taken from one,
    # and asking for parts says how to get a catalogue.
    bare = tools_with(tmp_path / "other", with_catalogue=False)
    report = bare.accuracy_report(data)
    assert "catalogue" not in report
    assert not any("LCSC" in row["source"] for row in report["tolerances"])
    with pytest.raises(ToolError, match="E_CATALOGUE_UNAVAILABLE"):
        bare.find_part("resistor", "10k")


def drawn_netlist() -> Netlist:
    """A few drawn parts, with the field names the real boards use."""

    def part(ref: str, lib_id: str, value: str, *fields: tuple[str, str]) -> Component:
        return Component(ref, value, "", lib_id, "", "/", fields)

    parts = (
        part("R1", "Device:R", "10k", ("LCSC", "c20144536")),
        part(
            "C1",
            "Device:C",
            "100n",
            ("Manufacturer Part", "CL10B104KB8SFNC"),
            ("LCSC Part #", "C16194178"),
        ),
        # The maker's number is unknown to the catalogue; the LCSC one is not.
        part(
            "R2", "Device:R", "10k", ("Part_number", "NOT-LISTED"), ("LCSC", "C99001")
        ),
        part("R3", "Device:R", "10k", ("LCSC", "C99001")),
        part("R4", "Device:R", "12k", ("LCSC Part", "C20144536")),
        part("U1", "Regulator_Linear:X", "X", ("MPN", "LM1117-3.3"), ("LCSC", "none")),
        part("R5", "Device:R", "1k", ("Notes", "C123"), ("Supplier", "Mouser")),
    )
    return Netlist(
        "test",
        ("/",),
        parts,
        (
            Net("A", tuple(Node(p.ref, "1", "", "passive") for p in parts)),
            Net("B", tuple(Node(p.ref, "2", "", "passive") for p in parts)),
        ),
    )


def tolerance(part: Any) -> float:
    stated = part.quantity("tolerance")
    assert stated is not None
    return stated.value


def test_a_drawn_part_takes_its_number_from_its_fields(catalogue: Catalogue) -> None:
    netlist = drawn_netlist()
    numbers = {part.ref: part_numbers(part) for part in netlist.components}
    assert numbers["C1"] == [
        ("CL10B104KB8SFNC", "Manufacturer Part"),
        ("C16194178", "LCSC Part #"),
    ]
    assert numbers["R1"] == [("C20144536", "LCSC")]
    assert numbers["U1"] == [("LM1117-3.3", "MPN")]
    # A field that is not a part number's is not read as one.
    assert numbers["R5"] == []

    facts = {"components": {"R3": {"params": {"mpn": "AT0603FRD0710KL"}}}}
    drawn = read_drawn(netlist, "drawn", facts, catalogue)
    parts = drawn.grounded.design.components
    assert parts["R1"].params["mpn"] == "C20144536"
    assert tolerance(parts["R1"]) == 1
    assert parts["C1"].params["mpn"] == "CL10B104KB8SFNC"
    assert parts["C1"].params["dielectric"] == "X7R"
    assert parts["R2"].params["mpn"] == "C99001"
    assert tolerance(parts["R2"]) == 5
    # What the caller states about a part comes before what is drawn.
    assert parts["R3"].params["mpn"] == "AT0603FRD0710KL"
    assert tolerance(parts["R3"]) == 1
    assert "tolerance" not in parts["R4"].params
    assert parts["U1"].params == {"mpn": "LM1117-3.3"}
    assert "mpn" not in parts["R5"].params
    assert drawn.numbered == {
        "R1": "C20144536, from its field 'LCSC'",
        "C1": "CL10B104KB8SFNC, from its field 'Manufacturer Part'",
        "R2": "C99001, from its field 'LCSC'",
        "R4": "C20144536, from its field 'LCSC Part'",
        "U1": "LM1117-3.3, from its field 'MPN'",
    }
    assert drawn.filled is not None
    assert [line.split()[0] for line in drawn.filled.filled] == ["R1", "C1", "R2", "R3"]
    assert "R4 is marked 12k" in drawn.filled.mismatched[0]

    # With no catalogue the number is still the part's, and nothing is filled.
    bare = read_drawn(netlist, "drawn")
    assert bare.filled is None
    assert bare.grounded.design.components["R2"].params == {"mpn": "NOT-LISTED"}
    assert bare.grounded.design.components["R1"].params == {"mpn": "C20144536"}


def test_a_regulators_listing_is_read_whole_or_not_at_all() -> None:
    by_number = {listing[1]: listing for listing in LISTINGS}

    def read(number: str) -> dict[str, Any]:
        listing = by_number[number]
        figures = read_listing(listing[3], listing[5])
        assert figures is not None
        return figures

    fixed = read("AMS1117-3.3")
    assert fixed == {
        "kind": "regulator",
        "tolerance": None,
        "topology": "ldo",
        "value": 3.3,
        "v_out_min": 3.3,
        "v_out_max": 3.3,
        "i_out": 1.0,
        # One figure for the supply is the most it may be.
        "v_in_min": None,
        "v_in_max": 12.0,
        "v_dropout": 1.3,
        "i_q": pytest.approx(0.004),
        "f_sw": None,
    }
    # A dropout listed at two currents is not one figure.
    assert read("RT9013-33GB-MS")["v_dropout"] is None
    buck = read("TPS562242DRLR")
    assert (buck["topology"], buck["value"]) == ("buck", None)
    assert (buck["v_out_min"], buck["v_out_max"]) == (pytest.approx(0.8), 10.0)
    assert (buck["v_in_min"], buck["v_in_max"]) == (3.0, 17.0)
    assert buck["f_sw"] == pytest.approx(1.4e6)
    # Two quiescent currents listed: neither is taken.
    assert read("HT7183SPER")["i_q"] is None
    assert "topology" not in read("NEG-EXAMPLE")
    assert "topology" not in read("FLYBACK-EXAMPLE")


def test_a_regulator_is_found_by_what_it_must_do(catalogue: Catalogue) -> None:
    def numbers(volts: float, **asked: Any) -> list[str]:
        found, total = catalogue.find("regulator", volts, **asked)
        assert total == len(found)
        return [part.mpn for part in found]

    # By stock; the adjustable parts reach 3.3 V too.
    assert numbers(3.3) == [
        "RT9013-33GB-MS",
        "AMS1117-3.3",
        "TPS562242DRLR",
        "HT7183SPER",
    ]
    assert numbers(3.3, topology="ldo") == ["RT9013-33GB-MS", "AMS1117-3.3"]
    assert numbers(3.3, topology="ldo", minimums={"i_out": 0.8}) == ["AMS1117-3.3"]
    # From 5 V both linear parts can make 3.3 V, the 1117 with 0.4 V to
    # spare over its listed dropout; the step-up part cannot step down.
    assert numbers(3.3, v_in=5.0) == [
        "RT9013-33GB-MS",
        "AMS1117-3.3",
        "TPS562242DRLR",
    ]
    # From 4.5 V the 1117's 1.3 V of dropout leaves it short.
    assert numbers(3.3, v_in=4.5, topology="ldo") == ["RT9013-33GB-MS"]
    # 12 V is more than the small linear part may take.
    assert numbers(3.3, v_in=12.0) == ["AMS1117-3.3", "TPS562242DRLR"]
    assert numbers(12.0, v_in=5.0) == ["HT7183SPER"]
    assert numbers(12.0, v_in=9.0) == []
    assert numbers(20.0) == []
    part = catalogue.lookup("tps562242drlr")
    assert part is not None and part.kind == "regulator" and part.f_sw == 1.4e6


def test_find_part_lists_regulators_for_choosing_and_fills_nothing(
    tmp_path: Path,
) -> None:
    tools = tools_with(tmp_path, with_catalogue=True)
    found = tools.find_part(
        "regulator", "5V", topology="buck", v_in="12V", i_out="1.5A"
    )
    assert found["matched"] == 1
    assert found["parts"] == [
        {
            "mpn": "TPS562242DRLR",
            "lcsc": "C30004",
            "manufacturer": "Texas Instruments",
            "package": "SOT-563-6",
            "topology": "buck",
            "output": "0.8 V to 10 V, adjustable",
            "i_out_listed": "2 A",
            "v_in_min_listed": "3 V",
            "v_in_max_listed": "17 V",
            "i_q_listed": "100 uA",
            "f_sw_listed": "1400 kHz",
            "datasheet": "https://example.invalid/TPS562242DRLR.pdf",
            "stock": 36897,
            "basic_part": False,
        }
    ]
    assert "never filled into a design" in found["notes"][1]
    with pytest.raises(ToolError, match="E_PART_QUERY: topology is one of"):
        tools.find_part("regulator", "5V", topology="flyback")
    with pytest.raises(ToolError, match="are for a regulator"):
        tools.find_part("resistor", "10k", v_in="5V")

    # A chip that names a listed regulator takes nothing from the listing.
    data = design()
    data["components"]["U1"].setdefault("params", {})["mpn"] = "AMS1117-3.3"
    before = dict(data["components"]["U1"]["params"])
    filled, notes = fill(data, Catalogue(tmp_path / "cache" / FILE_NAME))
    assert filled["components"]["U1"]["params"] == before
    assert not any(line.startswith("U1") for line in notes.filled)
