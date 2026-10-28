"""Exact nodal analysis, against circuits whose answers are known by hand."""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path
from typing import Any

import pytest

from kairo.spice.circuit import Circuit, Element
from kairo.spice.nodal import NodalError, linear, steady_state, sweep


def circuit(*elements: Element) -> Circuit:
    nodes = {n.upper(): n for e in elements for n in e.nodes if n != "0"}
    return Circuit("t", elements, nodes, {}, (), (), {}, {}, False, (), False, ())


def test_a_ladder_comes_out_as_its_exact_fractions() -> None:
    # 3 V into 2k, then 1k to ground in parallel with an inductor into 50
    # ohm: at DC the inductor is a short, so the bottom is 1k || 50.
    ladder = circuit(
        Element("Vsrc_1", ("a", "0"), "DC 3.0"),
        Element("R1", ("a", "m"), "2000.0"),
        Element("R2", ("m", "0"), "1000.0"),
        Element("C1", ("m", "0"), "1e-06"),
        Element("L1", ("m", "k"), "0.001"),
        Element("R3", ("k", "0"), "50.0"),
        Element("Iload_1", ("a", "0"), "DC 0.001"),
    )
    plot = steady_state(ladder)
    bottom = 1000 * 50 / 1050
    assert plot.real("v(m)")[0] == pytest.approx(
        3 * bottom / (2000 + bottom), rel=1e-15
    )
    assert plot.real("v(k)") == plot.real("v(m)")
    # SPICE's sign: a source supplying current carries a negative one. It
    # feeds the ladder and the 1 mA load.
    assert plot.real("i(vsrc_1)")[0] == pytest.approx(-(3 / (2000 + bottom) + 1e-3))
    assert plot.real("i(l1)")[0] == pytest.approx(plot.real("v(k)")[0] / 50)


def test_a_low_pass_matches_its_transfer_function() -> None:
    rc = circuit(
        Element("Vsrc_1", ("i", "0"), "DC 0 AC 1"),
        Element("R1", ("i", "o"), "1000.0"),
        Element("C1", ("o", "0"), "1e-07"),
    )
    frequencies = [10.0, 1591.5494309189535, 1e6]
    plot = sweep(rc, frequencies)
    for f, v in zip(frequencies, plot.values["v(o)"], strict=True):
        assert v == pytest.approx(1 / (1 + 2j * math.pi * f * 1e3 * 1e-7), rel=1e-12)
    # At the corner the gain is exactly 1 / sqrt(2).
    assert abs(plot.values["v(o)"][1]) == pytest.approx(math.sqrt(0.5), rel=1e-12)


def test_a_series_rlc_resonates_where_the_formula_says() -> None:
    rlc = circuit(
        Element("Vsrc_1", ("i", "0"), "DC 0 AC 1"),
        Element("R1", ("i", "a"), "10.0"),
        Element("L1", ("a", "o"), "1e-05"),
        Element("C1", ("o", "0"), "1e-06"),
    )
    f0 = 1 / (2 * math.pi * math.sqrt(1e-5 * 1e-6))
    # At resonance the capacitor's voltage is Q = sqrt(L / C) / R times the input.
    gain = abs(sweep(rlc, [f0]).values["v(o)"][0])
    assert gain == pytest.approx(math.sqrt(1e-5 / 1e-6) / 10, rel=1e-9)


def test_anything_not_linear_is_left_to_the_simulators() -> None:
    diode = circuit(
        Element("Vsrc_1", ("a", "0"), "DC 5.0"),
        Element("D1", ("a", "0"), "DMODEL"),
    )
    assert not linear(diode)
    with pytest.raises(NodalError, match="D1"):
        steady_state(diode)
    step = circuit(
        Element("Vsrc_1", ("a", "0"), "PULSE(0 5 0 1e-6 1e-6 1 2)"),
        Element("R1", ("a", "0"), "1000.0"),
    )
    assert not linear(step)
    floating = circuit(
        Element("Vsrc_1", ("a", "0"), "DC 5.0"),
        Element("C1", ("a", "b"), "1e-06"),
        Element("R1", ("b", "c"), "1000.0"),
    )
    with pytest.raises(NodalError, match="no single solution"):
        steady_state(floating)


def accuracy_cases() -> Any:
    path = Path(__file__).resolve().parents[2] / "evals" / "accuracy" / "circuits.py"
    spec = importlib.util.spec_from_file_location("accuracy_circuits_nodal", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["accuracy_circuits_nodal"] = module
    spec.loader.exec_module(module)
    return module.cases()


def test_the_accuracy_sets_linear_answers_follow_from_their_netlists() -> None:
    # The plan's third check on the accuracy set: each exact answer the
    # set records is worked out again from the circuit as written, with
    # no simulator, so a slip in a netlist or in a formula shows here.
    solved: list[str] = []
    for case in accuracy_cases():
        elements = tuple(
            Element(words[0], (words[1], words[2]), " ".join(words[3:]))
            for words in (line.split() for line in case.body)
        )
        built = circuit(*elements)
        if case.analysis.kind not in ("op", "ac") or not linear(built):
            continue
        if case.analysis.kind == "op":
            plot = steady_state(built)
        else:
            # The simulators' grid: 50 points a decade from the start.
            start = case.analysis.f_start.value
            stop = case.analysis.f_stop.value
            count = round(50 * math.log10(stop / start))
            plot = sweep(built, [start * 10 ** (k / 50) for k in range(count + 1)])
        for check in case.checks:
            assert check.measure(plot) == pytest.approx(
                check.exact, abs=check.budget
            ), (
                case.name,
                check.label,
            )
        solved.append(case.name)
    assert solved == ["resistor ladder", "RC low-pass"]
