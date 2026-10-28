"""The least spacing between conductors for a voltage, from IPC-2221's Table 6-1.

The table gives a spacing for each band of voltage (DC or AC peak) and,
above 500 V, a spacing to add for each further volt. Two revisions are
held here, because they differ and a board may be designed to either:

- ``B`` (IPC-2221B, 2012), read from the table as reproduced in Altium's
  article "Using an IPC-2221 Calculator for High Voltage Design", which
  also gives each column's meaning.
- ``C`` (IPC-2221C, December 2023, page 58), read from the table as
  reproduced, with IPC's permission, in Siemens' article "PCB high voltage
  spacing: what every engineer should know" (29 April 2025).

Neither was read from the standard itself, which Kairo's author does not
have; each value was read from a picture of the table. Revision C's
picture does not give its columns' meanings: it heads B1 to B5
"conductors (creepage on same layer, internal or external)", its note 4
says B5 was A5 before, and its A6 and A7 hold the values revision B has
under A7 and A6. So revision C's columns are offered by letter only, and
the calculations that name a use (internal, external uncoated) take
their meaning from revision B and the larger of the two revisions'
values.

The standard's own notes apply: the values presume woven glass coated
with resin, and a product safety standard may ask for more.
"""

from __future__ import annotations

from typing import Any, Literal

from kairo.calc.interval import Interval

Revision = Literal["B", "C"]

#: The top of each voltage band, in volts. The table's bands are whole
#: volts (0-15, 16-30, ...); a voltage between two bands is put in the
#: higher one.
BANDS: tuple[float, ...] = (15.0, 30.0, 50.0, 100.0, 150.0, 170.0, 250.0, 300.0, 500.0)

#: Each column's spacing in mm for each band of `BANDS`, then the mm to
#: add for each volt above 500 V.
TABLES: dict[Revision, dict[str, tuple[tuple[float, ...], float]]] = {
    "B": {
        "B1": ((0.05, 0.05, 0.1, 0.1, 0.2, 0.2, 0.2, 0.2, 0.25), 0.0025),
        "B2": ((0.1, 0.1, 0.6, 0.6, 0.6, 1.25, 1.25, 1.25, 2.5), 0.005),
        "B3": ((0.1, 0.1, 0.6, 1.5, 3.2, 3.2, 6.4, 12.5, 12.5), 0.025),
        "B4": ((0.05, 0.05, 0.13, 0.13, 0.4, 0.4, 0.4, 0.4, 0.8), 0.00305),
        "A5": ((0.13, 0.13, 0.13, 0.13, 0.4, 0.4, 0.4, 0.4, 0.8), 0.00305),
        "A6": ((0.13, 0.25, 0.4, 0.5, 0.8, 0.8, 0.8, 0.8, 1.5), 0.00305),
        "A7": ((0.13, 0.13, 0.13, 0.13, 0.4, 0.4, 0.4, 0.8, 0.8), 0.00305),
    },
    "C": {
        "B1": ((0.05, 0.05, 0.1, 0.1, 0.2, 0.2, 0.2, 0.2, 0.25), 0.0025),
        "B2": ((0.1, 0.1, 0.64, 0.64, 0.64, 1.25, 1.25, 1.25, 2.5), 0.005),
        "B3": ((0.1, 0.1, 0.64, 1.5, 3.2, 3.2, 6.4, 12.5, 12.5), 0.025),
        "B4": ((0.075, 0.075, 0.3, 0.3, 0.8, 0.8, 0.8, 0.8, 1.6), 0.00305),
        "B5": ((0.075, 0.075, 0.13, 0.13, 0.4, 0.4, 0.4, 0.4, 0.8), 0.00305),
        "A6": ((0.13, 0.13, 0.13, 0.13, 0.4, 0.4, 0.4, 0.4, 0.8), 0.00305),
        "A7": ((0.13, 0.25, 0.4, 0.5, 0.8, 0.8, 0.8, 0.8, 1.5), 0.00305),
        "A8": ((0.13, 0.25, 0.8, 1.0, 1.6, 1.6, 1.6, 1.6, 3.0), 0.0061),
    },
}

#: What each column of revision B is for, as its table states.
MEANINGS_B: dict[str, str] = {
    "B1": "internal conductors",
    "B2": "external conductors, uncoated, sea level to 3050 m",
    "B3": "external conductors, uncoated, over 3050 m",
    "B4": "external conductors, with permanent polymer coating (any elevation)",
    "A5": "external conductors, with conformal coating over assembly (any elevation)",
    "A6": "external component lead or termination, uncoated, sea level to 3050 m",
    "A7": "external component lead termination, with conformal coating (any elevation)",
}


def conductor_spacing(volts: float, column: str, revision: Revision) -> float:
    """The least spacing for a voltage between two conductors.

    Args:
        volts: The voltage between the conductors, DC or AC peak, not
            negative.
        column: The table's column, such as ``B1``.
        revision: ``B`` or ``C``.

    Returns:
        The spacing in mm. Above 500 V it is the 500 V value plus the
        column's addition for each further volt.

    Raises:
        ValueError: If the voltage is negative or the revision has no
            such column.
    """
    if volts < 0:
        raise ValueError("the voltage between conductors is given as a magnitude")
    table = TABLES[revision]
    if column not in table:
        raise ValueError(
            f"IPC-2221{revision} has no column {column}; it has {', '.join(table)}"
        )
    values, per_volt = table[column]
    for top, value in zip(BANDS, values, strict=True):
        if volts <= top:
            return value
    return values[-1] + (volts - BANDS[-1]) * per_volt


def larger_spacing(volts: float, column: str) -> float:
    """The larger of the two revisions' spacings for one column.

    Args:
        volts: The voltage between the conductors, DC or AC peak.
        column: A column both revisions have.

    Returns:
        The spacing in mm.
    """
    return max(
        conductor_spacing(volts, column, "B"), conductor_spacing(volts, column, "C")
    )


def spacing_formula(column: str) -> Any:
    """A column as a formula the calculation engine can run.

    The spacing never falls as the voltage rises, so over a range of
    voltage it lies between the values at the range's two ends.

    Args:
        column: A column both revisions have.

    Returns:
        A formula of one input, ``voltage``, on a number or an interval,
        giving the larger of the two revisions' spacings.
    """

    def formula(v: Any) -> Any:
        voltage = v["voltage"]
        if isinstance(voltage, Interval):
            return Interval(
                larger_spacing(voltage.lo, column), larger_spacing(voltage.hi, column)
            )
        return larger_spacing(float(voltage), column)

    return formula
