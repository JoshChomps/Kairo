from __future__ import annotations

import dataclasses

import pytest

from kairo.text_placement import (
    JUDGE,
    STRATEGIES,
    Box,
    Label,
    Part,
    Problem,
    Rules,
    Side,
    score,
    solve,
)
from kairo.text_placement.problem import Spot
from kairo.text_placement.scenarios import FAMILIES, connector_edge
from kairo.text_placement.scoring import Pricer

# --- geometry -----------------------------------------------------------------


def test_touching_boxes_do_not_overlap() -> None:
    a = Box(0, 0, 1, 1)
    assert not a.overlaps(Box(1, 0, 2, 1))
    assert a.overlaps(Box(0.5, 0.5, 2, 2))


def test_gap_is_euclidean_between_corners() -> None:
    assert Box(0, 0, 1, 1).gap(Box(4, 5, 6, 6)) == pytest.approx(5.0)
    assert Box(0, 0, 1, 1).gap(Box(0.5, 0.5, 2, 2)) == 0.0


def test_inverted_box_is_rejected() -> None:
    with pytest.raises(ValueError):
        Box(1, 0, 0, 1)


# --- problem and candidates ---------------------------------------------------


def _two_far_parts() -> Problem:
    parts = (Part("R1", Box(2, 2, 3.6, 2.8)), Part("R2", Box(12, 12, 13.6, 12.8)))
    labels = tuple(Label(p.ref, p.ref, 1.44, 0.8) for p in parts)
    return Problem(parts, labels, Box(0, 0, 20, 20))


def test_no_candidate_overlaps_or_crowds_its_own_part() -> None:
    problem = connector_edge(1)
    for i, spots in enumerate(problem.candidates):
        owner = problem.owner_of(i).box
        for spot in spots:
            assert not spot.box.overlaps(owner)
            assert spot.box.gap(owner) >= problem.rules.gap - 1e-9


def test_pinned_label_has_only_its_pinned_spot() -> None:
    problem = _two_far_parts()
    spot = problem.candidates[0][5]
    pinned = dataclasses.replace(problem.labels[0], pinned=spot)
    problem = dataclasses.replace(problem, labels=(pinned, problem.labels[1]))
    assert problem.candidates[0] == (spot,)


def test_label_for_unknown_part_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown parts"):
        Problem((), (Label("R9", "R9", 1, 1),), Box(0, 0, 1, 1))


# --- scoring ------------------------------------------------------------------


def test_unplaced_label_costs_the_unplaced_weight() -> None:
    problem = _two_far_parts()
    assert score(problem, [None, None]).per_label == JUDGE.unplaced


def test_overlapping_labels_both_pay_the_overlap_cost() -> None:
    problem = _two_far_parts()
    spot = problem.candidates[0][0]
    # Put both labels in the same place, regardless of whose spot it is.
    result = score(problem, [spot, spot])
    assert result.overlapping == 2
    assert result.per_label >= JUDGE.overlap


def _boxed_in() -> Problem:
    """R1 with bars close on every side.

    Above and below, a label fits but ends up nearer the bar than R1, so it
    would read as the bar's. Left and right, the label does not fit at all.
    """
    rules = Rules(gap=0.2, clearance=0.15, step=0.25, pushes=(0.0,), allow_turn=False)
    a = Box(-0.5, -0.5, 0.5, 0.5)
    d = 1.15  # bar distance from R1: label at 0.2..1.0, so 0.15 from the bar
    parts = (
        Part("R1", a),
        Part("N1", Box(-3, a.y1 + d, 3, a.y1 + d + 1)),
        Part("S1", Box(-3, a.y0 - d - 1, 3, a.y0 - d)),
        Part("E1", Box(a.x1 + d, -1, a.x1 + d + 1, 1)),
        Part("W1", Box(a.x0 - d - 1, -1, a.x0 - d, 1)),
    )
    return Problem(
        parts, (Label("R1", "R1", 1.44, 0.8, Side.N),), Box(-10, -10, 10, 10), rules
    )


def test_a_misleading_label_is_worse_than_a_missing_one() -> None:
    # Regression for the weighting: if ambiguity were cheaper than leaving a
    # label out, the solver would put R1's label where it reads as N1's.
    problem = _boxed_in()
    result = solve(problem, "descent")
    assert result.layout == (None,)
    assert result.score.unplaced == 1


def test_softer_ambiguity_weight_does_place_the_misleading_label() -> None:
    problem = _boxed_in()
    soft = dataclasses.replace(JUDGE, ambiguous=500.0)
    result = solve(problem, "descent", weights=soft)
    assert result.layout[0] is not None
    assert result.score.ambiguous == 1


def test_pricer_counts_its_evaluations() -> None:
    problem = _two_far_parts()
    pricer = Pricer(problem, JUDGE)
    pricer.cost(0, problem.candidates[0][0], [None, None])
    assert pricer.evaluations == 1


# --- solvers ------------------------------------------------------------------


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_every_strategy_solves_an_easy_problem_cleanly(strategy: str) -> None:
    result = solve(_two_far_parts(), strategy)
    assert None not in result.layout
    s = result.score
    assert (s.overlapping, s.ambiguous, s.too_close, s.unplaced) == (0, 0, 0, 0)


@pytest.mark.parametrize("strategy", ["anneal", "portfolio"])
def test_same_seed_gives_the_same_layout(strategy: str) -> None:
    problem = connector_edge(2)
    first = solve(problem, strategy, seed=7, budget=150_000)
    again = solve(problem, strategy, seed=7, budget=150_000)
    assert first.layout == again.layout


def test_descent_never_does_worse_than_greedy() -> None:
    problem = connector_edge(1)
    greedy = solve(problem, "greedy").score.per_label
    assert solve(problem, "descent").score.per_label <= greedy + 1e-9


def test_pinned_label_is_never_moved() -> None:
    problem = connector_edge(1)
    spot = problem.candidates[3][0]
    labels = list(problem.labels)
    labels[3] = dataclasses.replace(labels[3], pinned=spot)
    problem = dataclasses.replace(problem, labels=tuple(labels))
    for strategy in ("descent", "negotiate"):
        assert solve(problem, strategy).layout[3] is spot


def test_budget_stops_the_search_and_says_so() -> None:
    result = solve(connector_edge(1), "anneal", budget=2_000)
    assert result.stopped_by_budget
    assert result.evaluations < 10_000


def test_warm_start_on_an_unchanged_problem_moves_nothing() -> None:
    problem = connector_edge(1)
    first = solve(problem, "descent")
    sticky = dataclasses.replace(JUDGE, moved=8.0)
    again = solve(problem, "descent", weights=sticky, start=first.layout)
    moved = [
        k
        for k, (a, b) in enumerate(zip(first.layout, again.layout, strict=True))
        if (a is None) != (b is None)
        or (a is not None and b is not None and not a.same_place(b))
    ]
    assert moved == []


def test_unknown_strategy_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown strategy"):
        solve(_two_far_parts(), "magic")


# --- scenarios ----------------------------------------------------------------


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_scenarios_are_reproducible_and_parts_do_not_overlap(family: str) -> None:
    a = FAMILIES[family](11)
    assert a == FAMILIES[family](11)
    boxes = [p.box for p in a.parts]
    for i, b in enumerate(boxes):
        assert a.area.contains(b)
        assert not any(b.overlaps(c) for c in boxes[i + 1 :])


def test_a_labels_price_does_not_depend_on_label_order() -> None:
    part = Part("R1", Box(0, 0, 1, 1))
    labels = tuple(Label("R1", t, 1.0, 1.0) for t in "abc")
    problem = Problem((part,), labels, Box(-50, -50, 50, 50))

    def at(x: float) -> Spot:
        return Spot(Box(x, 5, x + 1, 6), Side.N, turned=False, along=0.0, push=0.0)

    here, overlapping, near = at(0.0), at(0.5), at(1.05)
    pricer = Pricer(problem, JUDGE)
    assert pricer.cost(0, here, [here, overlapping, near]) == pricer.cost(
        0, here, [here, near, overlapping]
    )


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_looking_only_at_nearby_parts_gives_the_same_facts_as_looking_at_all(
    family: str,
) -> None:
    # The pricer finds a spot's neighbours through a grid. Here every
    # candidate spot of a scenario is priced again against every part on
    # the sheet, and the two must agree exactly.
    problem = FAMILIES[family](3)
    pricer = Pricer(problem, JUDGE)
    clearance = problem.rules.clearance
    checked = 0
    for i, spots in enumerate(problem.candidates):
        owner = problem.owner_of(i)
        for spot in spots:
            blocked = not problem.area.contains(spot.box)
            closeness = 0.0
            nearest = float("inf")
            for part in problem.parts:
                if part.ref == owner.ref:
                    continue
                blocked = blocked or spot.box.overlaps(part.box)
                gap = spot.box.gap(part.box)
                nearest = min(nearest, gap)
                if gap < clearance:
                    closeness = max(closeness, 1.0 - gap / clearance)
            own = spot.box.gap(owner.box)
            got = pricer.static_info(i, spot)
            assert got.blocked == blocked
            assert got.closeness == closeness
            assert got.ambiguous == (nearest + 1e-6 < own)
            assert got.too_far == (own > problem.rules.max_distance)
            checked += 1
    assert checked > 100
