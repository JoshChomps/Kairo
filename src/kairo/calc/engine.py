"""Run a catalogue calculation on stated inputs.

`calculate` gives the nominal value and, when every input carries a
tolerance, the worst case over those tolerances with the method that
found it and a guaranteed enclosure. An input with no tolerance is never
given one: the worst case is then ``unverified`` and the result names the
input. A missing input makes the whole result ``unverified``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from kairo.calc.catalogue import CATALOGUE, problem
from kairo.calc.result import Accuracy, CalcResult, Corners, Given, Missing
from kairo.calc.worst_case import SEARCH_BUDGET, WorstCase, judge, worst_case
from kairo.units import Quantity


def calculate(
    name: str,
    inputs: Mapping[str, Given],
    *,
    budget: int = SEARCH_BUDGET,
) -> CalcResult:
    """Run one calculation from the catalogue.

    Args:
        name: The calculation's name, a key of `CATALOGUE`.
        inputs: Each input by the name the calculation gives it. A
            `Given` with a tolerance takes part in the worst case.
        budget: The most evaluations a worst-case search may spend.

    Returns:
        The result, with its inputs, what was missing, the worst case and
        the accuracy block.

    Raises:
        KeyError: If there is no such calculation.
        ValueError: If an input is given that the calculation does not
            take, or an input has the wrong unit.
    """
    calc = CATALOGUE[name]
    extra = sorted(set(inputs) - set(calc.inputs))
    if extra:
        raise ValueError(
            f"{name} takes {', '.join(calc.inputs)}; it was also given "
            f"{', '.join(extra)}"
        )
    for key, given in inputs.items():
        if given.value.unit != calc.inputs[key]:
            raise ValueError(
                f"{name}: {key} is in {given.value.unit or 'no unit'}; expected "
                f"{calc.inputs[key] or 'no unit'}"
            )
    used = tuple(inputs[key] for key in calc.inputs if key in inputs)
    missing = tuple(
        Missing(
            key,
            None,
            "the result cannot be computed",
            f"Give {key} ({calc.inputs[key] or 'a plain number'}) for {name}",
        )
        for key in calc.inputs
        if key not in inputs
    )
    if missing:
        return CalcResult(
            name=name,
            status="unverified",
            value=None,
            equation=calc.equation,
            inputs=used,
            missing=missing,
            source=calc.source,
            warnings=calc.notes,
            accuracy=_accuracy(used, missing, None),
        )

    nominal = {key: inputs[key].number for key in calc.inputs}
    outside = problem(calc, nominal, nominal)
    if outside is not None:
        raise ValueError(f"{name}: {outside}; the formula does not hold otherwise")
    value = float(calc.formula(nominal))
    without = [inputs[key].name for key in calc.inputs if inputs[key].tolerance is None]
    reasons = [f"no tolerance on {given_name}" for given_name in without]
    case: WorstCase | None = None
    if not without:
        ranges = {
            key: _range(inputs[key].number, inputs[key].tolerance or 0.0)
            for key in calc.inputs
        }
        outside = problem(
            calc,
            {key: low for key, (low, _) in ranges.items()},
            {key: high for key, (_, high) in ranges.items()},
        )
        if outside is not None:
            # Somewhere in the tolerance box the formula stops holding, so
            # no worst case over the box can be trusted.
            reasons.append(f"over the tolerances, {outside}")
        else:
            case = worst_case(
                calc.formula, nominal, ranges, calc.directions, budget=budget
            )
    # A corner or a searched point outside the enclosure would mean the
    # formula or the interval arithmetic is wrong.
    if (
        case is not None
        and case.enclosure is not None
        and not (
            case.enclosure.contains(case.minimum)
            and case.enclosure.contains(case.maximum)
        )
    ):
        raise AssertionError(
            f"{name}: a reached value lies outside its guaranteed enclosure"
        )
    range_missing = tuple(
        Missing("a worst case", None, reason, "Narrow the tolerances or change values")
        for reason in reasons
        if reason.startswith("over the tolerances")
    )
    return CalcResult(
        name=name,
        status="ok",
        value=Quantity(value, calc.unit),
        equation=calc.equation,
        inputs=used,
        source=calc.source,
        warnings=calc.notes,
        accuracy=_accuracy(used, range_missing, case),
        corners=_corners(case, calc.unit, reasons),
    )


def check_limit(
    result: CalcResult, limit: float, kind: Literal["at most", "at least"]
) -> Literal["pass", "fail", "unverified"]:
    """Decide whether a result respects a limit, by its worst case.

    Args:
        result: A result from `calculate`.
        limit: The limit, in the result's unit.
        kind: ``at most`` or ``at least``.

    Returns:
        ``fail`` if the nominal value or a reached worst case breaks the
        limit; ``pass`` only if the worst case is proven to meet it;
        ``unverified`` if the worst case was not computed or not proven.
    """
    if result.value is None:
        return "unverified"
    nominal = result.value.value
    if (nominal > limit) if kind == "at most" else (nominal < limit):
        return "fail"
    if result.case is None:
        return "unverified"
    return judge(result.case, limit, kind)


def _range(value: float, tolerance_percent: float) -> tuple[float, float]:
    low = value * (1.0 - tolerance_percent / 100.0)
    high = value * (1.0 + tolerance_percent / 100.0)
    return (min(low, high), max(low, high))


def _corners(case: WorstCase | None, unit: str, reasons: list[str]) -> Corners:
    if case is None:
        return Corners(status="unverified", missing=tuple(reasons))
    return Corners(
        status="computed",
        minimum=Quantity(case.minimum, unit),
        maximum=Quantity(case.maximum, unit),
        method=case.method,
        enclosure=(
            None if case.enclosure is None else (case.enclosure.lo, case.enclosure.hi)
        ),
        case=case,
    )


def _accuracy(
    inputs: tuple[Given, ...], missing: tuple[Missing, ...], case: WorstCase | None
) -> Accuracy:
    because: list[str] = []
    resolve: list[str] = []
    for item in missing:
        owner = f"{item.ref}: " if item.ref else ""
        because.append(f"{owner}no {item.name}; {item.effect}")
        resolve.append(item.resolve)
    tolerances: list[dict[str, Any]] = []
    for given in inputs:
        known = given.tolerance is not None
        tolerances.append(
            {
                "ref": given.ref,
                "parameter": given.name,
                "tolerance": f"{given.tolerance:g}%" if known else None,
                "source": given.source if known else "missing",
            }
        )
        if not known:
            owner = f"{given.ref}: " if given.ref else ""
            because.append(f"{owner}no tolerance on {given.name}")
            resolve.append(
                f"Give the tolerance of {given.name}"
                + (f" for {given.ref}" if given.ref else "")
                + ", with where it comes from"
            )
    if case is None:
        worst = {"method": "not computed", "scope": "none", "status": "unverified"}
    else:
        worst = {
            "method": case.method,
            # Initial tolerance only: drift with temperature and age is
            # not in these ranges.
            "scope": "initial",
            "status": "computed" if case.exact else "searched, not proven",
        }
    status: Literal["verified", "unverified"] = (
        "verified" if case is not None and not because else "unverified"
    )
    return Accuracy(status, tuple(tolerances), worst, tuple(because), tuple(resolve))
