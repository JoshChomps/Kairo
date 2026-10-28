"""Interval arithmetic with outward rounding.

An interval holds every value a quantity can take. Each operation returns
an interval that is sure to contain every result the operation can give
on its operands, and the ends are moved outward by one floating-point
step after each operation, so rounding can never shrink it (Moore,
*Interval Analysis*, 1966).

The price is width: when the same input appears more than once in a
formula, the result can be wider than the true range, because each
appearance is treated as free to vary on its own. An interval result is
therefore a guaranteed enclosure, never a claim that its ends are reached.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Interval:
    """Every value from ``lo`` to ``hi``.

    Attributes:
        lo: The smallest value.
        hi: The largest value.
    """

    lo: float
    hi: float

    def __post_init__(self) -> None:
        """Refuse ends in the wrong order or that are not numbers.

        Raises:
            ValueError: If ``lo`` is above ``hi`` or either is not finite.
        """
        if not (math.isfinite(self.lo) and math.isfinite(self.hi)):
            raise ValueError(f"an interval's ends must be finite: {self}")
        if self.lo > self.hi:
            raise ValueError(f"an interval's ends are the wrong way round: {self}")

    @classmethod
    def point(cls, value: float) -> Interval:
        """The interval holding one value.

        Args:
            value: The value.

        Returns:
            The interval from the value to itself.
        """
        return cls(value, value)

    def contains(self, value: float) -> bool:
        """Whether a value lies in the interval.

        Args:
            value: The value.

        Returns:
            True if it does, ends included.
        """
        return self.lo <= value <= self.hi

    @property
    def width(self) -> float:
        """The distance between the ends."""
        return self.hi - self.lo

    def __add__(self, other: Interval | float) -> Interval:
        """Every sum of a value here and a value there."""
        o = _interval(other)
        return _outward(self.lo + o.lo, self.hi + o.hi)

    __radd__ = __add__

    def __neg__(self) -> Interval:
        """Every negated value."""
        return Interval(-self.hi, -self.lo)

    def __sub__(self, other: Interval | float) -> Interval:
        """Every difference of a value here and a value there."""
        o = _interval(other)
        return _outward(self.lo - o.hi, self.hi - o.lo)

    def __rsub__(self, other: float) -> Interval:
        """Every difference of a number and a value here."""
        return _interval(other) - self

    def __mul__(self, other: Interval | float) -> Interval:
        """Every product of a value here and a value there."""
        o = _interval(other)
        products = (self.lo * o.lo, self.lo * o.hi, self.hi * o.lo, self.hi * o.hi)
        return _outward(min(products), max(products))

    __rmul__ = __mul__

    def __truediv__(self, other: Interval | float) -> Interval:
        """Every quotient of a value here and a value there.

        Raises:
            ZeroDivisionError: If the divisor's interval holds zero.
        """
        o = _interval(other)
        if o.lo <= 0.0 <= o.hi:
            raise ZeroDivisionError(f"division by an interval that holds zero: {o}")
        quotients = (self.lo / o.lo, self.lo / o.hi, self.hi / o.lo, self.hi / o.hi)
        return _outward(min(quotients), max(quotients))

    def __rtruediv__(self, other: float) -> Interval:
        """Every quotient of a number and a value here."""
        return _interval(other) / self


def sqrt(value: Interval | float) -> Interval | float:
    """The square root of a number or of every value in an interval.

    Args:
        value: A number or an interval, not negative.

    Returns:
        A number for a number; for an interval, an interval holding every
        root.

    Raises:
        ValueError: If the value, or any part of the interval, is negative.
    """
    if isinstance(value, Interval):
        if value.lo < 0:
            raise ValueError(f"square root of an interval that goes negative: {value}")
        return _outward(math.sqrt(value.lo), math.sqrt(value.hi), floor=0.0)
    return math.sqrt(value)


def log10(value: Interval | float) -> Interval | float:
    """The base-10 logarithm of a positive number or interval.

    A logarithm rises with its argument throughout, so the ends of the
    result are the logarithms of the ends.

    Args:
        value: A number or an interval, above zero.

    Returns:
        A number for a number; for an interval, an interval holding every
        logarithm. Each end is moved two floating-point steps outward,
        because a logarithm is computed less exactly than a sum or a
        product.

    Raises:
        ValueError: If the value, or any part of the interval, is not
            above zero.
    """
    if isinstance(value, Interval):
        if value.lo <= 0:
            raise ValueError(
                f"logarithm of an interval that is not above zero: {value}"
            )
        once = _outward(math.log10(value.lo), math.log10(value.hi))
        return _outward(once.lo, once.hi)
    if value <= 0:
        raise ValueError(f"logarithm of a number that is not above zero: {value}")
    return math.log10(value)


def log(value: Interval | float) -> Interval | float:
    """The natural logarithm of a positive number or interval.

    Rises with its argument throughout, so the ends of the result are the
    logarithms of the ends, each moved two floating-point steps outward.

    Args:
        value: A number or an interval, above zero.

    Returns:
        A number for a number; for an interval, an interval holding every
        logarithm.

    Raises:
        ValueError: If the value, or any part of the interval, is not
            above zero.
    """
    if isinstance(value, Interval):
        if value.lo <= 0:
            raise ValueError(
                f"logarithm of an interval that is not above zero: {value}"
            )
        once = _outward(math.log(value.lo), math.log(value.hi))
        return _outward(once.lo, once.hi)
    if value <= 0:
        raise ValueError(f"logarithm of a number that is not above zero: {value}")
    return math.log(value)


def exp(value: Interval | float) -> Interval | float:
    """The exponential of a number, or of every value in an interval.

    Rises with its argument throughout, so the ends of the result are the
    exponentials of the ends, each moved two floating-point steps outward
    and never below zero.

    Args:
        value: A number or an interval.

    Returns:
        A number for a number; for an interval, an interval holding every
        exponential.
    """
    if isinstance(value, Interval):
        once = _outward(math.exp(value.lo), math.exp(value.hi), floor=0.0)
        return _outward(once.lo, once.hi, floor=0.0)
    return math.exp(value)


def power(value: Interval | float, exponent: float) -> Interval | float:
    """A positive number, or every value in a positive interval, to a power.

    For a positive base a power moves one way throughout: up with the
    base for a positive exponent, down for a negative one. So the ends
    of the result are the powers of the ends.

    Args:
        value: A number or an interval, above zero.
        exponent: The power, any real number.

    Returns:
        A number for a number; for an interval, an interval holding every
        power. Each end is moved two floating-point steps outward,
        because a power is computed less exactly than a sum or a product.

    Raises:
        ValueError: If the value, or any part of the interval, is not
            above zero.
    """
    if isinstance(value, Interval):
        if value.lo <= 0:
            raise ValueError(f"power of an interval that is not above zero: {value}")
        ends = sorted((math.pow(value.lo, exponent), math.pow(value.hi, exponent)))
        once = _outward(ends[0], ends[1], floor=0.0)
        return _outward(once.lo, once.hi, floor=0.0)
    if value <= 0:
        raise ValueError(f"power of a number that is not above zero: {value}")
    return math.pow(value, exponent)


def _interval(value: Interval | float) -> Interval:
    return value if isinstance(value, Interval) else Interval.point(float(value))


def _outward(lo: float, hi: float, floor: float | None = None) -> Interval:
    """Move each end one floating-point step outward."""
    lower = math.nextafter(lo, -math.inf)
    if floor is not None:
        lower = max(lower, floor)
    return Interval(lower, math.nextafter(hi, math.inf))
