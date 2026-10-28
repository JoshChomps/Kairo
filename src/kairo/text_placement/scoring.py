"""How a text layout is priced.

One cost function serves both the solvers and the judge, so a solver can
never believe a layout is good while the check disagrees. The judge always
uses :data:`JUDGE`; a solver may search under different weights (for
example, punishing ambiguity harder than the judge does) but its result is
reported under :data:`JUDGE`.

The cost of a layout is the mean cost per label, so a lower number is
better and results from boards of different sizes can be compared.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from kairo.text_placement.problem import Problem, Spot

Layout = Sequence[Spot | None]
"""One entry per label, in the problem's label order; None means unplaced."""


@dataclass(frozen=True, slots=True)
class Weights:
    """Penalty points for each way a label can be bad.

    Attributes:
        overlap: Text overlapping another text, a part, or the edge of the
            area. Unreadable, so it outweighs everything else.
        ambiguous: Text nearer another part than its own, so a reader would
            take it for the wrong part's. Priced above ``unplaced`` on
            purpose: a label that names the wrong part misleads, while a
            missing one is reported and can be fixed. With the two the other
            way round, solvers keep misleading labels to save points (see
            ``docs/text-placement.md``).
        unplaced: A label with no acceptable spot.
        too_close: Text closer than the clearance to another text or part,
            scaled from 0 at the clearance to the full value at contact.
        too_far: Text farther from its part than the rules allow.
        distance: Per mm of push beyond the minimum gap.
        off_centre: Per mm away from the middle of the side.
        turned: Text rotated 90° from its normal reading direction.
        off_side: Not on the preferred side. Small, so it only breaks ties.
        moved: Per label whose place changed relative to a previous layout.
            Only solvers use this, to keep a layout stable after an edit; the
            judge leaves it at 0.
    """

    overlap: float = 1000.0
    ambiguous: float = 900.0
    unplaced: float = 600.0
    too_close: float = 400.0
    too_far: float = 300.0
    distance: float = 2.0
    off_centre: float = 2.0
    turned: float = 3.0
    off_side: float = 0.5
    moved: float = 0.0


JUDGE = Weights()
"""The fixed weights every result is reported under."""


@dataclass(frozen=True, slots=True)
class Score:
    """A layout's cost under the judge, with the reasons counted.

    Attributes:
        per_label: Mean penalty points per label. Lower is better.
        overlapping: Labels that overlap something.
        ambiguous: Labels nearer another part than their own.
        unplaced: Labels left without a spot.
        too_close: Labels inside the clearance of something.
        too_far: Labels farther from their part than allowed.
        turned: Labels rotated 90°.
        pinned: Labels a person placed.
    """

    per_label: float
    overlapping: int
    ambiguous: int
    unplaced: int
    too_close: int
    too_far: int
    turned: int
    pinned: int


@dataclass(frozen=True, slots=True)
class StaticInfo:
    """The part of a spot's cost that does not depend on other labels.

    Attributes:
        blocked: The spot overlaps a part or leaves the area.
        closeness: How far inside a part's clearance the spot is, from 0
            (outside it) to 1 (touching).
        ambiguous: The spot is nearer another part than its own.
        too_far: The spot is beyond the maximum distance from its part.
        soft: Distance, centring, turn and side costs, already weighted.
    """

    blocked: bool
    closeness: float
    ambiguous: bool
    too_far: bool
    soft: float


class Pricer:
    """Prices spots for one problem under one set of weights.

    Everything that depends only on the parts is worked out once per spot
    and cached; only label-against-label checks are done at each call.

    Args:
        problem: The problem to price.
        weights: The penalty weights.
        previous: An earlier layout to stay close to. Only used when
            ``weights.moved`` is non-zero.
    """

    def __init__(
        self, problem: Problem, weights: Weights, previous: Layout | None = None
    ) -> None:
        """Set up the caches for a problem."""
        self.problem = problem
        self.weights = weights
        self.previous = previous
        self.evaluations = 0
        self._static: dict[tuple[int, int], StaticInfo] = {}
        # Parts by the grid cells their boxes touch, so a spot is compared
        # with the parts near it and not with every part on the sheet.
        self._cells: dict[tuple[int, int], list[int]] = {}
        for index, part in enumerate(problem.parts):
            for cell in _cells_of(part.box.x0, part.box.y0, part.box.x1, part.box.y1):
                self._cells.setdefault(cell, []).append(index)
        self._spot_ids = [
            {id(spot): k for k, spot in enumerate(cands)}
            for cands in problem.candidates
        ]

    def cost(self, i: int, spot: Spot | None, layout: Layout) -> float:
        """Price label ``i`` at ``spot``, given where the other labels are.

        Args:
            i: The label's index.
            spot: The spot to price, or None for unplaced.
            layout: The current layout; entry ``i`` itself is ignored.

        Returns:
            Penalty points for this label alone.
        """
        self.evaluations += 1
        w = self.weights
        moved = self._moved(i, spot)
        if spot is None:
            return w.unplaced + moved
        st = self.static_info(i, spot)
        blocked = st.blocked
        closeness = st.closeness
        clearance = self.problem.rules.clearance
        for j, other in enumerate(layout):
            if j == i or other is None:
                continue
            # No early exit on an overlap: the closeness term must cover every
            # label, or the price would depend on the order labels come in.
            if spot.box.overlaps(other.box):
                blocked = True
            d = spot.box.gap(other.box)
            if d < clearance:
                closeness = max(closeness, 1.0 - d / clearance)
        cost = st.soft + moved + w.too_close * closeness
        if blocked:
            cost += w.overlap
        if st.ambiguous:
            cost += w.ambiguous
        if st.too_far:
            cost += w.too_far
        return cost

    def static_cost(self, i: int, spot: Spot | None) -> float:
        """Price label ``i`` at ``spot`` as if no other label existed.

        Args:
            i: The label's index.
            spot: The spot, or None for unplaced.

        Returns:
            Penalty points from parts, the area edge and soft preferences.
        """
        w = self.weights
        if spot is None:
            return w.unplaced + self._moved(i, None)
        st = self.static_info(i, spot)
        return (
            st.soft
            + self._moved(i, spot)
            + w.too_close * st.closeness
            + (w.overlap if st.blocked else 0.0)
            + (w.ambiguous if st.ambiguous else 0.0)
            + (w.too_far if st.too_far else 0.0)
        )

    def _moved(self, i: int, spot: Spot | None) -> float:
        if self.weights.moved == 0.0 or self.previous is None:
            return 0.0
        before = self.previous[i]
        if before is None and spot is None:
            return 0.0
        if before is None or spot is None or not spot.same_place(before):
            return self.weights.moved
        return 0.0

    def static_info(self, i: int, spot: Spot) -> StaticInfo:
        """Work out (or recall) everything about a spot that ignores other labels.

        Args:
            i: The label's index.
            spot: One of that label's candidate spots.

        Returns:
            The spot's fixed facts and soft cost.
        """
        key = (i, self._spot_ids[i].get(id(spot), -1))
        if key[1] >= 0:
            cached = self._static.get(key)
            if cached is not None:
                return cached
        result = self._compute_static(i, spot)
        if key[1] >= 0:
            self._static[key] = result
        return result

    def _compute_static(self, i: int, spot: Spot) -> StaticInfo:
        p = self.problem
        w = self.weights
        label = p.labels[i]
        owner = p.owner_of(i)
        clearance = p.rules.clearance
        blocked = not p.area.contains(spot.box)
        closeness = 0.0
        nearest_other = math.inf
        own = spot.box.gap(owner.box)
        # Another part matters only if it overlaps the spot, is within
        # the clearance, or is nearer than the owner. All three mean a
        # gap below this reach, so parts beyond it are not looked at;
        # what is found is the same as comparing with every part.
        reach = max(clearance, own) + 1e-6
        box = spot.box
        near: set[int] = set()
        for cell in _cells_of(
            box.x0 - reach, box.y0 - reach, box.x1 + reach, box.y1 + reach
        ):
            near.update(self._cells.get(cell, ()))
        for index in near:
            part = p.parts[index]
            if part.ref == owner.ref:
                continue
            if box.overlaps(part.box):
                blocked = True
            d = box.gap(part.box)
            nearest_other = min(nearest_other, d)
            if d < clearance:
                closeness = max(closeness, 1.0 - d / clearance)
        soft = (
            w.distance * spot.push
            + w.off_centre * abs(spot.along)
            + (w.turned if spot.turned else 0.0)
            + (w.off_side if spot.side is not label.preferred else 0.0)
        )
        return StaticInfo(
            blocked=blocked,
            closeness=closeness,
            # A small margin so a label exactly between two parts is not
            # called ambiguous on rounding noise.
            ambiguous=nearest_other + 1e-6 < own,
            too_far=own > p.rules.max_distance,
            soft=soft,
        )


#: The side of a grid cell, in mm: about the size of a small part with its
#: texts, so a spot's neighbourhood is a handful of cells.
CELL = 12.7


def _cells_of(x0: float, y0: float, x1: float, y1: float) -> list[tuple[int, int]]:
    """The grid cells a rectangle touches."""
    return [
        (column, row)
        for column in range(math.floor(x0 / CELL), math.floor(x1 / CELL) + 1)
        for row in range(math.floor(y0 / CELL), math.floor(y1 / CELL) + 1)
    ]


def score(problem: Problem, layout: Layout) -> Score:
    """Judge a layout with the fixed :data:`JUDGE` weights.

    Args:
        problem: The problem the layout is for.
        layout: One spot (or None) per label.

    Returns:
        The score and the count of each kind of fault.

    Raises:
        ValueError: If the layout has the wrong number of entries.
    """
    if len(layout) != len(problem.labels):
        raise ValueError("layout length does not match the number of labels")
    pricer = Pricer(problem, JUDGE)
    total = 0.0
    counts = dict.fromkeys(
        (
            "overlapping",
            "ambiguous",
            "unplaced",
            "too_close",
            "too_far",
            "turned",
            "pinned",
        ),
        0,
    )
    clearance = problem.rules.clearance
    for i, spot in enumerate(layout):
        total += pricer.cost(i, spot, layout)
        if problem.labels[i].pinned is not None:
            counts["pinned"] += 1
        if spot is None:
            counts["unplaced"] += 1
            continue
        st = pricer.static_info(i, spot)
        others = [o for j, o in enumerate(layout) if j != i and o is not None]
        if st.blocked or any(spot.box.overlaps(o.box) for o in others):
            counts["overlapping"] += 1
        elif st.closeness > 0 or any(spot.box.gap(o.box) < clearance for o in others):
            # Counted only when not already overlapping, so each label lands
            # in at most one of the two.
            counts["too_close"] += 1
        counts["ambiguous"] += st.ambiguous
        counts["too_far"] += st.too_far
        counts["turned"] += spot.turned
    per_label = total / len(layout) if layout else 0.0
    return Score(per_label=per_label, **counts)
