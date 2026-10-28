"""Benchmark the text placer: strategies, search weights, and stability after edits.

Three experiments, all judged by the same fixed weights
(``kairo.text_placement.JUDGE``):

A. Every strategy on every board.
B. Two strategies searching under different weights, to see what the
   weights change in the layouts they produce.
C. Moving one part and re-solving, cold versus warm-started, to see how many
   labels move that did not need to.

Boards come in two sets, scored separately: "seen" boards (seeds 1 to 3 of
each family) that development is allowed to look at, and "fresh" boards
generated from a new seed on every run. A gap between the two is a sign the
code has been tuned to the seen boards.

Run from the repository root::

    uv run python evals/text_placement/run.py
    uv run python evals/text_placement/run.py --fresh-seed 12345 --workers 8

Results are written to ``evals/text_placement/results/`` as JSON (every
run) and Markdown (the summary tables).
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import random
import statistics
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from kairo.text_placement import JUDGE, Problem, Weights, solve
from kairo.text_placement.problem import Part
from kairo.text_placement.scenarios import FAMILIES
from kairo.text_placement.scoring import Pricer

RESULTS = Path(__file__).parent / "results"
SEEN_SEEDS = (1, 2, 3)
FRESH_PER_FAMILY = 3
STRATEGIES = ("greedy", "descent", "anneal", "negotiate", "portfolio")
SEARCH_WEIGHTS: dict[str, Weights] = {
    "judge": JUDGE,
    # The ratio the Refdes Spring Solver prototype used: a misleading label
    # costs less than leaving one out.
    "soft_ambiguity": dataclasses.replace(JUDGE, ambiguous=500.0),
    # Pull labels in hard: three times the judge's distance and centring costs.
    "compact": dataclasses.replace(JUDGE, distance=6.0, off_centre=6.0),
}
WARM_STICKINESS = 8.0


def _boards(fresh_seed: int) -> list[tuple[str, str, int]]:
    rng = random.Random(fresh_seed)
    boards = [(family, "seen", seed) for family in FAMILIES for seed in SEEN_SEEDS]
    for family in FAMILIES:
        boards += [
            (family, "fresh", rng.randrange(10_000, 10**9))
            for _ in range(FRESH_PER_FAMILY)
        ]
    return boards


def _record(
    exp: str,
    family: str,
    board_set: str,
    seed: int,
    strategy: str,
    weighting: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "experiment": exp,
        "family": family,
        "set": board_set,
        "seed": seed,
        "strategy": strategy,
        "weighting": weighting,
        **extra,
    }


def _cannot_fit(problem: Problem) -> int:
    """Labels with no acceptable spot even if every other label were removed.

    No strategy can place these, so they set a floor under the unplaced
    count. Unplaced labels above this floor are the ones a better search
    could still win.
    """
    pricer = Pricer(problem, JUDGE)
    return sum(
        1
        for i in range(len(problem.labels))
        if min(pricer.static_cost(i, s) for s in problem.candidates[i])
        >= JUDGE.unplaced
    )


def _run_one(task: tuple[str, str, str, int, str, str]) -> dict[str, Any]:
    exp, family, board_set, seed, strategy, weighting = task
    problem = FAMILIES[family](seed)
    started = time.perf_counter()
    result = solve(problem, strategy, weights=SEARCH_WEIGHTS[weighting], seed=seed)
    seconds = time.perf_counter() - started
    s = result.score
    return _record(
        exp,
        family,
        board_set,
        seed,
        result.strategy,
        weighting,
        labels=len(problem.labels),
        cannot_fit=_cannot_fit(problem),
        per_label=s.per_label,
        overlapping=s.overlapping,
        ambiguous=s.ambiguous,
        unplaced=s.unplaced,
        too_close=s.too_close,
        turned=s.turned,
        evaluations=result.evaluations,
        stopped_by_budget=result.stopped_by_budget,
        seconds=seconds,
    )


def _moved_one_part(problem: Problem, seed: int) -> Problem:
    """The same problem with one part shifted 1 mm, where it still fits."""
    rng = random.Random(seed)
    parts = list(problem.parts)
    for _ in range(200):
        k = rng.randrange(len(parts))
        dx, dy = rng.choice([(1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0)])
        moved = parts[k].box.translated(dx, dy)
        others = [p.box for j, p in enumerate(parts) if j != k]
        if problem.area.contains(moved) and all(moved.gap(b) > 0.1 for b in others):
            parts[k] = Part(parts[k].ref, moved)
            return dataclasses.replace(problem, parts=tuple(parts))
    return problem


def _run_warm(task: tuple[str, str, int]) -> list[dict[str, Any]]:
    family, board_set, seed = task
    before = FAMILIES[family](seed)
    first = solve(before, "descent", seed=seed)
    after = _moved_one_part(before, seed)
    rows: list[dict[str, Any]] = []
    for mode in ("cold", "warm"):
        started = time.perf_counter()
        if mode == "cold":
            result = solve(after, "descent", seed=seed)
        else:
            sticky = dataclasses.replace(JUDGE, moved=WARM_STICKINESS)
            result = solve(
                after, "descent", weights=sticky, seed=seed, start=first.layout
            )
        moved = sum(
            1
            for old, new in zip(first.layout, result.layout, strict=True)
            if (old is None) != (new is None)
            or (old is not None and new is not None and not old.same_place(new))
        )
        rows.append(
            _record(
                "C",
                family,
                board_set,
                seed,
                "descent",
                mode,
                labels=len(after.labels),
                per_label=result.score.per_label,
                labels_moved=moved,
                seconds=time.perf_counter() - started,
            )
        )
    return rows


def _mean(rows: list[dict[str, Any]], key: str) -> float:
    return statistics.fmean(r[key] for r in rows) if rows else float("nan")


def _total(rows: list[dict[str, Any]], key: str) -> int:
    return sum(int(r[key]) for r in rows)


def _summary(
    records: list[dict[str, Any]], fresh_seed: int, workers: int, wall: float
) -> str:
    lines = [
        f"# Text placement benchmark, {dt.date.today().isoformat()}",
        "",
        f"Fresh seed {fresh_seed}; {workers} worker processes; {wall:.0f} s wall time.",
        "Points are mean penalty per label under the judge (lower is better), averaged",
        "over boards. Fault columns are totals across boards.",
        "",
        "## A. Strategies (search weights = judge)",
        "",
        "Avoidable is unplaced minus labels that cannot fit even alone: the most a",
        "better search could still place.",
        "",
        "| Strategy | Set | Points | Unplaced | Avoidable | Ambiguous | Overlapping "
        "| Mean s |",
        "|---|---|--:|--:|--:|--:|--:|--:|",
    ]
    a_rows = [r for r in records if r["experiment"] == "A"]
    for strategy in STRATEGIES:
        for board_set in ("seen", "fresh"):
            rows = [
                r
                for r in a_rows
                if r["strategy"].split(":")[0] == strategy and r["set"] == board_set
            ]
            lines.append(
                f"| {strategy} | {board_set} | {_mean(rows, 'per_label'):.1f} | "
                f"{_total(rows, 'unplaced')} | "
                f"{_total(rows, 'unplaced') - _total(rows, 'cannot_fit')} | "
                f"{_total(rows, 'ambiguous')} | {_total(rows, 'overlapping')} | "
                f"{_mean(rows, 'seconds'):.1f} |"
            )
    winners: defaultdict[str, int] = defaultdict(int)
    for r in a_rows:
        if r["strategy"].startswith("portfolio:"):
            winners[r["strategy"].split(":")[1]] += 1
    lines += [
        "",
        "Portfolio winners: "
        + ", ".join(f"{k} {v}" for k, v in sorted(winners.items())),
    ]

    lines += [
        "",
        "## A by family (seen and fresh together)",
        "",
        "| Family | " + " | ".join(STRATEGIES) + " |",
        "|---|" + "--:|" * len(STRATEGIES),
    ]
    for family in FAMILIES:
        cells: list[str] = []
        for strategy in STRATEGIES:
            rows = [
                r
                for r in a_rows
                if r["family"] == family and r["strategy"].split(":")[0] == strategy
            ]
            cells.append(f"{_mean(rows, 'per_label'):.1f}")
        lines.append(f"| {family} | " + " | ".join(cells) + " |")

    lines += [
        "",
        "## B. Search weights (all scored by the judge)",
        "",
        "| Strategy | Search weights | Points | Ambiguous | Unplaced | Overlapping |",
        "|---|---|--:|--:|--:|--:|",
    ]
    b_rows = [r for r in records if r["experiment"] in ("A", "B")]
    for strategy in ("descent", "negotiate"):
        for weighting in SEARCH_WEIGHTS:
            rows = [
                r
                for r in b_rows
                if r["strategy"] == strategy and r["weighting"] == weighting
            ]
            lines.append(
                f"| {strategy} | {weighting} | {_mean(rows, 'per_label'):.1f} | "
                f"{_total(rows, 'ambiguous')} | {_total(rows, 'unplaced')} | "
                f"{_total(rows, 'overlapping')} |"
            )

    lines += [
        "",
        "## C. One part moved 1 mm, then re-solved with descent (seen boards)",
        "",
        "| Mode | Points | Labels moved (mean per board) | Mean s |",
        "|---|--:|--:|--:|",
    ]
    for mode in ("cold", "warm"):
        rows = [r for r in records if r["experiment"] == "C" and r["weighting"] == mode]
        lines.append(
            f"| {mode} | {_mean(rows, 'per_label'):.1f} | "
            f"{_mean(rows, 'labels_moved'):.1f} | {_mean(rows, 'seconds'):.1f} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    """Run the three experiments and write the results."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--fresh-seed", type=int, default=time.time_ns() % 1_000_000)
    parser.add_argument(
        "--workers", type=int, default=max((os.cpu_count() or 2) - 2, 1)
    )
    args = parser.parse_args()

    boards = _boards(args.fresh_seed)
    tasks: list[tuple[str, str, str, int, str, str]] = [
        ("A", f, s, seed, strategy, "judge")
        for f, s, seed in boards
        for strategy in STRATEGIES
    ]
    tasks += [
        ("B", f, s, seed, strategy, weighting)
        for f, s, seed in boards
        for strategy in ("descent", "negotiate")
        for weighting in SEARCH_WEIGHTS
        if weighting != "judge"
    ]
    warm_tasks = [(f, s, seed) for f, s, seed in boards if s == "seen"]

    started = time.perf_counter()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        records = list(pool.map(_run_one, tasks, chunksize=1))
        for rows in pool.map(_run_warm, warm_tasks, chunksize=1):
            records += rows
    wall = time.perf_counter() - started

    RESULTS.mkdir(parents=True, exist_ok=True)
    stem = f"{dt.date.today().isoformat()}-fresh{args.fresh_seed}"
    payload = {
        "date": dt.datetime.now().isoformat(timespec="seconds"),
        "fresh_seed": args.fresh_seed,
        "workers": args.workers,
        "wall_seconds": wall,
        "judge": dataclasses.asdict(JUDGE),
        "search_weights": {k: dataclasses.asdict(v) for k, v in SEARCH_WEIGHTS.items()},
        "boards": [{"family": f, "set": s, "seed": seed} for f, s, seed in boards],
        "records": records,
    }
    (RESULTS / f"{stem}.json").write_text(
        json.dumps(payload, indent=1), encoding="utf-8"
    )
    summary = _summary(records, args.fresh_seed, args.workers, wall)
    (RESULTS / f"{stem}.md").write_text(summary, encoding="utf-8")
    print(summary)


if __name__ == "__main__":
    main()
