"""Place text (references, values, net labels) where it can be read.

The placer gives every label a spot clear of parts and other labels that
plainly belongs to its own part, or leaves it unplaced and says so. It works
on axis-aligned boxes only, so the same code serves schematic fields and PCB
silkscreen. See ``docs/text-placement.md`` for the method, the scoring and
the benchmark results.

Typical use::

    from kairo.text_placement import solve
    result = solve(problem)            # the portfolio of strategies
    print(result.score.per_label)      # mean penalty points; lower is better
"""

from kairo.text_placement.geometry import Box
from kairo.text_placement.problem import Label, Part, Problem, Rules, Side, Spot
from kairo.text_placement.scoring import JUDGE, Score, Weights, score
from kairo.text_placement.solvers import STRATEGIES, Result, solve

__all__ = [
    "JUDGE",
    "STRATEGIES",
    "Box",
    "Label",
    "Part",
    "Problem",
    "Result",
    "Rules",
    "Score",
    "Side",
    "Spot",
    "Weights",
    "score",
    "solve",
]
