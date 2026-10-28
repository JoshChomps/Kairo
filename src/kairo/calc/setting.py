"""Which values a chip's programmed setting can hold.

A threshold or delay held in a register is ``offset + N * step`` for a
whole code N, within a range the chip accepts. Texas Instruments'
BQ76952 technical reference manual (SLUUBY2B, May 2022, section 13) is a
typical case: its cell overvoltage threshold is a code in units of
50.6 mV, and its charge overcurrent threshold a code in units of 2 mV.
So a design that relies on a value between two steps gets one of its
neighbours, and one outside the range is refused or clipped.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: How close to a whole step a value must be to count as on it: a part
#: in a million of a step, so that a value written to a few significant
#: figures (50.6 mV times 84 is 4.2504 V) still counts.
_ON_STEP = 1e-6
#: Significant figures a value written in a design is taken to hold.
_WRITTEN = 4


@dataclass(frozen=True, slots=True)
class Settable:
    """What the chip makes of a value.

    Attributes:
        code: The nearest whole code.
        exact: True if the value is on a step, to the figures it is
            written with.
        in_range: True if the value is within the accepted range.
        below: The nearest value the chip holds at or below the value.
        above: The nearest value the chip holds at or above it.
        low: The least the chip accepts, if stated.
        high: The most it accepts, if stated.
    """

    code: int
    exact: bool
    in_range: bool
    below: float
    above: float
    low: float | None
    high: float | None


def settable(
    value: float, step: float, offset: float, low: float | None, high: float | None
) -> Settable:
    """Find the codes either side of a value, and whether the chip holds it.

    A value counts as on a step when it matches that step's value to the
    four significant figures a design writes it with, or to a part in a
    million of a step: 4.25 V is taken as code 84 of 50.6 mV (4.2504 V).

    Args:
        value: The value the design relies on.
        step: One step, above zero.
        offset: The value at code 0.
        low: The least the chip accepts, or ``None``.
        high: The most it accepts, or ``None``.

    Returns:
        The codes and values either side, and whether the value is held.

    Raises:
        ValueError: If the step is not above zero.
    """
    if not step > 0:
        raise ValueError("a setting's step must be above zero")
    position = (value - offset) / step
    code = round(position)
    on = offset + code * step
    exact = abs(position - code) <= _ON_STEP or _same_written(on, value)
    if exact:
        below = above = on
    else:
        below = offset + math.floor(position) * step
        above = offset + math.ceil(position) * step
    in_range = (low is None or value >= low - _ON_STEP * step) and (
        high is None or value <= high + _ON_STEP * step
    )
    return Settable(code, exact, in_range, below, above, low, high)


def _same_written(held: float, written: float) -> bool:
    """Whether two values agree to the figures a design writes a value with."""
    if written == 0:
        return held == 0
    scale = 10 ** (math.floor(math.log10(abs(written))) - (_WRITTEN - 1))
    return abs(held - written) <= scale / 2
