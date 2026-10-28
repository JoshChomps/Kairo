"""A catalogue of real parts with their published figures.

Two things a design needs come from here. Choosing a part: which
resistors of 10 kilohms in 0603 exist, with what tolerance and rating,
and are in stock. And the figures of a part already chosen: a design
that names a part number need not restate its tolerance, rating or
saturation current.

The catalogue is built from the jlcparts project's snapshot of JLCPCB's
and LCSC's parts (https://github.com/yaqwsx/jlcparts), a SQLite file
published in pieces at ``yaqwsx.github.io/jlcparts/data``. That file is
about 1.1 GB and holds over two million parts of every kind; `build`
keeps the kinds Kairo reckons with (resistors, capacitors, inductors,
LEDs) and the figures it can read, in a file of a few tens of
megabytes. Nothing is downloaded unless a person runs `fetch`::

    python -m kairo.parts.catalogue fetch FOLDER
    python -m kairo.parts.catalogue build FOLDER/cache.sqlite3 CATALOGUE

What the figures are: a distributor's listing of each part's published
parameters, as text such as ``±1%`` or ``100mW``, read on the date the
snapshot was taken. They are not the datasheet. A listing can be wrong
or lack a figure, the snapshot does not hold every part, and stock is
as of its date. So a figure taken from here says so in its source, and
a figure this module cannot read as one plain number is left out, never
guessed at: a tolerance of ``-20%~+80%`` is not turned into one number.

What was seen in the snapshot fetched on 2026-10-11 (2,074,000 parts):
figures are spelled ``10.2kΩ``, ``±1%``, ``100mW``, ``±100ppm/℃``,
``50V``, ``100nF``, ``330nH``, ``5.4A``, ``23mΩ``, ``2V~2.4V``; a
ceramic capacitor's dielectric is under ``Temperature Coefficient``;
about two parts in five have no attributes at all. That snapshot held
six parts with an LCSC number below a million and one part marked
basic, so the long-established parts (``C25804``, the common 10k
resistor, for one) were not in it: it was not the whole catalogue.
"""

from __future__ import annotations

import json
import re
import sqlite3
import struct
import sys
import urllib.request
import zlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

#: Where the jlcparts project publishes its snapshot, and the pieces of it.
SOURCE_URL = "https://yaqwsx.github.io/jlcparts/data/"
PIECES = ("cache.z01", "cache.z02", "cache.z03", "cache.zip")
#: The catalogue's file name in Kairo's cache folder.
FILE_NAME = "parts-catalogue.sqlite3"
#: What a figure from the catalogue says of where it came from.
SOURCE = "LCSC's catalogue"

Kind = Literal["resistor", "capacitor", "inductor", "led", "regulator"]

# Which of the snapshot's subcategories are which kind, and for
# capacitors what Kairo calls the dielectric when the listing gives no
# code of its own.
_SUBCATEGORIES: dict[str, tuple[Kind, str]] = {
    "Chip Resistor - Surface Mount": ("resistor", ""),
    "Through Hole Resistors": ("resistor", ""),
    "Multilayer Ceramic Capacitors MLCC - SMD/SMT": ("capacitor", ""),
    "Multilayer Ceramic Capacitors MLCC - Leaded": ("capacitor", ""),
    "Tantalum Capacitors": ("capacitor", "tantalum"),
    "Polymer Aluminum Capacitors": ("capacitor", "polymer"),
    "Aluminum Electrolytic Capacitors - SMD": ("capacitor", "electrolytic"),
    "Aluminum Electrolytic Capacitors - Leaded": ("capacitor", "electrolytic"),
    "Power Inductors": ("inductor", ""),
    "Inductors (SMD)": ("inductor", ""),
    "LED Indication - Discrete": ("led", ""),
    "Voltage Regulators - Linear, Low Drop Out (LDO) Regulators": (
        "regulator",
        "ldo",
    ),
    "DC-DC Converters": ("regulator", ""),
}
#: A converter's listed topology as Kairo names it; others are not kept.
_TOPOLOGIES = {"Buck": "buck", "Boost": "boost"}
_PREFIXES = {
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "µ": 1e-6,
    "μ": 1e-6,
    "m": 1e-3,
    "": 1.0,
    "k": 1e3,
    "K": 1e3,
    "M": 1e6,
    "G": 1e9,
}
_QUANTITY = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([pnuµμmkKMG]?)(Ω|F|Hz|H|V|A|W)")
_SYMMETRIC = re.compile(r"±\s*([0-9]+(?:\.[0-9]+)?)\s*%")
_TEMPCO = re.compile(r"±\s*([0-9]+(?:\.[0-9]+)?)\s*ppm/℃")
_RANGE = re.compile(
    r"([0-9]+(?:\.[0-9]+)?)\s*([pnuµμmkKMG]?)V\s*~\s*([0-9]+(?:\.[0-9]+)?)\s*([pnuµμmkKMG]?)V"
)
_DIELECTRIC = re.compile(r"\b(C0G|NP0|X5R|X7R|X6S|X7S|X7T|X8R|Y5V|Z5U|U2J)\b")


class CatalogueError(ValueError):
    """The catalogue, or the snapshot it is built from, cannot be used."""


@dataclass(frozen=True, slots=True)
class CataloguePart:
    """One part and the figures its listing gives.

    A figure the listing lacks, or that could not be read as one plain
    number, is ``None``.

    Attributes:
        lcsc: LCSC's number for it, without the leading ``C``.
        mpn: The maker's part number.
        manufacturer: The maker, as listed.
        kind: ``resistor``, ``capacitor``, ``inductor``, ``led`` or
            ``regulator``.
        package: The package, as listed: ``0603``, ``SOT-23``.
        value: The resistance in ohms, capacitance in farads or
            inductance in henries; for a regulator with a fixed
            output, that voltage.
        tolerance: The tolerance in percent, where the listing gives one
            figure for both sides.
        v_rating: The rated voltage, in volts.
        p_rating: The rated power, in watts.
        i_sat: An inductor's saturation current, in amps.
        i_rating: An inductor's rated current, in amps.
        dcr: An inductor's DC resistance, in ohms.
        esr: A capacitor's listed ESR, in ohms; the listing does not say
            whether it is typical or a limit.
        tempco: A resistor's temperature coefficient, in ppm per degree.
        dielectric: A capacitor's kind: a ceramic's code such as
            ``X7R``, or ``tantalum``, ``polymer``, ``electrolytic``.
        v_f: An LED's forward voltage, in volts: the middle of the
            listed range, or the one figure listed.
        v_f_tolerance: Half the listed range, in percent of the middle.
        stock: How many JLCPCB held when the snapshot was taken.
        basic: True if JLCPCB lists it as a basic or a preferred part,
            which it fits without an extra charge.
        datasheet: A link to its datasheet, if listed.
        fetched: When its listing was read, in seconds since 1970.
        topology: For a regulator, ``ldo``, ``buck`` or ``boost``.
        v_out_min: The least output voltage a regulator can be set
            to; the same as ``v_out_max`` for a fixed one.
        v_out_max: The greatest.
        i_out: A regulator's listed output current, in amps.
        v_in_min: The least input voltage listed, in volts.
        v_in_max: The greatest input voltage listed, in volts.
        v_dropout: A linear regulator's listed dropout, in volts, at
            a current the listing gives and this does not keep.
        i_q: The listed quiescent or standby current, in amps.
        f_sw: A converter's listed switching frequency, in hertz.
    """

    lcsc: int
    mpn: str
    manufacturer: str
    kind: Kind
    package: str
    value: float | None
    tolerance: float | None
    v_rating: float | None
    p_rating: float | None
    i_sat: float | None
    i_rating: float | None
    dcr: float | None
    esr: float | None
    tempco: float | None
    dielectric: str | None
    v_f: float | None
    v_f_tolerance: float | None
    stock: int
    basic: bool
    datasheet: str
    fetched: int
    topology: str | None = None
    v_out_min: float | None = None
    v_out_max: float | None = None
    i_out: float | None = None
    v_in_min: float | None = None
    v_in_max: float | None = None
    v_dropout: float | None = None
    i_q: float | None = None
    f_sw: float | None = None


_COLUMNS = (
    "lcsc",
    "mpn",
    "manufacturer",
    "kind",
    "package",
    "value",
    "tolerance",
    "v_rating",
    "p_rating",
    "i_sat",
    "i_rating",
    "dcr",
    "esr",
    "tempco",
    "dielectric",
    "v_f",
    "v_f_tolerance",
    "stock",
    "basic",
    "datasheet",
    "fetched",
    "topology",
    "v_out_min",
    "v_out_max",
    "i_out",
    "v_in_min",
    "v_in_max",
    "v_dropout",
    "i_q",
    "f_sw",
)
#: The catalogue file's layout; an older one must be built again.
LAYOUT = "kairo-parts-2"


def quantity(text: object, unit: str) -> float | None:
    """Read a listed figure such as ``10.2kΩ`` or ``100mW`` in its base unit.

    Args:
        text: The listing's text.
        unit: The unit's symbol: ``Ω``, ``F``, ``H``, ``Hz``, ``V``, ``A``
            or ``W``.

    Returns:
        The value, or ``None`` if the text is not one plain figure in
        that unit. Anything after the figure, such as ``@100kHz``, is
        left aside.
    """
    if not isinstance(text, str):
        return None
    found = _QUANTITY.match(text.strip())
    if found is None or found.group(3) != unit:
        return None
    return float(found.group(1)) * _PREFIXES[found.group(2)]


def read_listing(
    subcategory: str, attributes: dict[str, Any]
) -> dict[str, float | str | None] | None:
    """Read one listing's figures.

    Args:
        subcategory: The snapshot's subcategory for the part.
        attributes: Its attributes, as the snapshot holds them.

    Returns:
        The figures by `CataloguePart` field name, with ``kind``; or
        ``None`` if the subcategory is none Kairo keeps.
    """
    known = _SUBCATEGORIES.get(subcategory)
    if known is None:
        return None
    kind, plain = known
    out: dict[str, float | str | None] = {"kind": kind}
    symmetric = _SYMMETRIC.fullmatch(str(attributes.get("Tolerance", "")).strip())
    out["tolerance"] = float(symmetric.group(1)) if symmetric else None
    if kind == "resistor":
        out["value"] = quantity(attributes.get("Resistance"), "Ω")
        out["p_rating"] = quantity(attributes.get("Power(Watts)"), "W")
        drift = _TEMPCO.fullmatch(
            str(attributes.get("Temperature Coefficient", "")).strip()
        )
        out["tempco"] = float(drift.group(1)) if drift else None
    elif kind == "capacitor":
        out["value"] = quantity(attributes.get("Capacitance"), "F")
        out["v_rating"] = quantity(attributes.get("Voltage Rating"), "V")
        out["esr"] = quantity(attributes.get("Equivalent Series Resistance(ESR)"), "Ω")
        code = _DIELECTRIC.search(str(attributes.get("Temperature Coefficient", "")))
        out["dielectric"] = plain or (code.group(1) if code else None)
    elif kind == "inductor":
        out["value"] = quantity(attributes.get("Inductance"), "H")
        out["i_sat"] = quantity(attributes.get("Current - Saturation(Isat)"), "A")
        out["i_rating"] = quantity(attributes.get("Current Rating"), "A")
        out["dcr"] = quantity(attributes.get("DC Resistance(DCR)"), "Ω")
    elif kind == "regulator":
        out.update(_regulator(plain, attributes))
    else:
        listed = str(attributes.get("Voltage - Forward(Vf)", "")).strip()
        spread = _RANGE.fullmatch(listed)
        if spread is not None:
            low = float(spread.group(1)) * _PREFIXES[spread.group(2)]
            high = float(spread.group(3)) * _PREFIXES[spread.group(4)]
            if 0 < low <= high:
                middle = (low + high) / 2
                out["v_f"] = middle
                out["v_f_tolerance"] = round((high - low) / 2 / middle * 100, 3)
        else:
            single = _QUANTITY.fullmatch(listed)
            out["v_f"] = quantity(listed, "V") if single else None
    return out


def _single(text: object, unit: str) -> float | None:
    """A listed figure that is one plain figure and nothing else."""
    if not isinstance(text, str):
        return None
    found = _QUANTITY.fullmatch(text.strip())
    if found is None or found.group(3) != unit:
        return None
    return float(found.group(1)) * _PREFIXES[found.group(2)]


def _span(text: object) -> tuple[float | None, float | None]:
    """A listed voltage range such as ``4.5V~28V``, or one voltage as both ends."""
    if not isinstance(text, str):
        return None, None
    spread = _RANGE.fullmatch(text.strip())
    if spread is None:
        one = _single(text, "V")
        return one, one
    low = float(spread.group(1)) * _PREFIXES[spread.group(2)]
    high = float(spread.group(3)) * _PREFIXES[spread.group(4)]
    return (low, high) if 0 < low <= high else (None, None)


def _regulator(plain: str, attributes: dict[str, Any]) -> dict[str, float | str | None]:
    """A regulator's listed figures; none if it is no kind Kairo keeps.

    Kept are positive linear regulators and step-down and step-up
    converters with one output. A listing with two figures where one is
    expected (a part with two outputs) is read as having none.
    """
    topology = plain or _TOPOLOGIES.get(str(attributes.get("Topology", "")).strip())
    wrong_way = str(attributes.get("Output Configuration", "Positive")) != "Positive"
    low, high = _span(attributes.get("Output Voltage"))
    if topology is None or wrong_way or high is None:
        return {}
    supply_low, supply_high = _span(attributes.get("Voltage - Supply"))
    # A dropout is listed with its current, as 300mV@(200mA); one listed
    # at two currents is not one figure.
    dropout = str(attributes.get("Voltage Dropout", ""))
    if supply_low == supply_high:
        # One figure is the most the input may be.
        supply_low = None
    return {
        "topology": topology,
        "value": high if low == high else None,
        "v_out_min": low,
        "v_out_max": high,
        "i_out": _single(attributes.get("Output Current"), "A"),
        "v_in_min": supply_low,
        "v_in_max": supply_high,
        "v_dropout": (None if "," in dropout else _single(dropout.split("@")[0], "V")),
        "i_q": _single(
            attributes.get("Quiescent Current", attributes.get("standby current")),
            "A",
        ),
        "f_sw": _single(attributes.get("Frequency - Switching"), "Hz"),
    }


def unpack(pieces: Sequence[Path], target: Path) -> int:
    """Join and unpack the snapshot's split archive.

    The archive is a zip split into pieces, holding one file. It is read
    here with the standard library alone: the pieces in order are one
    stream that begins with the split marker and the file's header, and
    then the file, deflated.

    Args:
        pieces: The pieces in order, the ``.zip`` last.
        target: Where to write the file inside.

    Returns:
        The size of the file written, in bytes.

    Raises:
        CatalogueError: If the pieces are not such an archive, or what
            was unpacked does not match the archive's own checksum.
    """

    def blocks() -> Iterator[bytes]:
        for piece in pieces:
            with piece.open("rb") as handle:
                while block := handle.read(1 << 20):
                    yield block

    stream = blocks()
    head = next(stream, b"")
    while len(head) < 34:
        more = next(stream, None)
        if more is None:
            break
        head += more
    start = 4 if head[:4] == b"PK\x07\x08" else 0
    if head[start : start + 4] != b"PK\x03\x04" or len(head) < start + 30:
        raise CatalogueError("the pieces are not a zip archive, or are out of order")
    _, _, method, _, _, crc, _, _, name_length, extra_length = struct.unpack(
        "<HHHHHIIIHH", head[start + 4 : start + 30]
    )
    if method != 8:
        raise CatalogueError(f"the archive is packed by method {method}, not deflate")
    block = head[start + 30 + name_length + extra_length :]
    inflater = zlib.decompressobj(-15)
    written = 0
    check = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as out:
        while True:
            try:
                data = inflater.decompress(block)
            except zlib.error as exc:
                raise CatalogueError(f"the archive is damaged: {exc}") from exc
            out.write(data)
            written += len(data)
            check = zlib.crc32(data, check)
            if inflater.eof:
                break
            more = next(stream, None)
            if more is None:
                raise CatalogueError("the archive ends early: a piece is missing")
            block = more
    if check != crc:
        target.unlink()
        raise CatalogueError("what was unpacked does not match the archive's checksum")
    return written


def fetch(folder: Path) -> Path:
    """Download the snapshot's pieces and unpack them.

    This is the one place Kairo fetches anything, and only when a person
    runs it.

    Args:
        folder: Where to put the pieces and the unpacked ``cache.sqlite3``.

    Returns:
        The unpacked file.
    """
    folder.mkdir(parents=True, exist_ok=True)
    for name in PIECES:
        with urllib.request.urlopen(SOURCE_URL + name) as response:
            (folder / name).write_bytes(response.read())
    target = folder / "cache.sqlite3"
    unpack([folder / name for name in PIECES], target)
    return target


@dataclass(frozen=True, slots=True)
class Built:
    """What building a catalogue kept.

    Attributes:
        kept: How many parts are in it, by kind.
        without_figures: How many parts of those kinds were left out
            because their listing gave no value.
        snapshot: When the newest listing was read, in seconds since 1970.
    """

    kept: dict[str, int]
    without_figures: int
    snapshot: int


def build(snapshot: Path, target: Path) -> Built:
    """Build Kairo's catalogue from a jlcparts snapshot.

    Args:
        snapshot: The snapshot's ``cache.sqlite3``.
        target: The catalogue to write; an existing one is replaced.

    Returns:
        What was kept.

    Raises:
        CatalogueError: If the snapshot is not laid out as the one this
            was written against (format ``source-db-v2``).
    """
    try:
        source = sqlite3.connect(f"file:{snapshot.as_posix()}?mode=ro", uri=True)
        layout = source.execute(
            "select value from meta where key = 'format'"
        ).fetchone()
    except sqlite3.Error as exc:
        raise CatalogueError(
            f"{snapshot.name} is not a jlcparts snapshot: {exc}"
        ) from exc
    if layout is None or layout[0] != "source-db-v2":
        raise CatalogueError(
            f"{snapshot.name} is in the layout {layout[0] if layout else 'unknown'}; "
            "this was written against source-db-v2"
        )
    if target.exists():
        target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    out = sqlite3.connect(target)
    out.execute(
        "create table parts (lcsc integer primary key, mpn text not null, "
        "manufacturer text, kind text not null, package text, value real, "
        "tolerance real, v_rating real, p_rating real, i_sat real, i_rating real, "
        "dcr real, esr real, tempco real, dielectric text, v_f real, "
        "v_f_tolerance real, stock integer, basic integer, datasheet text, "
        "fetched integer, topology text, v_out_min real, v_out_max real, "
        "i_out real, v_in_min real, v_in_max real, v_dropout real, i_q real, "
        "f_sw real)"
    )
    out.execute("create table meta (key text primary key, value text not null)")
    kept: dict[str, int] = {}
    skipped = 0
    newest = 0
    marks = ",".join("?" * len(_SUBCATEGORIES))
    rows = source.execute(
        "select lcsc, mfr, manufacturer, subcategory, package, attributes, stock, "
        f"library_type, preferred, datasheet, fetched_at from jlc_components "
        f"where subcategory in ({marks})",
        tuple(_SUBCATEGORIES),
    )
    for (
        lcsc,
        mpn,
        maker,
        subcategory,
        package,
        attributes,
        stock,
        library,
        preferred,
        sheet,
        at,
    ) in rows:
        try:
            listed = json.loads(attributes)
        except json.JSONDecodeError:
            listed = {}
        figures = read_listing(
            subcategory,
            cast("dict[str, Any]", listed) if isinstance(listed, dict) else {},
        )
        if figures is None or not mpn:
            continue
        if (
            figures.get("value") is None
            and figures.get("v_f") is None
            and figures.get("v_out_max") is None
        ):
            skipped += 1
            continue
        kind = str(figures["kind"])
        kept[kind] = kept.get(kind, 0) + 1
        newest = max(newest, int(at))
        record = {
            "lcsc": lcsc,
            "mpn": mpn,
            "manufacturer": maker,
            "package": package,
            "stock": stock,
            "basic": 1 if library == "base" or preferred else 0,
            "datasheet": sheet,
            "fetched": at,
            **figures,
        }
        out.execute(
            f"insert into parts ({', '.join(_COLUMNS)}) "
            f"values ({', '.join('?' * len(_COLUMNS))})",
            tuple(record.get(column) for column in _COLUMNS),
        )
    out.execute("create index parts_mpn on parts (mpn collate nocase)")
    out.execute("create index parts_value on parts (kind, value, package)")
    out.execute("create index parts_output on parts (kind, v_out_max)")
    out.executemany(
        "insert into meta values (?, ?)",
        [
            ("source", "jlcparts snapshot of JLCPCB's and LCSC's parts"),
            ("snapshot", str(newest)),
            ("layout", LAYOUT),
        ],
    )
    out.commit()
    out.close()
    source.close()
    return Built(kept, skipped, newest)


class Catalogue:
    """A built catalogue, opened for reading.

    Args:
        path: The catalogue file `build` wrote.

    Raises:
        CatalogueError: If the file is not one.
    """

    def __init__(self, path: Path) -> None:
        """Open the file and read when its snapshot was taken."""
        self.path = path
        try:
            self._db = sqlite3.connect(
                f"file:{path.as_posix()}?mode=ro", uri=True, check_same_thread=False
            )
            meta = dict(self._db.execute("select key, value from meta").fetchall())
        except sqlite3.Error as exc:
            raise CatalogueError(
                f"{path.name} is not a parts catalogue: {exc}"
            ) from exc
        if meta.get("layout") != LAYOUT:
            raise CatalogueError(
                f"{path.name} is not a parts catalogue this Kairo built; build "
                "it again with python -m kairo.parts.catalogue build"
            )
        self.snapshot = int(meta.get("snapshot", "0"))

    @property
    def date(self) -> str:
        """The day the newest listing was read, as ``2026-10-11``."""
        import datetime

        return (
            datetime.datetime.fromtimestamp(self.snapshot, tz=datetime.UTC)
            .date()
            .isoformat()
        )

    def lookup(self, mpn: str) -> CataloguePart | None:
        """Find a part by its maker's part number or its LCSC number.

        Args:
            mpn: The part number, in any letter case, or an LCSC number
                written ``C25804``.

        Returns:
            The part; where several listings share the number, the one
            with the most stock. ``None`` if the catalogue has none.
        """
        number = re.fullmatch(r"[Cc](\d+)", mpn.strip())
        if number is not None:
            where, key = "lcsc = ?", int(number.group(1))
        else:
            where, key = "mpn = ? collate nocase", mpn.strip()
        row = self._db.execute(
            f"select {', '.join(_COLUMNS)} from parts where {where} "
            "order by stock desc, lcsc limit 1",
            (key,),
        ).fetchone()
        return _part(row) if row is not None else None

    def find(
        self,
        kind: Kind,
        value: float,
        *,
        package: str | None = None,
        tolerance: float | None = None,
        minimums: dict[str, float] | None = None,
        dielectric: str | None = None,
        topology: str | None = None,
        v_in: float | None = None,
        limit: int = 10,
    ) -> tuple[list[CataloguePart], int]:
        """Find parts of one value that meet what is asked.

        Args:
            kind: The kind of part.
            value: The value wanted, in ohms, farads or henries; for an
                LED, its forward voltage, matched within 20%; for a
                regulator, the output voltage, which a fixed part
                must have and an adjustable one must reach.
            package: The package, such as ``0603``; any if ``None``.
            tolerance: The widest tolerance accepted, in percent. A part
                whose listing gives none is left out when this is given.
            minimums: The least rating accepted, by field name:
                ``v_rating``, ``p_rating``, ``i_sat``, ``i_rating``,
                ``i_out``. A part whose listing lacks the figure is
                left out.
            dielectric: For a capacitor, the kind wanted, such as ``X7R``.
            topology: For a regulator, ``ldo``, ``buck`` or ``boost``.
            v_in: For a regulator, the input voltage it must take: at
                most its listed greatest, at least its listed least
                where the listing gives one, and on the right side
                of the output for its topology, by the listed
                dropout for a linear one.
            limit: The most parts to return.

        Returns:
            The parts, basic parts first and then by stock, and how many
            matched in all.

        Raises:
            CatalogueError: If a minimum names a field that is not a
                rating.
        """
        if kind == "led":
            clauses = ["kind = ?", "v_f between ? and ?"]
            values: list[Any] = [kind, value * 0.8, value * 1.2]
        elif kind == "regulator":
            clauses = ["kind = ?", "v_out_min <= ?", "v_out_max >= ?"]
            values = [kind, value * (1 + 1e-6), value * (1 - 1e-6)]
        else:
            # The listing's figures are text read as numbers, so a value
            # is matched within a part in a million.
            clauses = ["kind = ?", "value between ? and ?"]
            values = [kind, value * (1 - 1e-6), value * (1 + 1e-6)]
        if package is not None:
            clauses.append("package = ? collate nocase")
            values.append(package)
        if tolerance is not None:
            clauses.append("tolerance <= ?")
            values.append(tolerance)
        if dielectric is not None:
            clauses.append("dielectric = ? collate nocase")
            values.append(dielectric)
        if topology is not None:
            clauses.append("topology = ? collate nocase")
            values.append(topology)
        if v_in is not None:
            clauses.append("v_in_max >= ? and (v_in_min is null or v_in_min <= ?)")
            values += [v_in, v_in]
            if kind == "regulator":
                # A step-down part needs more in than out and a step-up
                # part less; a linear one needs its dropout as well,
                # where the listing gives one.
                clauses.append(
                    "(topology != 'boost' or ? < ?) and "
                    "(topology = 'boost' or ? > ? + coalesce(v_dropout, 0))"
                )
                values += [v_in, value, v_in, value]
        for name, least in (minimums or {}).items():
            if name not in ("v_rating", "p_rating", "i_sat", "i_rating", "i_out"):
                raise CatalogueError(f"{name} is not a rating the catalogue holds")
            clauses.append(f"{name} >= ?")
            values.append(least)
        where = " and ".join(clauses)
        total = self._db.execute(
            f"select count(*) from parts where {where}", values
        ).fetchone()[0]
        rows = self._db.execute(
            f"select {', '.join(_COLUMNS)} from parts where {where} "
            "order by basic desc, stock desc, lcsc limit ?",
            (*values, limit),
        ).fetchall()
        return [_part(row) for row in rows], int(total)


def _part(row: Sequence[Any]) -> CataloguePart:
    fields = dict(zip(_COLUMNS, row, strict=True))
    fields["basic"] = bool(fields["basic"])
    fields["manufacturer"] = fields["manufacturer"] or ""
    fields["package"] = fields["package"] or ""
    fields["datasheet"] = fields["datasheet"] or ""
    return CataloguePart(**fields)


def main(argv: Sequence[str] | None = None) -> int:
    """Fetch a snapshot, or build a catalogue from one.

    ``fetch FOLDER`` downloads and unpacks the snapshot into a folder;
    ``build SNAPSHOT CATALOGUE`` builds Kairo's catalogue from it.

    Args:
        argv: The arguments; the process's own if ``None``.

    Returns:
        0 on success, 2 if the arguments are not understood.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "fetch":
        print(f"unpacked {fetch(Path(args[1]))}")
        return 0
    if len(args) == 3 and args[0] == "build":
        built = build(Path(args[1]), Path(args[2]))
        print(f"kept {built.kept}; {built.without_figures} listings gave no value")
        return 0
    print(
        "usage: python -m kairo.parts.catalogue fetch FOLDER\n"
        "       python -m kairo.parts.catalogue build SNAPSHOT CATALOGUE"
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
