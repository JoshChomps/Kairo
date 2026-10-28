"""Measure waveforms: one definition of each metric, whichever simulator ran.

The simulators' own ``.meas`` statements are not used, because each engine
implements them differently. Every function here works on the samples as
stored and interpolates linearly between them, so a metric's error from
sampling is bounded by the spacing of the samples.

The step-response terms follow the usual control-engineering definitions
(for example Franklin, Powell and Emami-Naeini, "Feedback Control of
Dynamic Systems", section 3.3): rise time is the time from 10% to 90% of
the step, overshoot is how far the peak passes the final value as a
fraction of the step, and settling time is when the response last leaves
a band about the final value.
"""

from __future__ import annotations

import cmath
import math
from dataclasses import dataclass


class MeasureError(ValueError):
    """A waveform cannot be measured as asked."""


@dataclass(frozen=True, slots=True)
class Waveform:
    """Samples of one quantity against time.

    Attributes:
        t: The sample times in seconds, increasing.
        y: The value at each time.
    """

    t: tuple[float, ...]
    y: tuple[float, ...]

    def __post_init__(self) -> None:
        """Check that the samples pair up and time does not run backwards."""
        if len(self.t) != len(self.y) or len(self.t) < 2:
            raise MeasureError("a waveform needs two or more samples")
        if any(b < a for a, b in zip(self.t, self.t[1:], strict=False)):
            raise MeasureError("a waveform's times must not decrease")

    def at(self, time: float) -> float:
        """The value at a time, interpolated between samples.

        Args:
            time: When, in seconds, inside the record.

        Returns:
            The value.

        Raises:
            MeasureError: If the time is outside the record.
        """
        if not self.t[0] <= time <= self.t[-1]:
            raise MeasureError(f"{time} s is outside the record")
        low, high = 0, len(self.t) - 1
        while high - low > 1:
            middle = (low + high) // 2
            if self.t[middle] <= time:
                low = middle
            else:
                high = middle
        span = self.t[high] - self.t[low]
        if span == 0:
            return self.y[high]
        share = (time - self.t[low]) / span
        return self.y[low] + share * (self.y[high] - self.y[low])

    @property
    def first(self) -> float:
        """The value at the start of the record."""
        return self.y[0]

    @property
    def last(self) -> float:
        """The value at the end of the record."""
        return self.y[-1]

    @property
    def highest(self) -> float:
        """The largest sample."""
        return max(self.y)

    @property
    def lowest(self) -> float:
        """The smallest sample."""
        return min(self.y)


def time_of_peak(wave: Waveform, *, highest: bool = True) -> float:
    """When a waveform is at its largest or smallest sample.

    Args:
        wave: The waveform.
        highest: The largest sample if true, the smallest if false.

    Returns:
        The time of the first such sample, in seconds.
    """
    target = wave.highest if highest else wave.lowest
    return wave.t[wave.y.index(target)]


def has_settled(wave: Waveform, band: float, *, tail: float = 0.1) -> bool:
    """Whether a waveform has stopped moving by the end of its record.

    It has if, over the last part of the record, it stays within a band
    of its last value. A metric that needs the final value means nothing
    on a record that fails this.

    Args:
        wave: The waveform.
        band: Half the band's width, in the waveform's unit.
        tail: The share of the record's length that must be quiet.

    Returns:
        True if settled.
    """
    start = wave.t[-1] - tail * (wave.t[-1] - wave.t[0])
    return all(
        abs(y - wave.last) <= band
        for t, y in zip(wave.t, wave.y, strict=True)
        if t >= start
    )


def crossing_time(wave: Waveform, level: float, *, after: float | None = None) -> float:
    """The first time a waveform crosses a level.

    Args:
        wave: The waveform.
        level: The level to cross.
        after: Ignore crossings before this time.

    Returns:
        The time, in seconds, interpolated between the two samples either
        side of the level.

    Raises:
        MeasureError: If the waveform never crosses the level.
    """
    begin = wave.t[0] if after is None else after
    for index in range(1, len(wave.t)):
        if wave.t[index] < begin:
            continue
        before, here = wave.y[index - 1] - level, wave.y[index] - level
        if before == 0 and wave.t[index - 1] >= begin:
            return wave.t[index - 1]
        if before * here < 0 or here == 0:
            share = before / (before - here) if before != here else 1.0
            return wave.t[index - 1] + share * (wave.t[index] - wave.t[index - 1])
    raise MeasureError(f"the waveform never crosses {level}")


def rise_time(
    wave: Waveform, start: float, final: float, *, low: float = 0.1, high: float = 0.9
) -> float:
    """The time a step takes to go from 10% to 90% of the way.

    Args:
        wave: The waveform.
        start: The value before the step.
        final: The value it settles at.
        low: The lower fraction of the step.
        high: The upper fraction.

    Returns:
        The time between the first crossing of each level, in seconds.

    Raises:
        MeasureError: If the step has no height or a level is never crossed.
    """
    step = final - start
    if step == 0:
        raise MeasureError("there is no step to time: start and final are equal")
    first = crossing_time(wave, start + low * step)
    return crossing_time(wave, start + high * step, after=first) - first


def overshoot(wave: Waveform, start: float, final: float) -> float:
    """How far a step passes its final value, as a fraction of the step.

    Args:
        wave: The waveform.
        start: The value before the step.
        final: The value it settles at.

    Returns:
        ``(peak - final) / (final - start)`` for a rising step and the
        mirror for a falling one; 0 if it never passes the final value.

    Raises:
        MeasureError: If the step has no height.
    """
    step = final - start
    if step == 0:
        raise MeasureError("there is no step: start and final are equal")
    beyond = wave.highest - final if step > 0 else final - wave.lowest
    return max(0.0, beyond / abs(step))


def settling_time(wave: Waveform, final: float, band: float) -> float:
    """When a waveform last leaves a band about its final value.

    Args:
        wave: The waveform.
        final: The value it settles at.
        band: Half the band's width, in the waveform's unit.

    Returns:
        The time, from the start of the record, after which the waveform
        stays within the band, interpolated at the band's edge; 0 if it
        is never outside.

    Raises:
        MeasureError: If the waveform ends outside the band.
    """
    if band <= 0:
        raise MeasureError("the band must be wider than zero")
    if abs(wave.last - final) > band:
        raise MeasureError("the waveform ends outside the band")
    for index in range(len(wave.t) - 1, -1, -1):
        error = abs(wave.y[index] - final)
        if error > band:
            # The edge is crossed between this sample and the next.
            inside = abs(wave.y[index + 1] - final)
            share = (error - band) / (error - inside)
            edge = wave.t[index] + share * (wave.t[index + 1] - wave.t[index])
            return edge - wave.t[0]
    return 0.0


def peak_to_peak(wave: Waveform, *, after: float | None = None) -> float:
    """The spread between the largest and smallest sample.

    Args:
        wave: The waveform.
        after: Only count samples from this time on.

    Returns:
        The spread, in the waveform's unit.

    Raises:
        MeasureError: If no sample is at or after ``after``.
    """
    begin = wave.t[0] if after is None else after
    kept = [y for t, y in zip(wave.t, wave.y, strict=True) if t >= begin]
    if not kept:
        raise MeasureError(f"no samples at or after {begin} s")
    return max(kept) - min(kept)


def integral(wave: Waveform) -> float:
    """The area under a waveform, by the trapezium rule.

    Used for charge (the integral of a current) and energy (of a power).

    Args:
        wave: The waveform.

    Returns:
        The area, in the waveform's unit times seconds.
    """
    return sum(
        0.5 * (wave.y[i] + wave.y[i - 1]) * (wave.t[i] - wave.t[i - 1])
        for i in range(1, len(wave.t))
    )


def gain_db(response: complex, reference: complex) -> float:
    """A gain in decibels, ``20 * log10(|response / reference|)``.

    Args:
        response: The output phasor.
        reference: The input phasor.

    Returns:
        The gain in dB.

    Raises:
        MeasureError: If either is zero.
    """
    if response == 0 or reference == 0:
        raise MeasureError("a gain in decibels needs both values to be non-zero")
    return 20.0 * math.log10(abs(response / reference))


def phase_degrees(response: complex, reference: complex) -> float:
    """The phase of a response against a reference, in degrees.

    Args:
        response: The output phasor.
        reference: The input phasor.

    Returns:
        The angle in degrees, between -180 and 180.

    Raises:
        MeasureError: If the reference is zero.
    """
    if reference == 0:
        raise MeasureError("a phase needs a non-zero reference")
    return math.degrees(cmath.phase(response / reference))


def frequency_at_gain(
    frequency: tuple[float, ...], gains_db: tuple[float, ...], level_db: float
) -> float:
    """The first frequency at which a gain curve crosses a level.

    Interpolated in the logarithm of frequency, the scale on which a gain
    curve's slope is steady.

    Args:
        frequency: The frequencies in Hz, increasing.
        gains_db: The gain at each, in dB.
        level_db: The level to find, for example -3.0103 for a cutoff.

    Returns:
        The frequency, in Hz.

    Raises:
        MeasureError: If the curve never crosses the level.
    """
    for index in range(1, len(frequency)):
        before, here = gains_db[index - 1] - level_db, gains_db[index] - level_db
        if before == 0:
            return frequency[index - 1]
        if before * here < 0 or here == 0:
            share = before / (before - here)
            low, high = math.log(frequency[index - 1]), math.log(frequency[index])
            return math.exp(low + share * (high - low))
    raise MeasureError(f"the gain never crosses {level_db} dB")
