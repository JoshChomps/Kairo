"""Exact nodal analysis of a linear circuit: a third check beside the simulators.

ngspice and Xyce solve a circuit by iteration and, in time, by steps,
each to its own tolerances, and both read the netlist Kairo writes. For
a circuit of resistors, capacitors, inductors and independent sources
the answer can instead be had exactly from the same circuit, with no
simulator: the modified nodal equations (Ho, Ruehli and Brennan, "The
modified nodal approach to network analysis", IEEE Transactions on
Circuits and Systems 22(6), 1975) are linear, so the steady state is one
solve in rational numbers, and a frequency sweep one solve in complex
numbers at each frequency.

The result is a `Plot` like a simulator's, so the same measurements read
it and every number gets a third value. Any other element (a diode, a
behavioural or controlled source, a maker's subcircuit) is outside what
is solved here and the circuit gets no third check.
"""

from __future__ import annotations

import cmath
from collections.abc import Sequence
from fractions import Fraction

from kairo.spice.circuit import GROUND, Circuit, Element
from kairo.spice.raw import Plot

#: The element kinds solved here: resistor, capacitor, inductor, and
#: independent voltage and current sources.
KINDS = frozenset("RCLVI")

Number = Fraction | complex


class NodalError(ValueError):
    """The circuit holds something exact nodal analysis here does not solve."""


def linear(circuit: Circuit) -> bool:
    """Whether every element of a circuit is one this module solves.

    Args:
        circuit: The circuit.

    Returns:
        True if it holds only resistors, capacitors, inductors and
        independent sources with plain values.
    """
    try:
        for element in circuit.elements:
            _check(element)
    except NodalError:
        return False
    return True


def steady_state(circuit: Circuit) -> Plot:
    """The circuit's DC steady state, solved exactly.

    Capacitors are open and inductors are shorts. Each value is the exact
    binary number the netlist's text stands for, and the arithmetic is in
    rationals, so the only rounding is the final conversion to float.

    Args:
        circuit: A linear circuit.

    Returns:
        A one-point plot with ``v(node)`` for every node and ``i(name)``
        for every voltage source and inductor, in SPICE's sign: positive
        into the first node.

    Raises:
        NodalError: If an element is not solved here, or the equations
            have no single solution.
    """
    solved = _solve(circuit.elements, None)
    return Plot(
        "Exact nodal analysis (operating point)",
        False,
        tuple(solved),
        {name: "voltage" if name.startswith("v(") else "current" for name in solved},
        {name: (complex(value),) for name, value in solved.items()},
    )


def sweep(circuit: Circuit, frequencies: Sequence[float]) -> Plot:
    """The circuit's small-signal response at each frequency, solved directly.

    The input is every source's AC amplitude; DC levels play no part, as
    the circuit is linear.

    Args:
        circuit: A linear circuit.
        frequencies: The frequencies, in hertz.

    Returns:
        A plot with ``frequency`` first and then each node's voltage.

    Raises:
        NodalError: As `steady_state`.
    """
    columns: dict[str, list[complex]] = {}
    for f in frequencies:
        for name, value in _solve(circuit.elements, 2 * cmath.pi * f).items():
            if name.startswith("v("):
                columns.setdefault(name, []).append(complex(value))
    values = {"frequency": tuple(complex(f) for f in frequencies)}
    values.update({name: tuple(column) for name, column in columns.items()})
    return Plot(
        "Exact nodal analysis (AC)",
        True,
        tuple(values),
        {name: "frequency" if name == "frequency" else "voltage" for name in values},
        values,
    )


def _check(element: Element) -> None:
    """Refuse an element this module does not solve."""
    kind = element.name[0].upper()
    if kind not in KINDS or element.expression is not None or len(element.nodes) != 2:
        raise NodalError(f"{element.name} is not a two-terminal linear element")
    if kind in "RCL":
        _plain(element)
    else:
        _levels(element)


def _plain(element: Element) -> Fraction:
    try:
        value = Fraction(float(element.value))
    except ValueError as exc:
        raise NodalError(f"{element.name} has no plain value") from exc
    if value <= 0:
        raise NodalError(f"{element.name} is not above zero")
    return value


def _levels(element: Element) -> tuple[Fraction, Fraction]:
    """A source's DC level and AC amplitude, from ``DC x``, ``DC x AC y`` or ``x``."""
    words = element.value.split()
    dc = ac = Fraction(0)
    try:
        if len(words) == 1:
            dc = Fraction(float(words[0]))
        else:
            pairs = dict(zip(words[::2], words[1::2], strict=True))
            if set(pairs) - {"DC", "AC"}:
                raise NodalError(f"{element.name} is not a steady source")
            dc = Fraction(float(pairs.get("DC", "0")))
            ac = Fraction(float(pairs.get("AC", "0")))
    except ValueError as exc:
        raise NodalError(f"{element.name} is not a steady source") from exc
    return dc, ac


def _solve(elements: Sequence[Element], omega: float | None) -> dict[str, Number]:
    """Node voltages and branch currents, at DC (``omega`` None) or one frequency."""
    for element in elements:
        _check(element)
    nodes: list[str] = []
    for element in elements:
        for node in element.nodes:
            if node != GROUND and node not in nodes:
                nodes.append(node)
    # A voltage source has a branch current; so has an inductor, which is
    # a short at DC and an impedance at a frequency.
    branches = [e for e in elements if e.name[0].upper() in "VL"]
    size = len(nodes) + len(branches)
    zero: Number = Fraction(0) if omega is None else 0j
    matrix: list[list[Number]] = [[zero] * size for _ in range(size)]
    rhs: list[Number] = [zero] * size
    where = {node: i for i, node in enumerate(nodes)}

    def at(node: str) -> int | None:
        return where.get(node)

    def admit(a: str, b: str, y: Number) -> None:
        i, j = at(a), at(b)
        if i is not None:
            matrix[i][i] += y
        if j is not None:
            matrix[j][j] += y
        if i is not None and j is not None:
            matrix[i][j] -= y
            matrix[j][i] -= y

    for element in elements:
        kind = element.name[0].upper()
        a, b = element.nodes
        if kind == "R":
            admit(a, b, 1 / _plain(element))
        elif kind == "C" and omega is not None:
            admit(a, b, 1j * omega * float(_plain(element)))
        elif kind == "I":
            dc, ac = _levels(element)
            level: Number = dc if omega is None else complex(ac)
            # SPICE's current flows from the first node through the source
            # to the second, so it leaves the first and enters the second.
            i, j = at(a), at(b)
            if i is not None:
                rhs[i] -= level
            if j is not None:
                rhs[j] += level
    for k, element in enumerate(branches, start=len(nodes)):
        a, b = element.nodes
        i, j = at(a), at(b)
        # The branch current enters the first node's side of the element.
        if i is not None:
            matrix[i][k] += 1
            matrix[k][i] += 1
        if j is not None:
            matrix[j][k] -= 1
            matrix[k][j] -= 1
        if element.name[0].upper() == "V":
            dc, ac = _levels(element)
            rhs[k] = dc if omega is None else complex(ac)
        elif omega is not None:
            matrix[k][k] -= 1j * omega * float(_plain(element))
    solution = _eliminate(matrix, rhs)
    out: dict[str, Number] = {f"v({n})": solution[i] for n, i in where.items()}
    for k, element in enumerate(branches, start=len(nodes)):
        out[f"i({element.name.lower()})"] = solution[k]
    return out


def _eliminate(matrix: list[list[Number]], rhs: list[Number]) -> list[Number]:
    """Gaussian elimination, choosing the largest pivot in each column."""
    size = len(rhs)
    rows = [[*row, value] for row, value in zip(matrix, rhs, strict=True)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda r: abs(rows[r][column]))
        if rows[pivot][column] == 0:
            raise NodalError("the equations have no single solution")
        rows[column], rows[pivot] = rows[pivot], rows[column]
        head = rows[column]
        for r in range(column + 1, size):
            factor = rows[r][column] / head[column]
            if factor:
                rows[r] = [x - factor * y for x, y in zip(rows[r], head, strict=True)]
    solution: list[Number] = [rows[0][0] * 0] * size
    for r in range(size - 1, -1, -1):
        known = sum(
            (rows[r][c] * solution[c] for c in range(r + 1, size)), rows[r][0] * 0
        )
        solution[r] = (rows[r][size] - known) / rows[r][r]
    return solution
