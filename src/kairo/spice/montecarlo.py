"""A sampled spread of a circuit's numbers, for a report and never for a verdict.

Sampling rarely reaches the worst case: with ten values spread evenly,
the chance that one sample has every value in the worst tenth of its
band is one in ten thousand million. So a limit is decided by the worst
case (`kairo.spice.sweep`) and a Monte Carlo run only shows how the
numbers are spread when the parts are spread as the user says.

Only a value whose distribution the user states is sampled. A datasheet
tolerance of 1% is a limit: it does not say whether parts cluster at the
middle or sit anywhere in the band, and Kairo does not pick one. Every
other value stays at nominal, and the report lists them.

The random numbers come from Python's Mersenne Twister with the seed in
the request, drawn in a fixed order (sample by sample, each value in the
order the circuit lists them), so the same request gives the same
samples on any machine.
"""

from __future__ import annotations

import math
import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from kairo.library.grounding import Grounded
from kairo.parts.store import PartStore
from kairo.spice.circuit import CircuitError, Varied, build_circuit, varied_values
from kairo.spice.engines import ACCURATE, Profile, Simulator
from kairo.spice.registry import ModelRegistry
from kairo.spice.request import Distribution, SimRequest
from kairo.spice.simulate import Key
from kairo.spice.sweep import WORKERS, evaluator


@dataclass(frozen=True, slots=True)
class Spread:
    """How one metric came out over the samples.

    Attributes:
        metric: The metric's name.
        where: The net or part.
        unit: Its unit.
        lowest: The smallest value sampled.
        highest: The largest.
        mean: The mean.
        deviation: The standard deviation of the samples (n - 1).
        count: How many samples gave this number.
    """

    metric: str
    where: str
    unit: str
    lowest: float
    highest: float
    mean: float
    deviation: float
    count: int

    def as_dict(self) -> dict[str, Any]:
        """The spread as plain data."""
        return {
            "metric": self.metric,
            "where": self.where,
            "unit": self.unit,
            "lowest": self.lowest,
            "highest": self.highest,
            "mean": self.mean,
            "standard_deviation": self.deviation,
            "samples": self.count,
        }


@dataclass(frozen=True, slots=True)
class Sampled:
    """A Monte Carlo run's report.

    Attributes:
        spreads: Each metric's spread.
        sampled: Each value sampled, with its distribution in words.
        held: The values with a tolerance that stayed at nominal because
            no distribution was stated for them.
        failed: How many samples' runs failed.
        seed: The seed used.
        samples: How many samples were asked for.
        simulator: The simulator that ran them.
    """

    spreads: tuple[Spread, ...]
    sampled: tuple[str, ...]
    held: tuple[str, ...]
    failed: int
    seed: int
    samples: int
    simulator: str

    def as_dict(self) -> dict[str, Any]:
        """The report as plain data, with what it is not said first."""
        return {
            "decides": "nothing: a sampled spread is a report. Limits are judged "
            "at the worst case, which sampling rarely reaches.",
            "samples": self.samples,
            "seed": self.seed,
            "simulator": f"{self.simulator} alone, with the accurate settings; "
            "not checked by a second simulator or a tightened run",
            "sampled": list(self.sampled),
            "held_at_nominal": list(self.held),
            "failed_runs": self.failed,
            "spread": [spread.as_dict() for spread in self.spreads],
        }


def monte_carlo(
    grounded: Grounded,
    request: SimRequest,
    simulators: list[Simulator],
    *,
    store: PartStore | None = None,
    profile: Profile = ACCURATE,
    registry: ModelRegistry | None = None,
) -> Sampled:
    """Simulate a circuit many times with its values spread as stated.

    Args:
        grounded: The design with its pins resolved.
        request: The analysis and sources, with ``monte_carlo`` set.
        simulators: The simulators installed; the first that can run the
            circuit is used.
        store: Datasheet facts, for capacitor DC-bias curves.
        profile: The numerical settings.
        registry: Registered model files.

    Returns:
        Each metric's spread and what was and was not sampled.

    Raises:
        CircuitError: If a distribution names something the circuit does
            not vary, or a value with no tolerance to spread within.
        ValueError: If the request asks for no Monte Carlo run.
    """
    asked = request.monte_carlo
    if asked is None:
        raise ValueError("the request has no monte_carlo")
    circuit = build_circuit(grounded, request, store=store, registry=registry)
    varied = varied_values(grounded, circuit)
    by_name = {_name(item): item for item in varied}
    chosen: list[tuple[Varied, Distribution, float]] = []
    for name, distribution in asked.distributions.items():
        item = by_name.get(name)
        if item is None:
            raise CircuitError(
                f"monte_carlo names {name!r}, which is not a value this circuit "
                f"varies; it varies {', '.join(sorted(by_name)) or 'nothing'}"
            )
        if item.tolerance is None:
            raise CircuitError(
                f"{item.label} has no tolerance to spread within. {item.resolve}"
            )
        chosen.append((item, distribution, item.tolerance))
    # The circuit's own order, so the draws do not depend on how the
    # request happened to list them.
    order = {item.element: index for index, item in enumerate(varied)}
    chosen.sort(key=lambda entry: order[entry[0].element])

    generator = random.Random(asked.seed)
    points: list[dict[str, float]] = []
    for _ in range(asked.samples):
        point: dict[str, float] = {}
        for item, distribution, tolerance in chosen:
            if distribution.shape == "uniform":
                offset = generator.uniform(-tolerance, tolerance)
            else:
                assert distribution.sigmas is not None
                offset = generator.gauss(0.0, tolerance / distribution.sigmas)
            point[item.element] = 1.0 + offset / 100.0
        points.append(point)

    usable = [s for s in simulators if s.engine in circuit.engines]
    if not usable:
        raise CircuitError("no installed simulator can run this circuit")
    one = evaluator(
        grounded, request, usable[0], store=store, profile=profile, registry=registry
    )
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        measured = list(pool.map(one, points))

    values: dict[Key, list[float]] = {}
    units: dict[Key, str] = {}
    failed = 0
    for sample in measured:
        if sample is None:
            failed += 1
            continue
        for key, (number, unit) in sample.items():
            values.setdefault(key, []).append(number)
            units[key] = unit
    spreads = tuple(
        Spread(
            key[0],
            key[1],
            units[key],
            min(numbers),
            max(numbers),
            math.fsum(numbers) / len(numbers),
            _deviation(numbers),
            len(numbers),
        )
        for key, numbers in values.items()
    )
    taken = {item.element for item, _, _ in chosen}
    return Sampled(
        spreads,
        tuple(
            f"{item.label}: {_words(distribution)} within {tolerance:g}%"
            for item, distribution, tolerance in chosen
        ),
        tuple(
            item.label
            for item in varied
            if item.element not in taken and item.tolerance is not None
        ),
        failed,
        asked.seed,
        asked.samples,
        usable[0].engine,
    )


def _name(item: Varied) -> str:
    """What a request calls a varied value: the part, or the supply's net."""
    prefix = "the source on "
    if item.label.startswith(prefix):
        return item.label[len(prefix) :]
    return item.label.split("'s ")[0]


def _words(distribution: Distribution) -> str:
    if distribution.shape == "uniform":
        return "uniform"
    return f"normal, the tolerance being {distribution.sigmas:g} standard deviations"


def _deviation(numbers: list[float]) -> float:
    if len(numbers) < 2:
        return 0.0
    mean = math.fsum(numbers) / len(numbers)
    return math.sqrt(math.fsum((n - mean) ** 2 for n in numbers) / (len(numbers) - 1))
