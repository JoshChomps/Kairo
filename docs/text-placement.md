# Text placement

Kairo writes schematics, and later boards, so it has to put reference
designators, values and net labels somewhere a person can read them. The
text placer in `kairo.text_placement` does that. It gives each label a spot
clear of parts and other labels that plainly belongs to its own part, or
leaves the label out and says so. It never stacks text or puts a label where
it would be read as another part's.

The code is Kairo's own. The methods come from the published labelling and
routing literature and from two projects whose write-ups were read for ideas:
the Refdes Spring Solver prototype (candidate spots priced by the exact
score, ejection chains, an adversarially hardened scoring) and TraceMaker
(negotiated congestion, a deterministic portfolio, work budgets). See
[research](research.md) for both.

## The problem

- **Parts** are fixed rectangles: symbol bodies on a schematic, courtyards
  on a board.
- **Labels** belong to one part each and have a width and height. A label
  can be **pinned** to a spot a person chose; pinned labels never move and
  every other label treats them as obstacles.
- Each label may sit at one of a finite set of **candidate spots** around
  its part: every side, stepped along the side, at the minimum gap and at a
  few larger distances, upright or turned 90°. Or it may be left
  **unplaced**.

Working from a finite candidate list is what lets the solver and the judge
price exactly the same thing.

## The scoring

One cost function serves both the search and the final judgement. A
layout's score is the mean penalty per label, so lower is better and boards
of different sizes compare fairly. The judge's weights (`JUDGE`):

| Fault | Points |
|---|--:|
| Overlaps another label, a part, or the edge of the area | 1000 |
| Nearer another part than its own, so it reads as that part's | 900 |
| Unplaced | 600 |
| Inside the clearance of another label or part (scaled: full value at contact) | up to 400 |
| Farther from its part than allowed | 300 |
| Turned 90° | 3 |
| Each mm beyond the minimum gap | 2 |
| Each mm off the middle of its side | 2 |
| Not on its preferred side | 0.5 |

**Why a misleading label costs more than a missing one.** The prototype
these methods came from priced a misplaced label below a hidden one. With
that ratio, a solver keeps a label where it reads as the wrong part's
because it saves points. For Kairo a label naming the wrong part is worse
than no label: a missing label is reported and gets fixed, a wrong one
misleads. It is the same principle as reporting `unverified` instead of
guessing a value. Experiment B below measures what the ratio changes.

## The strategies

All five are deterministic for a given seed, and all stop at a work budget
counted in cost evaluations, not seconds, so results repeat on any machine.

| Strategy | What it does |
|---|---|
| `greedy` | Most constrained label first; each takes its cheapest spot given the labels already placed. The baseline. |
| `descent` | Greedy, then passes where each label, worst first, moves if the total drops. A label that cannot improve alone tries an ejection chain: it takes a better spot and the labels in its way move to their own best spots, kept only if the total drops. |
| `anneal` | Greedy, then simulated annealing with geometric cooling. Returns the best layout it saw rather than the one it ended on, then polishes with descent. |
| `negotiate` | Negotiated congestion, borrowed from routing (PathFinder): labels may share space at first, the price of sharing rises each round, and spots fought over repeatedly get dearer for good. Polished with descent under the exact cost. |
| `portfolio` | Runs descent, anneal and negotiate on equal shares of the budget and keeps the best, ties broken in that order. The default. |

After an edit, `solve(..., start=previous_layout)` starts from the old
layout, and a non-zero `Weights.moved` charges each label that moves, so a
re-solve only moves what it has to.

## The benchmark

`evals/text_placement/run.py` runs three experiments on synthetic boards from
five families (`kairo.text_placement.scenarios`):

| Family | What makes it hard |
|---|---|
| `passive_rows` | Rows of 0603 parts at a pitch tighter than a label needs; many labels cannot fit at all |
| `mcu_module` | A 7 mm chip ringed by small parts |
| `connector_edge` | A long connector on the board edge with parts crowded above it |
| `random_packed` | Mixed sizes dropped at random with little room |
| `schematic_sheet` | Schematic symbols on KiCad's 1.27 mm grid, each with a reference above and a value below |

Each family contributes three **seen** boards (seeds 1 to 3, which
development may look at) and three **fresh** boards generated from a new
seed on each run; the two sets are reported separately. Text width is a
stand-in (0.6 × font height per character); real Kairo drawings will use
sizes calibrated against KiCad's output.

Because many labels on the dense boards cannot fit anywhere, the benchmark
also counts, for each board, the labels that have no acceptable spot even
with every other label removed. **Avoidable** misses are unplaced labels
above that floor: the most any search could still win.

### Results (run of 2026-10-06, fresh seed 20261006)

Full tables and every record are in `evals/text_placement/results/`.

**A. Strategies**, searching under the judge's weights:

| Strategy | Set | Points | Unplaced | Avoidable | Ambiguous | Overlapping | Mean s |
|---|---|--:|--:|--:|--:|--:|--:|
| greedy | seen | 74.9 | 180 | 33 | 0 | 0 | 3.4 |
| greedy | fresh | 81.3 | 201 | 24 | 0 | 0 | 3.5 |
| descent | seen | 69.8 | 169 | 22 | 0 | 0 | 3.4 |
| descent | fresh | 76.5 | 192 | 15 | 0 | 0 | 3.5 |
| anneal | seen | 70.4 | 170 | 23 | 0 | 0 | 13.3 |
| anneal | fresh | 75.2 | 191 | 14 | 0 | 0 | 12.5 |
| negotiate | seen | 69.4 | 168 | 21 | 0 | 0 | 4.3 |
| negotiate | fresh | 76.8 | 193 | 16 | 0 | 0 | 4.0 |
| portfolio | seen | 68.8 | 167 | 20 | 0 | 0 | 15.7 |
| portfolio | fresh | 75.3 | 191 | 14 | 0 | 0 | 15.9 |

- Every strategy beats greedy. The portfolio misses 34 avoidable labels
  across all 30 boards against greedy's 57, so it places about 40% of the
  labels greedy loses.
- No strategy leaves an overlapping or misleading label on any board.
- By family, annealing is clearly ahead on `connector_edge` (3.5 points
  against 6.5 for descent and negotiation); negotiation matches annealing on
  `random_packed` at a third of the time. On `mcu_module` all five give the
  same layout, and `schematic_sheet` is too easy to separate them (0.1).
- Fresh boards score worse than seen ones for every strategy, greedy
  included, which has nothing to tune. They have more labels that cannot fit
  at all (177 against 147), which accounts for the gap; it is not a sign of
  tuning to the seen boards.

**B. Search weights**, each layout still scored by the judge (seen and fresh
together):

| Strategy | Search weights | Points | Ambiguous | Unplaced |
|---|---|--:|--:|--:|
| descent | judge | 73.1 | 0 | 361 |
| descent | prototype ratio (ambiguous 500 < unplaced 600) | 84.5 | 100 | 262 |
| descent | compact (distance and centring ×3) | 73.0 | 0 | 361 |
| negotiate | judge | 73.1 | 0 | 361 |
| negotiate | prototype ratio | 84.2 | 100 | 261 |
| negotiate | compact | 73.1 | 0 | 360 |

- Under the prototype's ratio, a hundred labels that the judge leaves out
  are placed where they read as another part's. This is the reason Kairo's
  judge prices ambiguity above omission.
- Tripling the distance and centring weights changes almost nothing: on
  these boards the layout is decided by what fits, not by fine positioning.

**C. Stability after an edit** (one part moved 1 mm, re-solved with descent,
seen boards):

| Mode | Points | Labels moved per board |
|---|--:|--:|
| cold | 69.6 | 0.9 |
| warm start with a moving cost | 69.6 | 0.8 |

Descent is already stable here: a cold re-solve moves under one label per
board. The warm start works (it is tested to move nothing when nothing
changed) but these boards do not show a benefit; a fair test needs edits
that open or close space for several labels.

### What the results do not show

- The boards are synthetic and the text widths are estimates. The real test
  is on KiCad schematics with calibrated text sizes (M1.10).
- `schematic_sheet` is too easy to rank strategies; a denser, more realistic
  schematic family is needed.
- The `passive_rows` family is mostly about labels that cannot fit; it
  stresses the "leave it out and say so" path more than the search.

## Using it

```python
from kairo.text_placement import solve
from kairo.text_placement.scenarios import mcu_module

result = solve(mcu_module(1))  # portfolio
print(result.strategy, result.score.per_label, result.score.unplaced)
```
