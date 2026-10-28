"""Values with units, as a design states them: ``5V``, ``300mA``, ``1k``.

A quantity is read strictly. Text that is not a number followed by an
optional SI prefix and one of the known units is an error, never a guess,
and the value is scaled to the base unit with decimal arithmetic so that
``300mA`` is exactly the float nearest 0.3.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

#: SI prefixes a design may use, with their powers of ten.
PREFIXES: dict[str, int] = {
    "p": -12,
    "n": -9,
    "u": -6,
    "µ": -6,
    "m": -3,
    "k": 3,
    "M": 6,
    "G": 9,
}
#: Units that take a prefix. The empty unit is a bare number such as a
#: resistor value written ``1k``.
SCALED_UNITS = ("V", "A", "W", "J", "F", "H", "Hz", "s", "ohm", "")
#: Units written without a prefix. A length is in millimetres, the unit
#: board dimensions are given in; a prefixed metre would make ``1m``
#: mean both a metre and a thousandth.
PLAIN_UNITS = ("degC/W", "degC", "%", "mm", "dB")
UNITS = (*SCALED_UNITS, *PLAIN_UNITS)

_NUMBER = r"[+-]?(?:\d+\.?\d*|\.\d+)"
_PATTERN = re.compile(
    rf"({_NUMBER})\s*(?:"
    rf"(?P<plain>{'|'.join(re.escape(u) for u in PLAIN_UNITS)})"
    rf"|(?P<prefix>[{''.join(PREFIXES)}])?(?P<unit>Hz|ohm|Ω|[VAWJFHs])?"
    rf")"
)


# The letter-for-decimal-point marking of IEC 60062 (the RKM code), as
# schematics write component values: 4K7 is 4.7 kilohms, 4R7 is 4.7 ohms,
# R47 is 0.47 ohms, 4n7 is 4.7 nanofarads. The standard's resistor
# letters are R, K, M and G; a lower-case k is also common and means
# the same. A lower-case m is left out for resistors, where it could be
# read as milli or as a slip for mega.
_RKM: dict[str, dict[str, int]] = {
    "ohm": {"R": 0, "K": 3, "k": 3, "M": 6, "G": 9},
    "F": {"p": -12, "n": -9, "u": -6, "µ": -6, "m": -3},
    "H": {"n": -9, "u": -6, "µ": -6, "m": -3},
}
_RKM_PATTERN = re.compile(r"(\d*)([A-Za-zµ])(\d*)")


def _letter_marked(text: str, unit: str) -> Quantity | None:
    """A component value in IEC 60062's marking, or ``None`` if it is not one."""
    found = _RKM_PATTERN.fullmatch(text)
    letters = _RKM.get(unit, {})
    if found is None or found.group(2) not in letters:
        return None
    whole, letter, part = found.groups()
    if not whole and not part:
        return None
    if not part and letter in PREFIXES:
        # 100n and 1M are ordinary prefixed numbers, read as such.
        return None
    value = Decimal(f"{whole or 0}.{part or 0}").scaleb(letters[letter])
    return Quantity(float(value), unit)


class QuantityError(ValueError):
    """Text that is not a quantity Kairo reads."""


@dataclass(frozen=True, slots=True)
class Quantity:
    """A value in a base unit.

    Attributes:
        value: The value in the base unit, so ``300mA`` holds 0.3. A
            percentage holds the number written, so ``10%`` holds 10.
        unit: One of `UNITS`.
    """

    value: float
    unit: str

    def __str__(self) -> str:
        """The value and unit, for example ``0.3 A``."""
        return f"{self.value:g} {self.unit}".rstrip()


def parse_quantity(
    text: str, unit: str | None = None, *, bare: bool = False
) -> Quantity:
    """Read a quantity such as ``3.3V``, ``22uF``, ``90degC/W`` or ``1k``.

    Args:
        text: What the design says.
        unit: The unit the value must have, or ``None`` to accept any.
        bare: Whether a number with no unit (``1k``, ``100n``) is taken to
            be in ``unit``, which is how resistor and capacitor values are
            usually written; so is IEC 60062's marking, where a letter
            stands for the decimal point (``4K7``, ``4R7``, ``4n7``).
            Otherwise a missing unit is an error: ``300``
            for a current could be amps or milliamps, and Kairo does not
            pick.

    Returns:
        The value in the base unit.

    Raises:
        QuantityError: If the text is not a quantity, or has another unit.
    """
    if bare and unit is not None:
        marked = _letter_marked(text.strip(), unit)
        if marked is not None:
            return marked
    found = _PATTERN.fullmatch(text.strip())
    if found is None:
        raise QuantityError(
            f"{text!r} is not a quantity: write a number, an optional prefix "
            f"({' '.join(PREFIXES)}) and a unit ({', '.join(u for u in UNITS if u)})"
        )
    if found.group("plain"):
        got, exponent = found.group("plain"), 0
    else:
        got = (found.group("unit") or "").replace("Ω", "ohm")
        exponent = PREFIXES[found.group("prefix")] if found.group("prefix") else 0
    if unit is not None and got != unit:
        if got or not bare or unit in PLAIN_UNITS:
            raise QuantityError(
                f"{text!r} is in {got or 'no unit'}; expected {unit}, for "
                f"example {_example(unit)}"
            )
        got = unit
    value = Decimal(found.group(1)).scaleb(exponent)
    return Quantity(float(value), got)


def format_quantity(quantity: Quantity) -> str:
    """Write a quantity as text that `parse_quantity` reads back the same.

    Args:
        quantity: The quantity.

    Returns:
        The value and unit with no prefix or exponent, for example
        ``0.3A`` or ``0.000022F``.
    """
    return f"{Decimal(repr(quantity.value)):f}{quantity.unit}"


def _example(unit: str) -> str:
    return {"V": "3.3V", "A": "300mA", "%": "5%", "degC": "40degC"}.get(
        unit, f"10{unit}"
    )
