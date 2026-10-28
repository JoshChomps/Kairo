"""Strategies that place labels, and the one entry point that runs them.

Every strategy works on the same finite candidate spots and asks the same
:class:`~kairo.text_placement.scoring.Pricer` what a spot costs, so the
search and the final check agree exactly. Every strategy is deterministic:
the same problem, weights, budget and seed give the same layout, and the
work budget is counted in cost evaluations rather than seconds so that holds
on any machine.

The strategies:

``greedy``
    Most constrained label first, each takes its cheapest spot given the
    labels already placed. Fast, and the baseline the others must beat.
``descent``
    Greedy, then repeated passes where each label, worst first, moves to its
    best spot if the total drops. A label that cannot improve alone tries an
    ejection chain: it takes a better spot and the labels in its way move to
    their own best spots, kept only if the total drops. Ejection chains come
    from the map-labelling and timetabling literature.
``anneal``
    Greedy, then simulated annealing (Kirkpatrick, Gelatt and Vecchi, 1983)
    with a geometric cooling schedule. Unlike a plain annealer it returns the
    best layout it saw, not the one it ended on, then polishes it with
    descent.
``negotiate``
    Negotiated congestion, borrowed from routing (PathFinder, McMurchie and
    Ebeling, 1995): labels may share space at first, the price of sharing
    rises every round, and spots that keep being fought over get more
    expensive for good. Ends with a descent polish under the exact cost.
``portfolio``
    Runs descent, anneal and negotiate, each on an equal share of the
    budget, and keeps the best under the judge, with ties broken by a fixed
    order so the choice is deterministic.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from kairo.text_placement.problem import Problem, Spot, spot_like
from kairo.text_placement.scoring import JUDGE, Layout, Pricer, Score, Weights, score

STRATEGIES = ("greedy", "descent", "anneal", "negotiate", "portfolio")
"""Names accepted by :func:`solve`, in portfolio tie-break order after greedy."""

_EPS = 1e-9


@dataclass(frozen=True, slots=True)
class Result:
    """What a solve produced.

    Attributes:
        strategy: The strategy that produced the layout. For ``portfolio``,
            the member that won, as ``portfolio:<name>``.
        layout: One spot (or None for unplaced) per label.
        score: The layout's score under the judge's weights.
        evaluations: Cost evaluations used.
        stopped_by_budget: True if the work budget ran out before the
            strategy finished on its own.
    """

    strategy: str
    layout: tuple[Spot | None, ...]
    score: Score
    evaluations: int
    stopped_by_budget: bool


class _State:
    """A layout being improved, with each label's current cost kept up to date."""

    def __init__(
        self, problem: Problem, pricer: Pricer, layout: Layout, budget: int
    ) -> None:
        self.problem = problem
        self.pricer = pricer
        self.layout: list[Spot | None] = list(layout)
        self.budget = budget
        self.free = [i for i, lab in enumerate(problem.labels) if lab.pinned is None]
        self.costs = [pricer.cost(i, s, self.layout) for i, s in enumerate(self.layout)]
        self.total = sum(self.costs)
        self._ranked: dict[int, list[tuple[float, Spot | None]]] = {}

    @property
    def exhausted(self) -> bool:
        return self.pricer.evaluations >= self.budget

    def ranked(self, i: int) -> list[tuple[float, Spot | None]]:
        """Label ``i``'s candidates and 'unplaced', cheapest alone first."""
        if i not in self._ranked:
            options: list[Spot | None] = [*self.problem.candidates[i], None]
            priced = [
                (self.pricer.static_cost(i, s), k, s) for k, s in enumerate(options)
            ]
            priced.sort(key=lambda t: (t[0], t[1]))
            self._ranked[i] = [(c, s) for c, _, s in priced]
        return self._ranked[i]

    def best_spot(self, i: int) -> Spot | None:
        """The spot that is cheapest for label ``i`` itself, others fixed.

        A spot's full cost is never below its cost alone, so the scan stops
        as soon as no remaining spot can win.
        """
        best: Spot | None = self.layout[i]
        best_cost = self.costs[i]
        for alone, spot in self.ranked(i):
            if alone >= best_cost - _EPS or self.exhausted:
                break
            c = self.pricer.cost(i, spot, self.layout)
            if c < best_cost - _EPS:
                best, best_cost = spot, c
        return best

    def neighbours(self, spot: Spot | None) -> set[int]:
        """Labels whose cost depends on whether something sits at ``spot``."""
        if spot is None:
            return set()
        clearance = self.problem.rules.clearance
        return {
            j
            for j, other in enumerate(self.layout)
            if other is not None and spot.box.gap(other.box) < clearance
        }

    def try_move(self, i: int, spot: Spot | None) -> tuple[float, dict[int, float]]:
        """Price moving label ``i`` to ``spot`` without keeping the move.

        Returns:
            The change in total cost, and the new cost of every label the
            move affects.
        """
        old = self.layout[i]
        affected = self.neighbours(old) | self.neighbours(spot) | {i}
        before = sum(self.costs[j] for j in affected)
        self.layout[i] = spot
        new = {j: self.pricer.cost(j, self.layout[j], self.layout) for j in affected}
        self.layout[i] = old
        return sum(new.values()) - before, new

    def apply(self, i: int, spot: Spot | None, new_costs: dict[int, float]) -> None:
        self.layout[i] = spot
        for j, c in new_costs.items():
            self.total += c - self.costs[j]
            self.costs[j] = c

    def snapshot(self) -> tuple[list[Spot | None], list[float], float]:
        return list(self.layout), list(self.costs), self.total

    def restore(self, snap: tuple[list[Spot | None], list[float], float]) -> None:
        self.layout, self.costs, self.total = list(snap[0]), list(snap[1]), snap[2]

    def recompute(self) -> None:
        self.costs = [
            self.pricer.cost(i, s, self.layout) for i, s in enumerate(self.layout)
        ]
        self.total = sum(self.costs)


def _start_layout(problem: Problem, start: Layout | None) -> list[Spot | None]:
    """Pinned labels at their spots; with a warm start, the rest where they were."""
    layout: list[Spot | None] = []
    for i, label in enumerate(problem.labels):
        if label.pinned is not None:
            layout.append(label.pinned)
        elif start is not None and start[i] is not None:
            previous = start[i]
            assert previous is not None
            layout.append(spot_like(problem, i, previous))
        else:
            layout.append(None)
    return layout


def _greedy(state: _State) -> None:
    """Place unplaced labels one at a time, most constrained first."""
    clean = state.pricer.weights.too_close  # cost below this means a usable spot

    def usable(i: int) -> int:
        return sum(1 for c, s in state.ranked(i) if s is not None and c < clean)

    todo = [i for i in state.free if state.layout[i] is None]
    for i in sorted(todo, key=lambda i: (usable(i), i)):
        best: Spot | None = None
        best_cost = math.inf
        for alone, spot in state.ranked(i):
            if alone >= best_cost - _EPS or state.exhausted:
                break
            c = state.pricer.cost(i, spot, state.layout)
            if c < best_cost - _EPS:
                best, best_cost = spot, c
        state.layout[i] = best
    state.recompute()


def _descent(state: _State, chain_limit: int = 3, chain_tries: int = 8) -> None:
    """Move labels to better spots, with ejection chains, until nothing improves."""
    w = state.pricer.weights
    improved = True
    while improved and not state.exhausted:
        improved = False
        for i in sorted(state.free, key=lambda i: (-state.costs[i], i)):
            if state.exhausted:
                return
            spot = state.best_spot(i)
            if spot is not state.layout[i]:
                delta, new = state.try_move(i, spot)
                if delta < -_EPS:
                    state.apply(i, spot, new)
                    improved = True
                    continue
            # Worth a chain only for a label that is clearly bad where it is.
            if state.costs[i] >= w.distance * 5 and _eject(
                state, i, chain_limit, chain_tries
            ):
                improved = True


def _eject(state: _State, i: int, chain_limit: int, chain_tries: int) -> bool:
    """Try moving label ``i`` into a better spot and pushing its blockers aside."""
    pinned = {k for k, lab in enumerate(state.problem.labels) if lab.pinned is not None}
    tried = 0
    for alone, spot in state.ranked(i):
        if spot is None or spot is state.layout[i]:
            continue
        if alone >= state.costs[i] - _EPS or tried >= chain_tries or state.exhausted:
            break
        blockers = sorted(state.neighbours(spot) - {i})
        if not blockers or len(blockers) > chain_limit or pinned & set(blockers):
            continue
        tried += 1
        snap = state.snapshot()
        _, new = state.try_move(i, spot)
        state.apply(i, spot, new)
        for j in blockers:
            target = state.best_spot(j)
            _, new = state.try_move(j, target)
            state.apply(j, target, new)
        if state.total < snap[2] - _EPS:
            return True
        state.restore(snap)
    return False


def _anneal(
    state: _State,
    rng: random.Random,
    moves_per_label: int = 300,
    t_start: float = 300.0,
    t_end: float = 1.0,
    choices: int = 24,
) -> None:
    """Simulated annealing that keeps the best layout it saw."""
    if not state.free:
        return
    total_moves = moves_per_label * len(state.free)
    best = state.snapshot()
    for m in range(total_moves):
        if state.exhausted:
            break
        temperature = t_start * (t_end / t_start) ** (m / total_moves)
        i = rng.choice(state.free)
        options = state.ranked(i)[:choices]
        _, spot = options[rng.randrange(len(options))]
        if spot is state.layout[i]:
            continue
        delta, new = state.try_move(i, spot)
        if delta < 0 or rng.random() < math.exp(-delta / temperature):
            state.apply(i, spot, new)
            if state.total < best[2] - _EPS:
                best = state.snapshot()
    state.restore(best)
    _descent(state)


def _negotiate(
    state: _State,
    rounds: int = 40,
    first_price: float = 0.05,
    growth: float = 1.5,
    history_step: float = 0.5,
) -> None:
    """Negotiated congestion: let labels share space, then make sharing expensive."""
    w = state.pricer.weights
    history: defaultdict[tuple[int, int], float] = defaultdict(float)
    price = first_price
    clearance = state.problem.rules.clearance
    for _ in range(rounds):
        if state.exhausted:
            break
        for i in state.free:
            best: Spot | None = state.layout[i]
            best_cost = math.inf
            for alone, spot in state.ranked(i):
                if alone >= best_cost - _EPS:
                    break
                state.pricer.evaluations += 1
                if spot is None:
                    c = alone
                else:
                    conflicts = sum(
                        1
                        for j, other in enumerate(state.layout)
                        if j != i
                        and other is not None
                        and spot.box.gap(other.box) < clearance
                    )
                    c = alone
                    if conflicts:
                        c += (
                            (1.0 + history[(i, id(spot))])
                            * price
                            * conflicts
                            * w.overlap
                        )
                if c < best_cost - _EPS:
                    best, best_cost = spot, c
            state.layout[i] = best
        fought = [
            i
            for i in state.free
            if (spot := state.layout[i]) is not None
            and any(
                j != i and other is not None and spot.box.gap(other.box) < clearance
                for j, other in enumerate(state.layout)
            )
        ]
        if not fought:
            break
        # A spot still fought over after a round gets dearer for good, so the
        # label stops returning to it once the present price alone is not
        # enough to move it.
        for i in fought:
            history[(i, id(state.layout[i]))] += history_step
        price *= growth
    state.recompute()
    _descent(state)


def solve(
    problem: Problem,
    strategy: str = "portfolio",
    *,
    weights: Weights = JUDGE,
    budget: int = 2_000_000,
    seed: int = 0,
    start: Layout | None = None,
) -> Result:
    """Place every label in a problem.

    Args:
        problem: What to place.
        strategy: One of :data:`STRATEGIES`.
        weights: The weights the search optimises. The result is always
            scored under the judge's weights, so different search weights can
            be compared fairly.
        budget: The most cost evaluations to spend. When it runs out the
            best layout so far is returned and ``stopped_by_budget`` is set.
        seed: Seed for the strategies that use randomness.
        start: An earlier layout to start from, for example the layout
            before an edit. Labels keep their side, turn and offset where
            they can. Combine with a non-zero ``weights.moved`` to keep
            labels still unless moving clearly helps.

    Returns:
        The layout, its judged score and the work it took.

    Raises:
        ValueError: If ``strategy`` is not one of :data:`STRATEGIES`, or
            ``start`` has the wrong length.
    """
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}; choose from {STRATEGIES}")
    if start is not None and len(start) != len(problem.labels):
        raise ValueError("start layout length does not match the number of labels")
    if strategy == "portfolio":
        return _portfolio(problem, weights, budget, seed, start)

    initial = _start_layout(problem, start)
    pricer = Pricer(problem, weights, previous=initial if start is not None else None)
    state = _State(problem, pricer, initial, budget)
    runs: dict[str, Callable[[], None]] = {
        "greedy": lambda: None,
        "descent": lambda: _descent(state),
        "anneal": lambda: _anneal(state, random.Random(seed)),
        "negotiate": lambda: _negotiate(state),
    }
    _greedy(state)
    runs[strategy]()
    layout = tuple(state.layout)
    return Result(
        strategy=strategy,
        layout=layout,
        score=score(problem, layout),
        evaluations=pricer.evaluations,
        stopped_by_budget=state.exhausted,
    )


def _portfolio(
    problem: Problem, weights: Weights, budget: int, seed: int, start: Layout | None
) -> Result:
    members = ("descent", "anneal", "negotiate")
    share = max(budget // len(members), 1)
    results = [
        solve(problem, name, weights=weights, budget=share, seed=seed + k, start=start)
        for k, name in enumerate(members)
    ]
    order = {name: k for k, name in enumerate(members)}
    best = min(
        results,
        key=lambda r: (
            r.score.per_label,
            r.score.overlapping,
            r.score.unplaced,
            order[r.strategy],
        ),
    )
    return Result(
        strategy=f"portfolio:{best.strategy}",
        layout=best.layout,
        score=best.score,
        evaluations=sum(r.evaluations for r in results),
        stopped_by_budget=any(r.stopped_by_budget for r in results),
    )
