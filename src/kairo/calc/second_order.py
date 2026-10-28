"""What a step response's overshoot says about damping and phase margin.

These are the standard results for a second-order system
``H(s) = wn^2 / (s^2 + 2 z wn s + wn^2)`` and for the unity-feedback loop
that has it as its closed-loop response (Franklin, Powell and
Emami-Naeini, "Feedback Control of Dynamic Systems", sections 3.4 and
6.4). A real circuit is rarely exactly second order, so a damping ratio
or phase margin worked out from an overshoot is an equivalent, a way of
saying how ringy a response is, not a measurement of a loop.
"""

from __future__ import annotations

import math


def overshoot_from_damping(zeta: float) -> float:
    """The overshoot of a second-order step response.

    ``Mp = exp(-pi z / sqrt(1 - z^2))``.

    Args:
        zeta: The damping ratio, from 0 up to but not including 1.

    Returns:
        The overshoot as a fraction of the step.

    Raises:
        ValueError: If the damping ratio is outside that range.
    """
    if not 0 <= zeta < 1:
        raise ValueError("overshoot is defined for a damping ratio from 0 to below 1")
    return math.exp(-math.pi * zeta / math.sqrt(1 - zeta * zeta))


def damping_from_overshoot(fraction: float) -> float:
    """The damping ratio that gives an overshoot: the inverse of the above.

    ``z = -ln(Mp) / sqrt(pi^2 + ln(Mp)^2)``.

    Args:
        fraction: The overshoot as a fraction of the step, above 0 and
            at most 1.

    Returns:
        The damping ratio.

    Raises:
        ValueError: If the overshoot is outside that range.
    """
    if not 0 < fraction <= 1:
        raise ValueError("a damping ratio needs an overshoot above 0 and at most 1")
    log = math.log(fraction)
    return -log / math.sqrt(math.pi**2 + log * log)


def phase_margin_from_damping(zeta: float) -> float:
    """The phase margin of the loop whose closed response has a damping ratio.

    ``PM = atan(2 z / sqrt(sqrt(1 + 4 z^4) - 2 z^2))``.

    Args:
        zeta: The damping ratio, above 0.

    Returns:
        The phase margin in degrees.

    Raises:
        ValueError: If the damping ratio is not above 0.
    """
    if zeta <= 0:
        raise ValueError("a phase margin needs a damping ratio above 0")
    return math.degrees(
        math.atan(2 * zeta / math.sqrt(math.sqrt(1 + 4 * zeta**4) - 2 * zeta * zeta))
    )
