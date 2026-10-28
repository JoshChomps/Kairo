# Text placement

How the placer works, how it is scored and how the strategies compare is in
[Text placement](../text-placement.md). This page lists the code.

```{automodule} kairo.text_placement
```

## Solving

```{automodule} kairo.text_placement.solvers
:members: solve, Result, STRATEGIES
```

## Scoring

```{automodule} kairo.text_placement.scoring
:members: Weights, JUDGE, Score, score, Pricer, StaticInfo, Layout
```

## Problems

```{automodule} kairo.text_placement.problem
:members: Problem, Part, Label, Spot, Side, Rules, spot_like
```

## Geometry

```{automodule} kairo.text_placement.geometry
:members: Box
```

## Benchmark scenarios

```{automodule} kairo.text_placement.scenarios
:members:
```

## The size of KiCad's text

```{automodule} kairo.text_placement.kicad_text
:members:
```
