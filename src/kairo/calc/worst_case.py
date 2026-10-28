"""The worst case of a formula over its inputs' tolerance ranges.

A datasheet tolerance is a limit, so whether a requirement holds is
decided by the worst case over those limits, never by sampling at random.
Three things are computed for a formula ``f`` and a box of input ranges:

- **Corners.** Where ``f`` is declared to move one way as an input moves
  across its range, its extremes lie at that input's ends, and trying the
  ends is exact. A function declares this per input, and a test checks
  each declaration.
- **Search.** For an input with no declaration, the extreme may lie
  inside the range. A bounded, repeatable search looks for it: a grid
  over the undeclared inputs, refined around the best point, within a
  budget counted in evaluations. A search finds points that are reached;
  it cannot show nothing worse exists.
- **Enclosure.** The same formula run on intervals gives a range that is
  sure to contain every result, though it may be wider than the truth.

A limit is then judged like this: it **fails** if a point that is reached
breaks it; it **passes** if the corners are exact and meet it, or the
enclosure itself meets it; otherwise it is **unverified**, because the
search found no failure but nothing proves there is none.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from kairo.calc.interval import Interval

#: How an output moves as one input rises: up, down, or not declared.
Direction = Literal["rising", "falling"]
#: Evaluations a search may spend, so it always ends and repeats exactly.
SEARCH_BUDGET = 20_000

Formula = Callable[[Mapping[str, Any]], Any]


@dataclass(frozen=True, slots=True)
class WorstCase:
    """The range of a formula over a box of inputs.

    Attributes:
        minimum: The smallest value found, which is reached.
        maximum: The largest value found, which is reached.
        method: ``monotonic corners`` when every ranged input is declared
            one-way, so the two values are the true extremes; ``search``
            when at least one is not, so they are the worst found.
        exact: True for monotonic corners.
        enclosure: A range sure to contain every result, or ``None`` if
            the formula could not be run on intervals.
        evaluations: How many times the formula was evaluated.
        at_minimum: The inputs that give the minimum.
        at_maximum: The inputs that give the maximum.
    """

    minimum: float
    maximum: float
    method: Literal["monotonic corners", "search"]
    exact: bool
    enclosure: Interval | None
    evaluations: int
    at_minimum: dict[str, float]
    at_maximum: dict[str, float]


def worst_case(
    formula: Formula,
    nominal: Mapping[str, float],
    ranges: Mapping[str, tuple[float, float]],
    directions: Mapping[str, Direction],
    *,
    budget: int = SEARCH_BUDGET,
) -> WorstCase:
    """Find the range of a formula over its inputs' ranges.

    Args:
        formula: The formula, taking its inputs by name. It must work on
            plain numbers; if it also works on `Interval` values, an
            enclosure is computed.
        nominal: Every input's nominal value.
        ranges: The lowest and highest value of each input that varies.
            An input not listed stays at its nominal value.
        directions: For each input the formula is known to move one way
            with, which way.
        budget: The most evaluations a search may spend.

    Returns:
        The extremes found, how they were found, and the enclosure.

    Raises:
        ValueError: If a range names an input the formula does not have,
            or is the wrong way round.
    """
    for name, (low, high) in ranges.items():
        if name not in nominal:
            raise ValueError(f"a range is given for {name!r}, which is not an input")
        if low > high:
            raise ValueError(f"the range of {name!r} is the wrong way round")
    declared = [name for name in ranges if name in directions]
    free = [name for name in ranges if name not in directions]
    count = 0

    def value(point: Mapping[str, float]) -> float:
        nonlocal count
        count += 1
        return float(formula({**nominal, **point}))

    def corner(want_maximum: bool) -> dict[str, float]:
        # Each declared input sits at the end that pushes the result the
        # wanted way.
        return {
            name: ranges[name][
                1 if (directions[name] == "rising") == want_maximum else 0
            ]
            for name in declared
        }

    if not free:
        low_at, high_at = corner(False), corner(True)
        result = WorstCase(
            value(low_at),
            value(high_at),
            "monotonic corners",
            True,
            _enclosure(formula, nominal, ranges),
            count,
            low_at,
            high_at,
        )
        return result

    best: dict[bool, tuple[float, dict[str, float]]] = {}
    for want_maximum in (False, True):
        fixed = corner(want_maximum)
        share = max(budget // 2, 1)
        best[want_maximum] = _search(
            lambda point, fixed=fixed: value({**fixed, **point}),
            {name: ranges[name] for name in free},
            want_maximum,
            share,
        )
        best[want_maximum] = (best[want_maximum][0], {**fixed, **best[want_maximum][1]})
    return WorstCase(
        best[False][0],
        best[True][0],
        "search",
        False,
        _enclosure(formula, nominal, ranges),
        count,
        best[False][1],
        best[True][1],
    )


def judge(
    case: WorstCase, limit: float, kind: Literal["at most", "at least"]
) -> Literal["pass", "fail", "unverified"]:
    """Decide whether a limit holds over the whole range.

    Args:
        case: The worst case of the quantity.
        limit: The limit.
        kind: ``at most`` if the quantity must not exceed the limit,
            ``at least`` if it must not fall below it.

    Returns:
        ``fail`` if a reached value breaks the limit; ``pass`` if the
        corners are exact and meet it or the enclosure meets it;
        otherwise ``unverified``.
    """
    reached = case.maximum if kind == "at most" else case.minimum
    broken = reached > limit if kind == "at most" else reached < limit
    if broken:
        return "fail"
    if case.exact:
        return "pass"
    if case.enclosure is not None:
        bound = case.enclosure.hi if kind == "at most" else case.enclosure.lo
        if (bound <= limit) if kind == "at most" else (bound >= limit):
            return "pass"
    return "unverified"


def _enclosure(
    formula: Formula,
    nominal: Mapping[str, float],
    ranges: Mapping[str, tuple[float, float]],
) -> Interval | None:
    """Run the formula on intervals, if it can be."""
    inputs: dict[str, Any] = {
        name: Interval(*ranges[name]) if name in ranges else Interval.point(value)
        for name, value in nominal.items()
    }
    try:
        result = formula(inputs)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return result if isinstance(result, Interval) else None


def _search(
    value: Callable[[Mapping[str, float]], float],
    box: Mapping[str, tuple[float, float]],
    want_maximum: bool,
    budget: int,
) -> tuple[float, dict[str, float]]:
    """Look for an extreme inside a box, on a grid refined around the best.

    The grid has the same points every time, so the result repeats. It
    always includes the box's ends, so a search is never worse than
    trying the corners.
    """
    names = list(box)
    spent = 0
    # As many points per input as the budget allows for a few rounds.
    per_axis = 9
    while per_axis > 3 and per_axis ** len(names) > budget // 4:
        per_axis -= 2
    current = {name: box[name] for name in names}
    best_value: float | None = None
    best_point: dict[str, float] = {name: box[name][0] for name in names}
    if 2 ** len(names) > budget:
        raise ValueError(
            f"{len(names)} inputs with no declared direction are more than a "
            f"budget of {budget} evaluations can search"
        )
    # The box's own corners first, whatever the budget allows after.
    for combination in itertools.product(*(box[name] for name in names)):
        point = dict(zip(names, combination, strict=True))
        found = value(point)
        spent += 1
        if best_value is None or (
            found > best_value if want_maximum else found < best_value
        ):
            best_value, best_point = found, point
    while spent + per_axis ** len(names) <= budget:
        axes = [
            [low + (high - low) * step / (per_axis - 1) for step in range(per_axis)]
            for low, high in (current[name] for name in names)
        ]
        improved = False
        for combination in itertools.product(*axes):
            point = dict(zip(names, combination, strict=True))
            found = value(point)
            spent += 1
            better = best_value is None or (
                found > best_value if want_maximum else found < best_value
            )
            if better:
                best_value, best_point, improved = found, point, True
        # Shrink each range to the cells either side of the best point.
        narrowed: dict[str, tuple[float, float]] = {}
        for name in names:
            low, high = current[name]
            half = (high - low) / (per_axis - 1)
            narrowed[name] = (
                max(box[name][0], best_point[name] - half),
                min(box[name][1], best_point[name] + half),
            )
        if not improved or all(
            narrowed[name][1] - narrowed[name][0] <= 1e-12 * max(abs(box[name][1]), 1.0)
            for name in names
        ):
            break
        current = narrowed
    assert best_value is not None
    return best_value, best_point
