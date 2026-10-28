"""What to simulate: the sources to apply, the analysis, and the limits to judge."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kairo.ir.models import quantity_field
from kairo.units import Quantity, QuantityError, parse_quantity

if TYPE_CHECKING:
    Volts = Quantity
    Seconds = Quantity
    Ohms = Quantity
    Henries = Quantity
    Hertz = Quantity
    Percent = Quantity
    Celsius = Quantity
else:
    Volts = quantity_field("V")
    Seconds = quantity_field("s")
    Ohms = quantity_field("ohm")
    Henries = quantity_field("H")
    Hertz = quantity_field("Hz")
    Percent = quantity_field("%")
    Celsius = quantity_field("degC")

#: The metrics a limit can name, with the unit a limit on each is written
#: in. ``voltage``, ``supplied_current``, ``current`` and ``power`` are the
#: steady state's (``op``); ``cutoff`` is a sweep's (``ac``); the rest are a
#: run in time's (``tran``). ``ripple`` is the spread from highest to
#: lowest over the settled part of the run.
METRICS: dict[str, str] = {
    "voltage": "V",
    "supplied_current": "A",
    "current": "A",
    "power": "W",
    "peak": "V",
    "lowest": "V",
    "final": "V",
    "overshoot": "%",
    "rise_time": "s",
    "settling_time": "s",
    "peak_current": "A",
    "cutoff": "Hz",
    "ripple": "V",
}


class _Strict(BaseModel):
    """A model that refuses fields it does not know."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)


class Stimulus(_Strict):
    """A voltage source applied between a net and ground.

    Attributes:
        net: The net it drives.
        kind: ``dc`` for a steady level, ``step`` for a level that changes
            once, ``ac`` for the input of a frequency sweep, ``sine`` for
            a steady level with a sine wave on it, such as a supply's
            ripple, in a run in time.
        value: The level, or the level a step ends at. Not used for
            ``ac``, whose amplitude is 1 so every result is a gain.
        amplitude: A sine's amplitude, half its peak-to-peak.
        frequency: A sine's frequency.
        start: The level a step begins at.
        delay: When a step begins.
        rise: How long a step takes. A supply's edge is never
            instantaneous, and how fast it is decides how hard the
            circuit rings, so a step must say.
        resistance: The source's series resistance, if it has one worth
            modelling: a cable, a connector, a current-limited supply.
        inductance: The source's series inductance, such as a cable's.
    """

    net: str
    kind: Literal["dc", "step", "ac", "sine"] = "dc"
    value: Volts | None = None
    amplitude: Volts | None = None
    frequency: Hertz | None = None
    start: Volts | None = None
    delay: Seconds | None = None
    rise: Seconds | None = None
    resistance: Ohms | None = None
    inductance: Henries | None = None

    @model_validator(mode="after")
    def _complete(self) -> Stimulus:
        if self.kind in ("dc", "step", "sine") and self.value is None:
            raise ValueError(f"a {self.kind} source needs its value, e.g. value: 5V")
        if self.kind == "sine":
            if self.amplitude is None or self.amplitude.value <= 0:
                raise ValueError("a sine needs its amplitude, e.g. amplitude: 50mV")
            if self.frequency is None or self.frequency.value <= 0:
                raise ValueError("a sine needs its frequency, e.g. frequency: 100kHz")
        elif self.amplitude is not None or self.frequency is not None:
            raise ValueError("amplitude and frequency belong to a sine")
        if self.kind == "step":
            if self.rise is None or self.rise.value <= 0:
                raise ValueError(
                    "a step needs its rise time, e.g. rise: 100us; Kairo does "
                    "not choose one, because the edge decides the overshoot"
                )
        elif self.rise is not None or self.start is not None or self.delay is not None:
            raise ValueError("start, delay and rise belong to a step")
        for name in ("resistance", "inductance"):
            given = getattr(self, name)
            if given is not None and given.value <= 0:
                raise ValueError(f"{name} must be above zero; leave it out for none")
        return self


class Analysis(_Strict):
    """Which analysis to run.

    Attributes:
        kind: ``op`` for the steady state, ``tran`` for the response in
            time, ``ac`` for the response against frequency, ``dc`` for
            the steady state as one source is swept.
        duration: How long a ``tran`` runs.
        f_start: Where an ``ac`` sweep begins.
        f_stop: Where it ends.
        sweep: For ``dc``, the net whose source is swept; it must have a
            ``dc`` stimulus or be a supply with no regulator.
        v_start: Where a ``dc`` sweep begins.
        v_stop: Where it ends.
        temperature: The temperature of the run; 27 degC if not given.
            It reaches only models that carry temperature data, which
            Kairo's own stand-ins do not.
        temperatures: The temperature corners at which limits are
            judged as well as at 27 degC, such as -40degC and 85degC.
            At each, every value's tolerance is widened by what its
            part states of its change with temperature.
        ripple_from: For ``tran``, when the settled part of the run
            begins, over which ``ripple`` is measured; the second half
            of the run if not given.
    """

    kind: Literal["op", "tran", "ac", "dc"]
    duration: Seconds | None = None
    f_start: Hertz | None = None
    f_stop: Hertz | None = None
    sweep: str | None = None
    v_start: Volts | None = None
    v_stop: Volts | None = None
    temperature: Celsius | None = None
    temperatures: list[Celsius] | None = None
    ripple_from: Seconds | None = None

    @model_validator(mode="after")
    def _complete(self) -> Analysis:
        if self.temperatures is not None:
            if not self.temperatures:
                raise ValueError("temperatures names at least one corner")
            if self.temperature is not None:
                raise ValueError(
                    "give temperature for one run or temperatures for the "
                    "corners, not both"
                )
        if self.kind == "tran":
            if self.duration is None or self.duration.value <= 0:
                raise ValueError("a tran analysis needs its duration, e.g. 5ms")
            if self.ripple_from is not None and not (
                0 <= self.ripple_from.value < self.duration.value
            ):
                raise ValueError("ripple_from must be within the run's duration")
        elif self.duration is not None:
            raise ValueError("duration belongs to a tran analysis")
        elif self.ripple_from is not None:
            raise ValueError("ripple_from belongs to a tran analysis")
        if self.kind == "ac":
            if self.f_start is None or self.f_stop is None:
                raise ValueError("an ac analysis needs f_start and f_stop")
            if not 0 < self.f_start.value < self.f_stop.value:
                raise ValueError("f_start must be above zero and below f_stop")
        elif self.f_start is not None or self.f_stop is not None:
            raise ValueError("f_start and f_stop belong to an ac analysis")
        if self.kind == "dc":
            if self.sweep is None or self.v_start is None or self.v_stop is None:
                raise ValueError("a dc analysis needs sweep, v_start and v_stop")
            if self.v_start.value >= self.v_stop.value:
                raise ValueError("v_start must be below v_stop")
        elif (
            self.sweep is not None
            or self.v_start is not None
            or self.v_stop is not None
        ):
            raise ValueError("sweep, v_start and v_stop belong to a dc analysis")
        return self


class Limit(_Strict):
    """A bound on one measured value, judged pass, fail or unverified.

    Attributes:
        metric: What is bounded; see `METRICS`.
        net: The net it is measured on, for a voltage metric.
        ref: The part whose current it is, for ``current`` and
            ``peak_current``.
        max: The most it may be, with its unit.
        min: The least it may be, with its unit.
    """

    metric: Literal[
        "voltage",
        "supplied_current",
        "current",
        "power",
        "peak",
        "lowest",
        "final",
        "overshoot",
        "rise_time",
        "settling_time",
        "peak_current",
        "cutoff",
        "ripple",
    ]
    net: str | None = None
    ref: str | None = None
    max: str | None = None
    min: str | None = None

    @model_validator(mode="after")
    def _complete(self) -> Limit:
        if self.max is None and self.min is None:
            raise ValueError("a limit needs max or min")
        if self.metric in ("peak_current", "current", "power"):
            if self.ref is None or self.net is not None:
                raise ValueError(f"a {self.metric} limit names a part with ref")
        elif self.net is None or self.ref is not None:
            raise ValueError(f"a {self.metric} limit names a net")
        for name in ("max", "min"):
            text = getattr(self, name)
            if text is not None:
                try:
                    parse_quantity(text, METRICS[self.metric])
                except QuantityError as exc:
                    raise ValueError(f"{name}: {exc}") from exc
        return self


class Loop(_Strict):
    """A loop-gain measurement at one pin.

    Attributes:
        pin: The pin the loop passes through, as ``REF.PIN``: a feedback
            input, for example.
        into: Which way the loop's signal flows there: ``pin`` if into
            the part, ``net`` if out of it. Needed only where the pin's
            type does not say.
    """

    pin: str
    into: Literal["pin", "net"] | None = None


class Distribution(_Strict):
    """How one value is spread within its tolerance, as the user states it.

    Attributes:
        shape: ``uniform`` for any value within the tolerance equally
            likely; ``normal`` for a bell curve about the nominal value.
        sigmas: For ``normal``, how many standard deviations the
            tolerance is: 3 if a maker states its tolerance as three
            sigma. Samples are not cut off at the tolerance.
    """

    shape: Literal["uniform", "normal"]
    sigmas: float | None = None

    @model_validator(mode="after")
    def _complete(self) -> Distribution:
        if self.shape == "normal":
            if self.sigmas is None or self.sigmas <= 0:
                raise ValueError(
                    "a normal distribution needs sigmas: how many standard "
                    "deviations the tolerance is"
                )
        elif self.sigmas is not None:
            raise ValueError("sigmas belongs to a normal distribution")
        return self


class MonteCarlo(_Strict):
    """A sampled spread, for a report only.

    Attributes:
        samples: How many circuits to simulate, from 2 to 500.
        seed: The seed of the random numbers, so a run can be repeated
            exactly.
        distributions: The distribution of each value sampled, by the
            part's reference, or the net's name for a supply. A datasheet
            tolerance is a limit and says nothing of the spread within
            it, so a value with no distribution stated stays at nominal.
    """

    samples: int = Field(ge=2, le=500)
    seed: int
    distributions: dict[str, Distribution]

    @model_validator(mode="after")
    def _some(self) -> MonteCarlo:
        if not self.distributions:
            raise ValueError(
                "monte_carlo samples only values whose distribution is stated: "
                "give distributions"
            )
        return self


class SimRequest(_Strict):
    """One simulation of a design.

    Attributes:
        analysis: The analysis to run.
        stimuli: The sources to apply. A power net that no regulator
            makes and no stimulus drives gets a steady source at its
            nominal voltage.
        limits: Bounds to judge.
        settle_band: The band about the final value used for settling
            time, as a percentage of the step. 1%, 2% and 5% are all in
            common use, so the result names the band it used.
        sweep: Whether limits that hold at nominal are taken to their
            worst case over the stated tolerances. It costs more runs;
            without it no limit can pass.
        monte_carlo: A sampled spread of every metric, reported beside
            the result. It never decides a limit: the worst case does.
        loop: Measure the gain of the loop through one pin, in place
            of the ordinary ``ac`` sweep: its crossover and margins.
    """

    analysis: Analysis
    stimuli: list[Stimulus] = Field(default_factory=list[Stimulus])
    limits: list[Limit] = Field(default_factory=list[Limit])
    settle_band: Percent | None = None
    sweep: bool = True
    monte_carlo: MonteCarlo | None = None
    loop: Loop | None = None

    @model_validator(mode="after")
    def _consistent(self) -> SimRequest:
        nets = [s.net for s in self.stimuli]
        if len(set(nets)) != len(nets):
            raise ValueError("each net takes one stimulus")
        kinds = {s.kind for s in self.stimuli}
        if self.loop is not None:
            if self.analysis.kind != "ac":
                raise ValueError("a loop measurement is an ac analysis")
            if "ac" in kinds:
                raise ValueError(
                    "a loop measurement injects its own signal: give no ac stimulus"
                )
            if self.limits or self.monte_carlo is not None:
                raise ValueError(
                    "a loop measurement takes no limits and no monte_carlo"
                )
        elif self.analysis.kind == "ac" and "ac" not in kinds:
            raise ValueError(
                "an ac analysis needs one stimulus of kind ac as its input"
            )
        if self.analysis.kind != "ac" and "ac" in kinds:
            raise ValueError("an ac stimulus belongs to an ac analysis")
        for timed in ("step", "sine"):
            if self.analysis.kind != "tran" and timed in kinds:
                raise ValueError(f"a {timed} stimulus belongs to a tran analysis")
        if self.analysis.kind == "dc":
            swept = [s for s in self.stimuli if s.net == self.analysis.sweep]
            if swept and swept[0].kind != "dc":
                raise ValueError("the swept net's stimulus must be of kind dc")
        if sum(1 for s in self.stimuli if s.kind == "ac") > 1:
            raise ValueError("an ac analysis has one input")
        if self.analysis.temperatures is not None and not self.sweep:
            raise ValueError(
                "temperature corners are part of the worst case: leave sweep on"
            )
        if self.settle_band is not None and not 0 < self.settle_band.value < 100:
            raise ValueError("settle_band is a percentage between 0 and 100")
        return self
