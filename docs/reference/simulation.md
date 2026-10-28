# Simulation

A design is turned into a SPICE circuit, run in ngspice and in Xyce, read
back by Kairo's own raw-file reader and measured by one measurement
library. A number is `checked` only when a tightened re-run agrees with
it, the simulators agree with each other, and every model in the circuit
supports that kind of result; otherwise it is `unverified` and says why.

The numbers reported are at nominal values. A limit is judged at the
worst case over the tolerances the design states, and passes only there.

## What to simulate

```{automodule} kairo.spice.request
:members:
```

## The circuit and its netlist

```{automodule} kairo.spice.circuit
:members:
```

## Maker models

```{automodule} kairo.spice.registry
:members:
```

```{automodule} kairo.library.kinds
:members:
```

## Running the simulators

```{automodule} kairo.spice.engines
:members:
```

## Reading results

```{automodule} kairo.spice.raw
:members:
```

## Measuring

```{automodule} kairo.spice.measure
:members:
```

## One simulation, end to end

```{automodule} kairo.spice.simulate
:members:
```

## The worst case over tolerances

```{automodule} kairo.spice.sweep
:members:
```

## Loop gain

```{automodule} kairo.spice.loop
:members:
```

## A sampled spread

```{automodule} kairo.spice.montecarlo
:members:
```

## KiCad's own SPICE netlist

```{automodule} kairo.spice.crosscheck
:members:
```

## Comparing with design practice

```{automodule} kairo.spice.practice
:members:
```

```{automodule} kairo.calc.second_order
:members:
```

## Graphs and tables

```{automodule} kairo.spice.report
:members:
```

```{automodule} kairo.spice.plot
:members:
```
