"""Designs run through the real simulators, against answers known in closed form.

Each test runs on ngspice, and on Xyce as well when it is installed. They
are skipped when ngspice is not found.
"""

# These tests build designs as plain JSON data, which a strict type checker
# cannot follow.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false
from __future__ import annotations

import importlib.util
import math
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from kairo.ir import Design
from kairo.library import LibraryIndex
from kairo.library.grounding import Grounded, ground
from kairo.spice.engines import (
    ACCURATE,
    Simulator,
    commands,
    find_simulator,
    run_netlist,
)
from kairo.spice.practice import review
from kairo.spice.report import charts, table
from kairo.spice.request import SimRequest
from kairo.spice.simulate import SimResult, simulate

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "unit"))
from minilib import (  # pyright: ignore[reportMissingImports]
    ldo,
    make_index,
)

pytestmark = pytest.mark.spice
ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture(scope="module")
def simulators() -> list[Simulator]:
    ngspice, searched = find_simulator("ngspice")
    if ngspice is None:
        pytest.skip(f"ngspice not found (searched: {'; '.join(searched)})")
    xyce, _ = find_simulator("xyce")
    return [ngspice] if xyce is None else [ngspice, xyce]


@pytest.fixture(scope="module")
def index(tmp_path_factory: pytest.TempPathFactory) -> LibraryIndex:
    return make_index(tmp_path_factory.mktemp("libs"))


def accuracy_cases() -> Any:
    path = ROOT / "evals" / "accuracy" / "circuits.py"
    spec = importlib.util.spec_from_file_location("accuracy_circuits", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["accuracy_circuits"] = module
    spec.loader.exec_module(module)
    return module.cases()


def test_every_accuracy_circuit_is_within_its_budget(
    simulators: list[Simulator],
) -> None:
    for simulator in simulators:
        for case in accuracy_cases():
            lines = [
                case.name,
                *case.lines(simulator.engine),
                *commands(simulator.engine, case.analysis, ACCURATE),
                ".end",
            ]
            run = run_netlist(simulator, "\n".join(lines) + "\n", ACCURATE)
            assert run.plot is not None, (simulator.engine, case.name, run.error)
            for check in case.checks:
                error = abs(check.measure(run.plot) - check.exact)
                assert error <= check.budget, (simulator.engine, case.name, check.label)


def regulator(index: LibraryIndex, *, connector: bool = False) -> Grounded:
    data = ldo()
    if not connector:
        # The small library's connector has no model; without it every
        # part of the example has one.
        del data["components"]["J1"]
        for pins in data["connections"].values():
            pins[:] = [p for p in pins if not p.startswith("J1.")]
        for refs in data.get("blocks", {}).values():
            refs[:] = [r for r in refs if r != "J1"]
    return ground(Design.model_validate(data), index)


def passive(
    index: LibraryIndex,
    parts: dict[str, Any],
    nets: dict[str, Any],
    connections: dict[str, list[str]],
    margins: str = "stated",
) -> Grounded:
    return ground(
        Design.model_validate(
            {
                "design": "passive",
                # Only stated tolerances, unless a test says otherwise:
                # most of these are of what a missing one does.
                "policy": {"margins": margins},
                "nets": nets,
                "components": parts,
                "connections": connections,
            }
        ),
        index,
    )


def rc_filter(index: LibraryIndex) -> Grounded:
    return passive(
        index,
        {
            "R1": {"lib_id": "Device:R", "value": "1k"},
            "C1": {
                "lib_id": "Device:C",
                "value": "100nF",
                "params": {"dielectric": "C0G"},
            },
        },
        {
            "IN": {"role": "signal"},
            "OUT": {"role": "signal"},
            "GND": {"role": "ground"},
        },
        {"IN": ["R1.1"], "OUT": ["R1.2", "C1.1"], "GND": ["C1.2"]},
    )


def input_capacitor(index: LibraryIndex) -> Grounded:
    return passive(
        index,
        {
            "C1": {
                "lib_id": "Device:C",
                "value": "10uF",
                "params": {"v_rating": "6.3V"},
            },
            "R1": {"lib_id": "Device:R", "value": "100"},
        },
        {"VIN": {"role": "power", "voltage": "5V"}, "GND": {"role": "ground"}},
        {"VIN": ["C1.1", "R1.1"], "GND": ["C1.2", "R1.2"]},
    )


def run(
    grounded: Grounded, request: dict[str, Any], simulators: list[Simulator]
) -> SimResult:
    return simulate(grounded, SimRequest.model_validate(request), simulators)


def value(result: SimResult, metric: str, where: str) -> float:
    found = result.metric(metric, where)
    assert found is not None, (
        metric,
        where,
        [(m.metric, m.where) for m in result.metrics],
    )
    return found.value


def test_the_regulator_examples_steady_state_matches_the_calculation(
    index: LibraryIndex, simulators: list[Simulator]
) -> None:
    result = run(regulator(index), {"analysis": {"kind": "op"}}, simulators)
    assert result.status == "checked"
    assert value(result, "voltage", "VIN_5V") == pytest.approx(5.0, abs=1e-9)
    assert value(result, "voltage", "+3V3") == pytest.approx(3.3, abs=1e-9)
    # The LED's anode sits at its stated 2 V plus the model's own few millivolts.
    assert value(result, "voltage", "LED_A") == pytest.approx(2.0, abs=0.007)
    # 300 mA of load plus (3.3 V - 2 V) / 1k through the LED.
    assert value(result, "supplied_current", "+3V3") == pytest.approx(0.3013, rel=1e-4)
    assert value(result, "supplied_current", "VIN_5V") == pytest.approx(
        0.3013, rel=1e-4
    )
    assert value(result, "current", "R1") == pytest.approx(1.3e-3, rel=5e-3)
    assert [c.agree for c in result.compared] == [True, True]
    assert "real current is higher" in result.compared[0].note
    if len(simulators) == 2:
        both = result.metric("voltage", "LED_A")
        assert both is not None and set(both.engines) == {"ngspice", "xyce"}
        assert both.engines["ngspice"] == pytest.approx(both.engines["xyce"], rel=1e-3)
    assert both_runs_recorded(result, simulators)


def both_runs_recorded(result: SimResult, simulators: list[Simulator]) -> bool:
    names = [(r.simulator.engine, r.profile.name) for r in result.runs]
    expected = [
        (s.engine, name)
        for s in simulators
        for name in ("accurate", "accurate, tightened")
    ]
    record = result.as_dict()["engines"]
    return names == expected and [e["name"] for e in record] == [
        s.engine for s in simulators
    ]


def test_a_part_with_no_model_makes_every_number_unverified(
    index: LibraryIndex, simulators: list[Simulator]
) -> None:
    result = run(
        regulator(index, connector=True), {"analysis": {"kind": "op"}}, simulators
    )
    assert result.status == "unverified"
    assert all(m.status == "unverified" for m in result.metrics)
    data = result.as_dict()
    assert data["unverified_because"] == [
        "Kairo has no simulation model for J1 (Device:Conn); it is left open"
    ]
    assert "Register a model for Device:Conn" in data["to_resolve"][0]


def test_an_rc_filter_charges_and_rolls_off_as_the_formulas_say(
    index: LibraryIndex, simulators: list[Simulator]
) -> None:
    tau = 1e3 * 100e-9
    request = {
        "analysis": {"kind": "tran", "duration": "1ms"},
        "stimuli": [{"net": "IN", "kind": "step", "value": "3.3V", "rise": "100ns"}],
        "limits": [
            {"metric": "settling_time", "net": "OUT", "max": "400us"},
            {"metric": "overshoot", "net": "OUT", "max": "5%"},
            {"metric": "cutoff", "net": "OUT", "min": "1kHz"},
        ],
    }
    result = run(rc_filter(index), request, simulators)
    assert result.status == "checked", result.as_dict()["unverified_because"]
    assert value(result, "rise_time", "OUT") == pytest.approx(
        tau * math.log(9), rel=2e-3
    )
    assert value(result, "settling_time", "OUT") == pytest.approx(
        tau * math.log(100), rel=2e-3
    )
    assert value(result, "overshoot", "OUT") == pytest.approx(0.0, abs=1e-3)
    assert value(result, "final", "OUT") == pytest.approx(3.3, abs=1e-3)
    # The charging current starts at 3.3 V / 1k.
    assert value(result, "peak_current", "source IN") == pytest.approx(3.3e-3, rel=1e-2)
    verdicts = {j.limit.metric: j for j in result.limits}
    # 460 us is over the 400 us asked for.
    assert verdicts["settling_time"].verdict == "fail"
    assert "above the max of 0.0004 s" in verdicts["settling_time"].detail
    # A limit that holds at nominal is still not a pass.
    assert verdicts["overshoot"].verdict == "unverified"
    assert "tolerances are not swept" in verdicts["overshoot"].detail
    assert verdicts["cutoff"].verdict == "unverified"
    assert "not measured" in verdicts["cutoff"].detail
    assert review(rc_filter(index), SimRequest.model_validate(request), result) == []

    sweep = run(
        rc_filter(index),
        {
            "analysis": {"kind": "ac", "f_start": "10Hz", "f_stop": "1MHz"},
            "stimuli": [{"net": "IN", "kind": "ac"}],
        },
        simulators,
    )
    assert sweep.status == "checked"
    assert value(sweep, "cutoff", "OUT") == pytest.approx(
        1 / (2 * math.pi * tau), rel=1e-3
    )
    assert value(sweep, "gain_at_start", "OUT") == pytest.approx(0.0, abs=1e-3)
    # 20 dB a decade above the cutoff: -55.96 dB at 1 MHz.
    assert value(sweep, "gain_at_stop", "OUT") == pytest.approx(-55.964, abs=0.01)
    assert sweep.metric("cutoff", "IN") is None
    drawn = charts(sweep, "rc")
    assert set(drawn) == {"gain.svg"} and "1 kHz" in drawn["gain.svg"]
    assert table(sweep).splitlines()[0] == 'frequency_Hz,"IN","OUT"'


def test_ripple_through_a_filter_is_the_formulas_and_names_a_missing_esr(
    index: LibraryIndex, simulators: list[Simulator]
) -> None:
    # 5 V with 100 mV of 100 kHz on it, through 1k into 100 nF: settled,
    # the output's peak-to-peak is 2 * A * |H(jw)|, with H = 1 / (1 + jwRC)
    # for an ideal capacitor and (1 + jwC*ESR) / (1 + jwC(R + ESR)) with ESR.
    w, r, c, amplitude = 2 * math.pi * 1e5, 1e3, 100e-9, 0.1

    def exact(esr: float) -> float:
        return (
            2 * amplitude * abs((1 + 1j * w * c * esr) / (1 + 1j * w * c * (r + esr)))
        )

    request = {
        "analysis": {"kind": "tran", "duration": "2ms"},
        "stimuli": [
            {
                "net": "IN",
                "kind": "sine",
                "value": "5V",
                "amplitude": "100mV",
                "frequency": "100kHz",
            }
        ],
        "limits": [{"metric": "ripple", "net": "OUT", "max": "5mV"}],
        "sweep": False,
    }
    ideal = run(rc_filter(index), request, simulators)
    ripple = ideal.metric("ripple", "OUT")
    assert ripple is not None
    assert ripple.value == pytest.approx(exact(0.0), rel=2e-3)
    # Nothing says the capacitor is ideal, so the number is not checked.
    assert ripple.status == "unverified"
    assert "C1 on OUT has no ESR stated" in ripple.reasons[-1]
    assert ideal.limits[0].verdict == "unverified"
    # Ripple is measured only where a limit asks for it.
    assert ideal.metric("ripple", "IN") is None

    def filtered(**params: str) -> Grounded:
        return passive(
            index,
            {
                "R1": {
                    "lib_id": "Device:R",
                    "value": "1k",
                    "params": {"tolerance": "1%"},
                },
                "C1": {
                    "lib_id": "Device:C",
                    "value": "100nF",
                    "params": {"dielectric": "C0G", "tolerance": "5%", **params},
                },
            },
            {
                "IN": {"role": "signal", "tolerance": "2%"},
                "OUT": {"role": "signal"},
                "GND": {"role": "ground"},
            },
            {"IN": ["R1.1"], "OUT": ["R1.2", "C1.1"], "GND": ["C1.2"]},
        )

    with_esr = run(filtered(esr="10ohm"), request, simulators)
    ripple = with_esr.metric("ripple", "OUT")
    assert ripple is not None
    assert ripple.value == pytest.approx(exact(10.0), rel=2e-3)
    assert ripple.status == "checked", ripple.reasons
    assert exact(10.0) > 1.15 * exact(0.0)

    # At the worst case the ESR moves too, over the range the part states;
    # with no range a limit it moves cannot pass. Ripple grows with R and
    # with ESR and falls with C: 4.19 mV at R -1%, C -5%, ESR 12 ohm and
    # the source +2%, where an ESR taken as exact gave 3.98 mV.
    from kairo.spice.sweep import worst_case

    swept = {**request, "sweep": True}
    ranged = worst_case(
        filtered(esr="10ohm", esr_min="8ohm", esr_max="12ohm"),
        SimRequest.model_validate(swept),
        simulators,
    )
    assert ranged.limits[0].verdict == "pass", ranged.limits[0].detail
    assert "C1's ESR +20%" in ranged.limits[0].detail
    w_low, r_low = w * 0.95, r * 0.99
    corner = (
        2
        * amplitude
        * 1.02
        * abs((1 + 1j * w_low * c * 12) / (1 + 1j * w_low * c * (r_low + 12)))
    )
    found = float(ranged.limits[0].detail.split(" V at the worst case")[0])
    assert found == pytest.approx(corner, rel=2e-3)
    bare = worst_case(
        filtered(esr="10ohm"), SimRequest.model_validate(swept), simulators
    )
    assert bare.limits[0].verdict == "unverified"
    assert "C1's ESR, which has no stated range" in bare.limits[0].detail

    for wrong, says in (
        ({"kind": "sine", "value": "5V", "frequency": "1kHz"}, "amplitude"),
        ({"kind": "sine", "value": "5V", "amplitude": "1V"}, "frequency"),
        ({"kind": "dc", "value": "5V", "amplitude": "1V"}, "belong to a sine"),
    ):
        with pytest.raises(ValueError, match=says):
            SimRequest.model_validate({**request, "stimuli": [{"net": "IN", **wrong}]})
    with pytest.raises(ValueError, match="belongs to a tran"):
        SimRequest.model_validate(
            {"analysis": {"kind": "op"}, "stimuli": request["stimuli"]}
        )
    with pytest.raises(ValueError, match="ripple_from"):
        SimRequest.model_validate(
            {
                **request,
                "analysis": {"kind": "tran", "duration": "2ms", "ripple_from": "3ms"},
            }
        )


def test_settling_time_is_counted_from_when_the_step_begins(
    index: LibraryIndex, simulators: list[Simulator]
) -> None:
    tau = 1e3 * 100e-9
    request = {
        "analysis": {"kind": "tran", "duration": "2ms"},
        "stimuli": [
            {
                "net": "IN",
                "kind": "step",
                "value": "3.3V",
                "rise": "100ns",
                "delay": "1ms",
            }
        ],
    }
    result = run(rc_filter(index), request, simulators[:1])
    # Not the 1.46 ms from the start of the record.
    assert value(result, "settling_time", "OUT") == pytest.approx(
        tau * math.log(100), rel=5e-3
    )
    assert value(result, "rise_time", "OUT") == pytest.approx(
        tau * math.log(9), rel=5e-3
    )


def test_a_steady_state_can_be_held_to_limits(
    index: LibraryIndex, simulators: list[Simulator]
) -> None:
    request = {
        "analysis": {"kind": "op"},
        "limits": [
            {"metric": "voltage", "net": "+3V3", "min": "3.4V"},
            {"metric": "voltage", "net": "+3V3", "min": "3.2V", "max": "3.4V"},
            {"metric": "supplied_current", "net": "VIN_5V", "max": "250mA"},
            {"metric": "current", "ref": "R1", "max": "2mA"},
        ],
    }
    result = run(regulator(index), request, simulators)
    verdicts = [j.verdict for j in result.limits]
    # 3.3 V is below 3.4 V and 301 mA is above 250 mA: both fail. What
    # holds at nominal is still not a pass.
    assert verdicts == ["fail", "unverified", "fail", "unverified"]
    assert "below the min of 3.4 V" in result.limits[0].detail


def test_a_run_that_stops_early_is_a_failed_run_not_half_a_result(
    index: LibraryIndex, simulators: list[Simulator]
) -> None:
    from kairo.spice.simulate import whole_run

    request = SimRequest.model_validate(
        {
            "analysis": {"kind": "tran", "duration": "1ms"},
            "stimuli": [{"net": "IN", "kind": "step", "value": "1V", "rise": "1us"}],
        }
    )
    longer = SimRequest.model_validate(
        {
            "analysis": {"kind": "tran", "duration": "2ms"},
            "stimuli": [{"net": "IN", "kind": "step", "value": "1V", "rise": "1us"}],
        }
    )
    result = simulate(rc_filter(index), request, simulators[:1])
    whole = result.runs[0]
    assert whole.plot is not None and whole_run(whole, request) is whole
    # The same results judged against a run asked to go twice as long.
    cut = whole_run(whole, longer)
    assert cut.plot is None and cut.error.startswith("stopped at 0.001 s of 0.002 s")


def hot_plug(resistance: str) -> dict[str, Any]:
    return {
        "analysis": {"kind": "tran", "duration": "400us"},
        "stimuli": [
            {
                "net": "VIN",
                "kind": "step",
                "value": "5V",
                "rise": "100ns",
                "resistance": resistance,
                "inductance": "1uH",
            }
        ],
    }


def test_a_live_supply_plugged_into_a_ceramic_capacitor_rings_past_its_rating(
    index: LibraryIndex, simulators: list[Simulator]
) -> None:
    grounded = input_capacitor(index)
    request = hot_plug("100mohm")
    result = run(grounded, request, simulators)
    assert result.status == "checked", result.as_dict()["unverified_because"]
    # A series RLC of 0.1 ohm, 1 uH and 10 uF with 100 ohm across the
    # capacitor: damping ratio R/2 * sqrt(C/L) + 1/(2 Rload) * sqrt(L/C)
    # = 0.160, so the first peak is about 1.6 times the step.
    zeta = 0.1 / 2 * math.sqrt(10e-6 / 1e-6) + 1 / (2 * 100) * math.sqrt(1e-6 / 10e-6)
    peak = 5 * (1 + math.exp(-math.pi * zeta / math.sqrt(1 - zeta**2)))
    assert value(result, "peak", "VIN") == pytest.approx(peak, rel=3e-3)
    assert value(result, "final", "VIN") == pytest.approx(5.0, abs=0.01)
    advice = {
        a.code: a for a in review(grounded, SimRequest.model_validate(request), result)
    }
    assert next(iter(advice)) == "A_OVER_VOLTAGE_RATING"
    assert advice["A_OVER_VOLTAGE_RATING"].severity == "error"
    assert "rated for 6.3 V" in advice["A_OVER_VOLTAGE_RATING"].message
    assert advice["A_HOT_PLUG_RINGING"].severity == "warning"
    assert "AN88" in advice["A_HOT_PLUG_RINGING"].source
    assert advice["A_HOT_PLUG_RINGING"].checked
    assert "A_INRUSH" in advice and "SLYT332" in advice["A_INRUSH"].source
    drawn = charts(result, "plug")
    assert set(drawn) == {"voltages.svg", "currents.svg"}
    assert table(result).startswith('time_s,"VIN","current from the source on VIN"\n')


def test_enough_series_resistance_stops_the_ringing(
    index: LibraryIndex, simulators: list[Simulator]
) -> None:
    # Critical damping is R = 2 * sqrt(L / C) = 0.632 ohm; 1 ohm is past it.
    grounded = input_capacitor(index)
    request = hot_plug("1ohm")
    result = run(grounded, request, simulators)
    assert result.status == "checked"
    assert value(result, "peak", "VIN") < 5.0
    assert value(result, "overshoot", "VIN") == pytest.approx(0.0, abs=0.01)
    codes = [
        a.code for a in review(grounded, SimRequest.model_validate(request), result)
    ]
    assert "A_HOT_PLUG_RINGING" not in codes and "A_OVER_VOLTAGE_RATING" not in codes


def test_a_step_with_no_inductance_says_what_it_cannot_show(
    index: LibraryIndex, simulators: list[Simulator]
) -> None:
    grounded = input_capacitor(index)
    request = {
        "analysis": {"kind": "tran", "duration": "1ms"},
        "stimuli": [{"net": "VIN", "kind": "step", "value": "5V", "rise": "100us"}],
        "limits": [{"metric": "peak_current", "ref": "C1", "max": "100mA"}],
    }
    result = run(grounded, request, simulators)
    advice = {
        a.code: a for a in review(grounded, SimRequest.model_validate(request), result)
    }
    assert "twice the supply voltage" in advice["A_HOT_PLUG_NOT_ASSESSED"].message
    # C * dV/dt = 10 uF * 5 V / 100 us.
    inrush = result.metric("peak_current", "C1")
    assert inrush is not None and inrush.value == pytest.approx(0.5, rel=1e-2)
    # Nothing stated limits that current but the edge, so it is not checked.
    assert inrush.status == "unverified"
    assert "set by the edge alone" in inrush.reasons[0]
    assert result.limits[0].verdict == "unverified"
    assert value(result, "peak", "VIN") == pytest.approx(5.0, abs=1e-6)


def test_a_run_in_time_through_the_regulator_model_is_never_checked(
    index: LibraryIndex, simulators: list[Simulator]
) -> None:
    request = {
        "analysis": {"kind": "tran", "duration": "5ms"},
        "stimuli": [{"net": "VIN_5V", "kind": "step", "value": "5V", "rise": "100us"}],
    }
    result = run(regulator(index), request, simulators[:1])
    assert result.status == "unverified"
    reasons = result.as_dict()["unverified_because"]
    assert any("U1 is a behavioural model (kairo:ldo_macro)" in r for r in reasons)
    assert value(result, "final", "+3V3") == pytest.approx(3.3, abs=1e-3)
    assert "Only ngspice ran" in " ".join(result.notes)
    assert "(unverified: see the result)" in charts(result, "ldo")["voltages.svg"]


def test_settings_too_loose_for_the_circuit_come_back_unstable(
    index: LibraryIndex, simulators: list[Simulator]
) -> None:
    # Ten steps for a response that rings forty times cannot follow it.
    loose = replace(ACCURATE, name="loose", reltol=0.5, vntol=0.5, abstol=0.5, steps=10)
    result = simulate(
        input_capacitor(index),
        SimRequest.model_validate(
            {
                "analysis": {"kind": "tran", "duration": "1ms"},
                "stimuli": [
                    {
                        "net": "VIN",
                        "kind": "step",
                        "value": "5V",
                        "rise": "100ns",
                        "resistance": "10mohm",
                        "inductance": "1uH",
                    }
                ],
            }
        ),
        simulators[:1],
        profile=loose,
    )
    assert result.status == "unverified"
    peak = result.metric("peak", "VIN")
    assert peak is not None and peak.tightened is not None
    assert any(r.startswith("numerically unstable: ") for r in peak.reasons)


def test_a_run_that_takes_too_long_is_killed_and_says_so(
    simulators: list[Simulator],
) -> None:
    # A thousand million steps of an oscillator, given a twentieth of a second.
    text = "\n".join(
        [
            "slow",
            "V1 in 0 PULSE(0 1 0 1e-9 1e-9 1e6 2e6)",
            "L1 in out 1e-9",
            "C1 out 0 1e-9",
            ".tran 1e-6 1 0 1e-9",
            ".end",
        ]
    )
    run_ = run_netlist(simulators[0], text + "\n", ACCURATE, timeout_s=0.05)
    assert run_.plot is None and "killed after 0.05 s" in run_.error


def test_a_netlist_the_simulator_rejects_returns_its_words(
    simulators: list[Simulator],
) -> None:
    for simulator in simulators:
        bad = run_netlist(simulator, "bad\nR1 a 0 notanumber\n.op\n.end\n", ACCURATE)
        assert bad.plot is None and bad.error


PLAIN_MODEL = """* test regulator, not a maker's model: two thirds of its input
.subckt TESTTWOTHIRDS IN OUT GND
E1 mid GND IN GND 0.66
R1 mid OUT 0.01
.ends TESTTWOTHIRDS
"""


def test_a_registered_model_is_simulated_and_named_in_the_result(
    index: LibraryIndex, simulators: list[Simulator], tmp_path: Path
) -> None:
    from kairo.spice.registry import ModelRegistry

    folder = tmp_path / "models"
    folder.mkdir()
    (folder / "plain.lib").write_text(PLAIN_MODEL, encoding="utf-8")
    registry = ModelRegistry(folder)
    facts: dict[str, Any] = {
        "lib_id": "Reg:REG-3.3",
        "file": "plain.lib",
        "subckt": "TESTTWOTHIRDS",
        "nodes": ["VI", "VO", "GND"],
        "dialect": "spice3",
        "engines": ["ngspice", "xyce"],
        "fidelity": "vendor",
        "manufacturer": "Kairo test data",
        "source": "tests/integration/test_simulation.py",
        "version": "1",
        "covers": "nothing; it is a test model",
    }
    entry = registry.register(facts)
    request = SimRequest.model_validate({"analysis": {"kind": "op"}})
    result = simulate(regulator(index), request, simulators, registry=registry)
    # 0.66 * 5 V behind 10 mohm, feeding 300 mA and the LED's branch.
    current = value(result, "supplied_current", "+3V3")
    assert value(result, "voltage", "+3V3") == pytest.approx(
        3.3 - 0.01 * current, abs=1e-6
    )
    assert current == pytest.approx(0.3013, rel=1e-3)
    assert result.status == "checked"
    model = next(m for m in result.as_dict()["models"] if m["ref"] == "U1")
    assert model["fidelity"] == "vendor" and model["sha256"] == entry.sha256

    # The same file declared as a PSpice model that only ngspice runs and
    # nobody has compared with PSpice: it runs, and nothing is checked.
    registry.register(
        {
            **facts,
            "dialect": "pspice",
            "engines": ["ngspice"],
            "fidelity": "vendor-converted",
        }
    )
    again = simulate(regulator(index), request, simulators, registry=registry)
    assert again.status == "unverified"
    assert {r.simulator.engine for r in again.runs} == {"ngspice"}
    assert value(again, "voltage", "+3V3") == pytest.approx(
        value(result, "voltage", "+3V3"), abs=1e-9
    )
    because = again.as_dict()["unverified_because"]
    assert any("has not been compared with it" in r for r in because)
    if len(simulators) == 2:
        assert any("runs only in ngspice" in r for r in because)


# Written for these tests in PSpice's spelling; not a maker's model. A
# regulator held off below 1.5 V by an ideal switch, with a soft start
# whose capacitor states IC=0 and a dropout read from a TABLE written
# without '=': each is a thing Xyce reads differently from ngspice.
UVLO_MODEL = """* test regulator in PSpice's spelling, not a maker's model
.SUBCKT TESTUVLO IN OUT GND
.PARAM vset=3.3 uvlo=1.5
E_OK OK GND VALUE { IF(V(IN, GND) > {uvlo}, 1, 0) }
E_DROP DROP GND TABLE { V(IN, GND) }
+ ( (0,0.5) (5,1.0) )
R_SS OK SS 1k
C_SS SS GND 100n IC=0
E_OUT MID GND VALUE { MIN({vset}*V(SS, GND), MAX(V(IN, GND) - V(DROP, GND), 0)) }
R_OUT MID OUT 10m
R_IN IN GND 1k
.ENDS TESTUVLO
"""


def test_a_pspice_model_runs_in_both_simulators_and_they_agree(
    index: LibraryIndex, simulators: list[Simulator], tmp_path: Path
) -> None:
    from kairo.spice.engines import STEPS_BY_ITERATIONS
    from kairo.spice.registry import ModelRegistry

    if len(simulators) < 2:
        pytest.skip("Xyce not found")
    folder = tmp_path / "models"
    folder.mkdir()
    (folder / "uvlo.lib").write_text(UVLO_MODEL, encoding="utf-8")
    registry = ModelRegistry(folder)
    registry.register(
        {
            "lib_id": "Reg:REG-3.3",
            "file": "uvlo.lib",
            "subckt": "TESTUVLO",
            "nodes": ["VI", "VO", "GND"],
            "dialect": "pspice",
            "engines": ["ngspice", "xyce"],
            "fidelity": "vendor-converted",
            "manufacturer": "Kairo test data",
            "source": "tests/integration/test_simulation.py",
            "version": "1",
            "covers": "nothing; it is a test model",
        }
    )
    at_rest = simulate(
        regulator(index),
        SimRequest.model_validate({"analysis": {"kind": "op"}}),
        simulators,
        registry=registry,
    )
    assert {r.simulator.engine for r in at_rest.runs} == {"ngspice", "xyce"}
    out = at_rest.metric("voltage", "+3V3")
    assert out is not None
    # Started, as ngspice reads it: Xyce held the soft start at its IC=0
    # and gave 0 V before the copy dropped it.
    assert out.engines["xyce"] == pytest.approx(out.engines["ngspice"], abs=1e-9)
    assert out.value == pytest.approx(3.3, abs=0.01)
    assert not [r for r in out.reasons if "disagree" in r]
    # Still not a check: nobody has compared the model with PSpice.
    assert any("has not been compared with it" in r for r in out.reasons)
    model = next(m for m in at_rest.as_dict()["models"] if m["ref"] == "U1")
    assert (
        "Xyce reads a copy changed only in spelling: line 5, E_DROP" in (model["note"])
    )

    # The supply passes the switch's threshold at 30 us. Xyce's error
    # estimate stopped there; with steps by iteration count it runs on.
    rising = simulate(
        regulator(index),
        SimRequest.model_validate(
            {
                "analysis": {"kind": "tran", "duration": "1ms"},
                "stimuli": [
                    {"net": "VIN_5V", "kind": "step", "value": "5V", "rise": "100us"}
                ],
            }
        ),
        simulators,
        registry=registry,
    )
    assert rising.as_dict()["failed_runs"] == []
    final = rising.metric("final", "+3V3")
    assert final is not None and set(final.engines) == {"ngspice", "xyce"}
    assert final.engines["xyce"] == pytest.approx(final.engines["ngspice"], abs=1e-4)
    for item in rising.metrics:
        assert not [r for r in item.reasons if "disagree" in r or "gave no" in r], item
    assert any(STEPS_BY_ITERATIONS in note for note in rising.notes)


def toleranced_filter(index: LibraryIndex, r: str | None, c: str | None) -> Grounded:
    resistor: dict[str, Any] = {"lib_id": "Device:R", "value": "1k"}
    capacitor: dict[str, Any] = {
        "lib_id": "Device:C",
        "value": "100nF",
        "params": {"dielectric": "C0G"},
    }
    if r is not None:
        resistor["params"] = {"tolerance": r}
    if c is not None:
        capacitor["params"]["tolerance"] = c
    return passive(
        index,
        {"R1": resistor, "C1": capacitor},
        {
            "IN": {"role": "signal"},
            "OUT": {"role": "signal"},
            "GND": {"role": "ground"},
        },
        {"IN": ["R1.1"], "OUT": ["R1.2", "C1.1"], "GND": ["C1.2"]},
    )


def settle(limit: str) -> dict[str, Any]:
    return {
        "analysis": {"kind": "tran", "duration": "1ms"},
        "stimuli": [{"net": "IN", "kind": "step", "value": "3.3V", "rise": "100ns"}],
        "limits": [{"metric": "settling_time", "net": "OUT", "max": limit}],
    }


def test_a_limit_passes_only_at_the_worst_case_over_the_tolerances(
    index: LibraryIndex, simulators: list[Simulator]
) -> None:
    from kairo.spice.sweep import SCOPE, worst_case

    # R 1k 1%, C 100n 5%: the slowest filter is 1010 ohm * 105 nF, which
    # settles to 1% in ln(100) * 106.05 us = 488.4 us; nominal is 460.5 us.
    grounded = toleranced_filter(index, "1%", "5%")
    slowest = math.log(100) * 1010 * 105e-9
    held = worst_case(grounded, SimRequest.model_validate(settle("500us")), simulators)
    (verdict,) = held.limits
    assert verdict.verdict == "pass", verdict.detail
    assert "R1 +1%" in verdict.detail and "C1 +5%" in verdict.detail
    assert f"{slowest:.3g}"[:3] in verdict.detail
    assert held.notes[0] == SCOPE
    # The signal's amplitude has no tolerance, and settling time does not
    # depend on it, so it does not stand in the way.
    assert "the source on IN" not in verdict.detail

    # 470 us holds at nominal and not at the worst case.
    broken = worst_case(
        grounded, SimRequest.model_validate(settle("470us")), simulators
    )
    assert broken.limits[0].verdict == "fail"
    assert (
        "at the worst case (R1 +1%, C1 +5%) is above the max" in broken.limits[0].detail
    )
    assert "0.000460" in broken.limits[0].detail

    # Asked not to sweep, the same limit is only held at nominal.
    plain = simulate(grounded, SimRequest.model_validate(settle("500us")), simulators)
    assert plain.limits[0].verdict == "unverified" and plain.limits[0].held


def test_a_value_with_no_tolerance_that_matters_leaves_the_limit_unverified(
    index: LibraryIndex, simulators: list[Simulator]
) -> None:
    from kairo.spice.sweep import worst_case

    grounded = toleranced_filter(index, "1%", None)
    result = worst_case(
        grounded, SimRequest.model_validate(settle("500us")), simulators
    )
    (verdict,) = result.limits
    assert verdict.verdict == "unverified"
    assert (
        "depends on C1, which has no tolerance. Give C1's tolerance" in verdict.detail
    )
    # The peak does depend on the source, whose tolerance is not given.
    request = settle("500us")
    request["limits"] = [{"metric": "peak", "net": "OUT", "max": "3.4V"}]
    result = worst_case(
        toleranced_filter(index, "1%", "5%"),
        SimRequest.model_validate(request),
        simulators,
    )
    assert result.limits[0].verdict == "unverified"
    assert "depends on the source on IN" in result.limits[0].detail


def test_a_steady_state_limit_is_swept_and_matches_the_divider_formula(
    index: LibraryIndex, simulators: list[Simulator]
) -> None:
    from kairo.spice.sweep import worst_case

    grounded = passive(
        index,
        {
            "R1": {"lib_id": "Device:R", "value": "10k", "params": {"tolerance": "1%"}},
            "R2": {"lib_id": "Device:R", "value": "10k", "params": {"tolerance": "1%"}},
        },
        {
            "VIN": {"role": "power", "voltage": "5V", "tolerance": "2%"},
            "MID": {"role": "signal"},
            "GND": {"role": "ground"},
        },
        {"VIN": ["R1.1"], "MID": ["R1.2", "R2.1"], "GND": ["R2.2"]},
    )
    # Highest: 5.1 V * 10100 / (9900 + 10100) = 2.5755 V.
    # Lowest: 4.9 V * 9900 / (10100 + 9900) = 2.4255 V.
    request = {
        "analysis": {"kind": "op"},
        "limits": [
            {"metric": "voltage", "net": "MID", "min": "2.42V", "max": "2.58V"},
            {"metric": "voltage", "net": "MID", "max": "2.57V"},
        ],
    }
    result = worst_case(grounded, SimRequest.model_validate(request), simulators)
    inside, outside = result.limits
    assert inside.verdict == "pass", inside.detail
    assert "2.5755 V at the worst case for the max" in inside.detail
    assert "2.4255 V at the worst case for the min" in inside.detail
    assert "R1 -1%" in inside.detail and "R2 +1%" in inside.detail
    assert "the source on VIN +2%" in inside.detail
    assert outside.verdict == "fail" and "2.5755 V at the worst case" in outside.detail


def test_a_dc_sweep_shows_the_regulator_coming_out_of_dropout(
    index: LibraryIndex, simulators: list[Simulator]
) -> None:
    request = SimRequest.model_validate(
        {
            "analysis": {
                "kind": "dc",
                "sweep": "VIN_5V",
                "v_start": "0V",
                "v_stop": "6V",
            }
        }
    )
    result = simulate(regulator(index), request, simulators)
    assert result.status == "checked", result.as_dict()["unverified_because"]
    assert value(result, "start", "+3V3") == pytest.approx(0.0, abs=1e-6)
    assert value(result, "final", "+3V3") == pytest.approx(3.3, abs=1e-6)
    assert value(result, "peak", "VIN_5V") == pytest.approx(6.0)
    volts, out = result.waves["+3V3"]
    assert len(volts) == 201 and volts[0] == 0.0 and volts[-1] == pytest.approx(6.0)
    # The output is the input less the 1.3 V dropout until it reaches 3.3 V.
    for vin, vout in zip(volts, out, strict=True):
        assert vout == pytest.approx(max(0.0, min(3.3, vin - 1.3)), abs=1e-6)
    drawn = charts(result, "ldo")
    assert set(drawn) == {"sweep.svg"} and "against VIN_5V" in drawn["sweep.svg"]
    assert table(result).startswith("sweep_V,")
    with pytest.raises(Exception, match="has no source to sweep"):
        simulate(
            regulator(index),
            SimRequest.model_validate(
                {
                    "analysis": {
                        "kind": "dc",
                        "sweep": "+3V3",
                        "v_start": "0V",
                        "v_stop": "1V",
                    }
                }
            ),
            simulators,
        )


def test_the_steady_state_gives_each_two_pin_parts_dissipation(
    index: LibraryIndex, simulators: list[Simulator]
) -> None:
    request = {
        "analysis": {"kind": "op"},
        "limits": [{"metric": "power", "ref": "R1", "max": "62.5mW"}],
    }
    result = run(regulator(index), request, simulators)
    current = value(result, "current", "R1")
    # I^2 R in the 1k resistor.
    assert value(result, "power", "R1") == pytest.approx(current**2 * 1000, rel=1e-6)
    assert value(result, "power", "R1") == pytest.approx(1.69e-3, rel=2e-2)
    assert result.metric("power", "U1") is None
    assert result.limits[0].held


def test_a_run_at_another_temperature_is_asked_for_in_both_dialects() -> None:
    from kairo.spice.request import Analysis

    hot = Analysis.model_validate({"kind": "op", "temperature": "85degC"})
    assert "temp=85.0 tnom=27.0" in commands("ngspice", hot, ACCURATE)[0]
    assert any("temp=85.0 tnom=27.0" in x for x in commands("xyce", hot, ACCURATE))
    swept = Analysis.model_validate(
        {"kind": "dc", "sweep": "VIN", "v_start": "0V", "v_stop": "5V"}
    )
    assert commands("ngspice", swept, ACCURATE, "Vsrc_vin")[-1] == (
        ".dc Vsrc_vin 0.0 5.0 0.025"
    )
    with pytest.raises(ValueError, match="needs the source element"):
        commands("ngspice", swept, ACCURATE)
    for bad, message in (
        (
            {"kind": "dc", "sweep": "VIN", "v_start": "5V", "v_stop": "0V"},
            "below v_stop",
        ),
        ({"kind": "dc", "sweep": "VIN"}, "needs sweep, v_start and v_stop"),
        ({"kind": "op", "v_start": "0V"}, "belong to a dc analysis"),
    ):
        with pytest.raises(ValueError, match=message):
            Analysis.model_validate(bad)


def test_a_worst_case_between_the_ends_is_found_by_search(
    index: LibraryIndex, simulators: list[Simulator]
) -> None:
    # The plan's accuracy circuit 9. Power into a load is greatest when the
    # load equals the source resistance, and R2's tolerance (88 to 132
    # ohms) spans R1 (99 to 101 ohms), so the greatest power is inside the
    # range and no corner has it:
    #   P = V^2 R2 / (R1 + R2)^2
    #   nominal            10^2   * 110 / 210^2 = 0.249433 W
    #   R2 at its low end  10.1^2 *  88 / 187^2 = 0.256710 W
    #   R2 at its high end 10.1^2 * 132 / 231^2 = 0.252343 W
    #   the true greatest  10.1^2 *  99 / 198^2 = 0.257576 W
    from kairo.spice.sweep import worst_case

    grounded = passive(
        index,
        {
            "R1": {"lib_id": "Device:R", "value": "100", "params": {"tolerance": "1%"}},
            "R2": {
                "lib_id": "Device:R",
                "value": "110",
                "params": {"tolerance": "20%"},
            },
        },
        {
            "VIN": {"role": "power", "voltage": "10V", "tolerance": "1%"},
            "MID": {"role": "signal"},
            "GND": {"role": "ground"},
        },
        {"VIN": ["R1.1"], "MID": ["R1.2", "R2.1"], "GND": ["R2.2"]},
    )
    request = {
        "analysis": {"kind": "op"},
        "limits": [
            # Above both ends, below the peak: only a search can break it.
            {"metric": "power", "ref": "R2", "max": "0.2572W"},
            # Above the peak: nothing breaks it, and a search proves nothing.
            {"metric": "power", "ref": "R2", "max": "0.26W"},
        ],
    }
    result = worst_case(grounded, SimRequest.model_validate(request), simulators)
    broken, unproven = result.limits
    assert broken.verdict == "fail", broken.detail
    assert (
        "found by search" in broken.detail and "between the ends of R2" in broken.detail
    )
    found = float(broken.detail.split(" W ")[0])
    assert found == pytest.approx(10.1**2 * 99 / 198**2, rel=2e-4)
    assert unproven.verdict == "unverified", unproven.detail
    assert "a search does not prove" in unproven.detail
    worst = float(unproven.detail.split("found at worst ")[1].split(" W")[0])
    assert worst == pytest.approx(10.1**2 * 99 / 198**2, rel=2e-4)


def divider(index: LibraryIndex, r2: dict[str, Any]) -> Grounded:
    return passive(
        index,
        {
            "R1": {
                "lib_id": "Device:R",
                "value": "10k",
                "params": {"tolerance": "1%", "tempco": "100ppm/degC"},
            },
            "R2": {"lib_id": "Device:R", "value": "10k", "params": r2},
        },
        {
            "VIN": {"role": "power", "voltage": "5V", "tolerance": "2%"},
            "MID": {"role": "signal"},
            "GND": {"role": "ground"},
        },
        {"VIN": ["R1.1"], "MID": ["R1.2", "R2.1"], "GND": ["R2.2"]},
    )


def test_a_linear_circuit_gets_an_exact_third_value(
    index: LibraryIndex, simulators: list[Simulator], monkeypatch: pytest.MonkeyPatch
) -> None:
    from kairo.spice.simulate import EXACT

    result = run(
        divider(index, {"tolerance": "1%"}), {"analysis": {"kind": "op"}}, simulators
    )
    mid = result.metric("voltage", "MID")
    assert mid is not None and mid.engines[EXACT] == 2.5
    assert mid.status == "checked"
    supplied = result.metric("supplied_current", "VIN")
    assert supplied is not None and supplied.engines[EXACT] == pytest.approx(2.5e-4)
    assert any(EXACT in note for note in result.notes)

    swept = run(
        rc_filter(index),
        {
            "analysis": {"kind": "ac", "f_start": "10Hz", "f_stop": "1MHz"},
            "stimuli": [{"net": "IN", "kind": "ac"}],
        },
        simulators,
    )
    cutoff = swept.metric("cutoff", "OUT")
    assert cutoff is not None
    assert cutoff.engines[EXACT] == pytest.approx(cutoff.value, rel=1e-6)

    # The regulator's behavioural model is not linear: no third value.
    nonlinear = run(regulator(index), {"analysis": {"kind": "op"}}, simulators)
    rail = nonlinear.metric("voltage", "+3V3")
    assert rail is not None and EXACT not in rail.engines

    # Where the exact answer and the simulators part, the number is not
    # checked: here the exact solution is made 1% off on purpose.
    from kairo.spice import nodal
    from kairo.spice.raw import Plot

    real = nodal.steady_state

    def off(circuit: Any) -> Plot:
        plot = real(circuit)
        values = {k: tuple(v * 1.01 for v in vs) for k, vs in plot.values.items()}
        return replace(plot, values=values)

    monkeypatch.setattr(nodal, "steady_state", off)
    parted = run(
        divider(index, {"tolerance": "1%"}), {"analysis": {"kind": "op"}}, simulators
    )
    mid = parted.metric("voltage", "MID")
    assert mid is not None and mid.status == "unverified"
    assert "exact nodal analysis of the same circuit gives 2.525 V" in mid.reasons


def test_a_limit_is_judged_at_each_temperature_corner(
    index: LibraryIndex, simulators: list[Simulator]
) -> None:
    # At -40 degC, 67 degrees from nominal, 100 ppm/degC widens each
    # resistor's 1% to 1.67%, so the divider's highest output is
    #   5.1 V * 10167 / (9833 + 10167) = 2.592585 V (shown as 2.59259)
    # against 2.5755 V at 27 degC and 5.1 * 10158 / 20000 = 2.59029 V at 85.
    from kairo.spice.sweep import worst_case

    grounded = divider(index, {"tolerance": "1%", "tempco": "±100 ppm/°C"})
    request = {
        "analysis": {"kind": "op", "temperatures": ["-40degC", "85degC"]},
        "limits": [
            {"metric": "voltage", "net": "MID", "max": "2.58V"},
            {"metric": "voltage", "net": "MID", "max": "2.60V"},
        ],
    }
    result = worst_case(grounded, SimRequest.model_validate(request), simulators)
    cold_breaks, holds = result.limits
    # It holds at 27 degC and breaks at both corners; what decided it leads.
    assert cold_breaks.verdict == "fail", cold_breaks.detail
    assert cold_breaks.detail.startswith("at -40 degC: fail, 2.59259 V")
    assert "at 27 degC: pass, 2.5755 V" in cold_breaks.detail
    assert "R1 -1.67%" in cold_breaks.detail and "R2 +1.67%" in cold_breaks.detail
    assert holds.verdict == "pass", holds.detail
    assert "at 85 degC: pass, 2.59029 V" in holds.detail
    assert any("judged at 27 degC and at -40 degC, 85 degC" in n for n in result.notes)
    # The numbers listed are still those at 27 degC, nominal.
    assert value(result, "voltage", "MID") == pytest.approx(2.5, rel=1e-6)


def test_a_part_that_states_nothing_of_temperature_leaves_the_corner_unverified(
    index: LibraryIndex, simulators: list[Simulator]
) -> None:
    from kairo.spice.sweep import worst_case

    grounded = divider(index, {"tolerance": "1%"})
    request = {
        "analysis": {"kind": "op", "temperatures": ["85degC"]},
        "limits": [{"metric": "voltage", "net": "MID", "max": "2.60V"}],
    }
    result = worst_case(grounded, SimRequest.model_validate(request), simulators)
    (limit,) = result.limits
    assert limit.verdict == "unverified", limit.detail
    assert "R2, which has no stated change with temperature" in limit.detail
    assert "Give R2's temperature coefficient" in limit.detail
    assert "at 27 degC: pass" in limit.detail

    for analysis, sweep, says in (
        ({"kind": "op", "temperatures": []}, True, "at least one corner"),
        (
            {"kind": "op", "temperature": "50degC", "temperatures": ["85degC"]},
            True,
            "not both",
        ),
        ({"kind": "op", "temperatures": ["85degC"]}, False, "leave sweep on"),
    ):
        with pytest.raises(ValueError, match=says):
            SimRequest.model_validate({"analysis": analysis, "sweep": sweep})


def test_a_sampled_spread_repeats_exactly_and_stays_inside_the_extremes(
    index: LibraryIndex, simulators: list[Simulator]
) -> None:
    # The plan's accuracy circuit 8. The divider's output over its whole
    # tolerance box runs from 2.4255 V to 2.5755 V (see the sweep test),
    # so no sample may lie outside that.
    from kairo.spice.circuit import CircuitError
    from kairo.spice.montecarlo import monte_carlo

    grounded = divider(index, {"tolerance": "1%"})

    def request(seed: int, **distributions: Any) -> SimRequest:
        return SimRequest.model_validate(
            {
                "analysis": {"kind": "op"},
                "monte_carlo": {
                    "samples": 120,
                    "seed": seed,
                    "distributions": distributions,
                },
            }
        )

    uniform = {"shape": "uniform"}
    everything = {"R1": uniform, "R2": uniform, "VIN": uniform}
    first = monte_carlo(grounded, request(7, **everything), simulators)
    again = monte_carlo(grounded, request(7, **everything), simulators)
    other = monte_carlo(grounded, request(8, **everything), simulators)
    assert first == again
    assert first != other
    spread = next(s for s in first.spreads if (s.metric, s.where) == ("voltage", "MID"))
    assert spread.count == 120 and first.failed == 0
    assert 2.4255 <= spread.lowest < spread.mean < spread.highest <= 2.5755
    assert spread.mean == pytest.approx(2.5, abs=0.01)
    assert first.held == ()
    assert first.sampled[0] == "R1: uniform within 1%"
    report = first.as_dict()
    assert next(iter(report)) == "decides" and "nothing" in report["decides"]

    # Only what has a stated distribution is sampled; the rest is named.
    partly = monte_carlo(
        grounded, request(7, R1={"shape": "normal", "sigmas": 3}), simulators
    )
    assert partly.held == ("R2", "the source on VIN")
    assert partly.sampled == (
        "R1: normal, the tolerance being 3 standard deviations within 1%",
    )
    with pytest.raises(CircuitError, match="'R9', which is not a value"):
        monte_carlo(grounded, request(7, R9=uniform), simulators)
    for wrong, says in (
        ({"samples": 10, "seed": 1, "distributions": {}}, "give distributions"),
        (
            {"samples": 10, "seed": 1, "distributions": {"R1": {"shape": "normal"}}},
            "needs sigmas",
        ),
        ({"samples": 1, "seed": 1, "distributions": {"R1": uniform}}, "samples"),
    ):
        with pytest.raises(ValueError, match=says):
            SimRequest.model_validate(
                {"analysis": {"kind": "op"}, "monte_carlo": wrong}
            )


def test_a_sampled_spread_never_changes_a_verdict(
    index: LibraryIndex, simulators: list[Simulator]
) -> None:
    from kairo.spice.sweep import worst_case

    grounded = divider(index, {"tolerance": "1%"})
    limits = [{"metric": "voltage", "net": "MID", "max": "2.57V"}]
    plain = worst_case(
        grounded,
        SimRequest.model_validate({"analysis": {"kind": "op"}, "limits": limits}),
        simulators,
    )
    sampled = worst_case(
        grounded,
        SimRequest.model_validate(
            {
                "analysis": {"kind": "op"},
                "limits": limits,
                "monte_carlo": {
                    "samples": 5,
                    "seed": 1,
                    "distributions": {"R1": {"shape": "uniform"}},
                },
            }
        ),
        simulators,
    )
    # 2.5755 V at the worst case breaks 2.57 V, though no sample of five would.
    assert plain.limits[0].verdict == sampled.limits[0].verdict == "fail"
    assert plain.limits[0].detail == sampled.limits[0].detail


# An amplifier of gain -1000 from its input pin to its output, behind
# 1 kilohm with 1 uF to ground: one pole inside the part.
LOOP_MODEL = """* test amplifier, not a maker's model
.subckt TESTAMP IN OUT GND
E1 e GND GND IN 1000
R1 e OUT 1000
C1 OUT GND 1e-6
.ends TESTAMP
"""


def loop_design(index: LibraryIndex) -> Grounded:
    # The amplifier's output goes back to its input through 10 kilohms,
    # with 10 nF to ground at the input: a second pole outside the part.
    return passive(
        index,
        {
            "U1": {"lib_id": "Reg:REG-3.3"},
            "R2": {"lib_id": "Device:R", "value": "10k"},
            "C2": {"lib_id": "Device:C", "value": "10n"},
        },
        {
            "OUT": {"role": "signal"},
            "FB": {"role": "signal"},
            "GND": {"role": "ground"},
        },
        {
            "OUT": ["U1.VO", "R2.1"],
            "FB": ["R2.2", "C2.1", "U1.VI"],
            "GND": ["U1.GND", "C2.2"],
        },
    )


def loop_exact(frequency: float) -> complex:
    # The two RC sections load each other, so the gain round the loop is
    #   T = A0 / (1 + s (R1 C1 + R1 C2 + R2 C2) + s^2 R1 R2 C1 C2)
    s = 2j * math.pi * frequency
    return 1000 / (1 + s * (1e-3 + 1e-5 + 1e-4) + s * s * 1e-7)


def test_a_loops_gain_and_margins_match_a_loop_known_exactly(
    index: LibraryIndex, simulators: list[Simulator], tmp_path: Path
) -> None:
    # The plan's accuracy circuit 10: a two-pole loop. Its crossover and
    # phase margin follow from the formula above, solved here by bisection.
    import cmath

    from kairo.spice.loop import measure_loop
    from kairo.spice.registry import ModelRegistry

    low, high = 1e3, 1e5
    for _ in range(200):
        middle = math.sqrt(low * high)
        low, high = (middle, high) if abs(loop_exact(middle)) > 1 else (low, middle)
    crossover = math.sqrt(low * high)
    margin = 180 + math.degrees(cmath.phase(loop_exact(crossover)))
    # About 6 degrees: a loop this close to ringing shows any error in
    # the method plainly.
    assert 5 < margin < 8

    folder = tmp_path / "models"
    folder.mkdir()
    (folder / "amp.lib").write_text(LOOP_MODEL, encoding="utf-8")
    registry = ModelRegistry(folder)
    facts: dict[str, Any] = {
        "lib_id": "Reg:REG-3.3",
        "file": "amp.lib",
        "subckt": "TESTAMP",
        "nodes": ["VI", "VO", "GND"],
        "dialect": "spice3",
        "engines": ["ngspice", "xyce"],
        "fidelity": "vendor",
        "manufacturer": "Kairo test data",
        "source": "tests/integration/test_simulation.py",
        "version": "1",
        "covers": "nothing; it is a test model",
    }
    registry.register(facts)
    grounded = loop_design(index)

    def ask(**loop: Any) -> SimRequest:
        return SimRequest.model_validate(
            {
                "analysis": {"kind": "ac", "f_start": "10Hz", "f_stop": "1MHz"},
                "loop": loop,
            }
        )

    # The input pin is a supply input in this test library, which does not
    # say which way a signal goes, so the request must.
    from kairo.spice.circuit import CircuitError

    with pytest.raises(CircuitError, match=r"give loop\.into"):
        measure_loop(grounded, ask(pin="U1.VI"), simulators, registry=registry)
    with pytest.raises(CircuitError, match="not one connected pin"):
        measure_loop(grounded, ask(pin="U9.VI"), simulators, registry=registry)

    result = measure_loop(
        grounded, ask(pin="U1.VI", into="pin"), simulators, registry=registry
    )
    if len(simulators) == 2:
        assert result.status == "checked", result.reasons
    else:
        # CI has ngspice alone: one engine's loop is measured, not checked.
        assert result.status == "unverified"
        assert "no second simulator" in " ".join(result.reasons)
    assert result.margins.crossover_hz == pytest.approx(crossover, rel=1e-3)
    assert result.margins.phase_margin_deg == pytest.approx(margin, abs=0.05)
    assert result.low_frequency_gain_db == pytest.approx(
        20 * math.log10(abs(loop_exact(10.0))), abs=1e-3
    )
    # Two poles never turn the phase a full half turn, so there is no
    # gain margin to give, and none is made up.
    assert result.margins.gain_margin_db is None
    assert set(result.engines) == {s.engine for s in simulators}
    # Measured at the amplifier's output instead, where the signal flows
    # out of the part, the same loop gives the same margins.
    at_output = measure_loop(grounded, ask(pin="U1.VO"), simulators, registry=registry)
    assert at_output.downstream == "net"
    assert at_output.margins.phase_margin_deg == pytest.approx(margin, abs=0.05)
    # Told the wrong way, the measurement is of something else.
    wrong = measure_loop(
        grounded, ask(pin="U1.VI", into="net"), simulators, registry=registry
    )
    assert wrong.margins.phase_margin_deg != pytest.approx(margin, abs=1.0)

    # A model that is not a maker's does not represent a loop.
    registry.register(
        {
            **facts,
            "fidelity": "vendor-converted",
            "dialect": "pspice",
            "engines": ["ngspice"],
        }
    )
    lesser = measure_loop(
        grounded, ask(pin="U1.VI", into="pin"), simulators, registry=registry
    )
    assert lesser.status == "unverified"
    assert any("does not represent the part's loop" in r for r in lesser.reasons)

    for wrong_request, says in (
        ({"analysis": {"kind": "op"}, "loop": {"pin": "U1.VI"}}, "is an ac analysis"),
        (
            {
                "analysis": {"kind": "ac", "f_start": "10Hz", "f_stop": "1MHz"},
                "loop": {"pin": "U1.VI"},
                "stimuli": [{"net": "FB", "kind": "ac", "value": "1V"}],
            },
            "give no ac stimulus",
        ),
    ):
        with pytest.raises(ValueError, match=says):
            SimRequest.model_validate(wrong_request)


def test_calculation_and_simulation_are_compared_at_the_worst_corner(
    index: LibraryIndex, simulators: list[Simulator]
) -> None:
    # An LED and its resistor on a 5 V, 2% supply. The calculation's
    # greatest current takes the supply high, the resistor low and the
    # forward voltage low: (5.1 - 1.8) / 990 = 3.3333 mA. The simulation
    # is sent to the corner its own runs find worst, which must be the same.
    from kairo.spice.sweep import worst_case

    grounded = passive(
        index,
        {
            "R1": {"lib_id": "Device:R", "value": "1k", "params": {"tolerance": "1%"}},
            "D1": {
                "lib_id": "Device:LED",
                "params": {"v_f": "2.0V", "v_f_tolerance": "10%", "i_f": "20mA"},
            },
        },
        {
            "VIN": {"role": "power", "voltage": "5V", "tolerance": "2%"},
            "A": {"role": "signal"},
            "GND": {"role": "ground"},
        },
        {"VIN": ["R1.1"], "A": ["R1.2", "D1.A"], "GND": ["D1.K"]},
    )
    result = worst_case(
        grounded, SimRequest.model_validate({"analysis": {"kind": "op"}}), simulators
    )
    nominal, corner = result.compared
    assert nominal.what == "current supplied to VIN"
    assert nominal.calculated == pytest.approx(3.0e-3)
    assert corner.what == (
        "greatest current supplied to VIN, at the worst corner "
        "(R1 -1%, D1's forward voltage -10%, the source on VIN +2%)"
    )
    assert corner.calculated == pytest.approx((5.1 - 1.8) / 990)
    # The diode stand-in's own few millivolts are all that part them.
    assert corner.simulated == pytest.approx(corner.calculated, rel=5e-3)
    assert corner.agree
    # With no limits asked, nothing else of the result changes.
    assert result.limits == () and result.status in ("checked", "unverified")

    # A value the current depends on with no tolerance: no corner to
    # compare at, and none is made up.
    bare = passive(
        index,
        {
            "R1": {"lib_id": "Device:R", "value": "1k"},
            "D1": {
                "lib_id": "Device:LED",
                "params": {"v_f": "2.0V", "v_f_tolerance": "10%", "i_f": "20mA"},
            },
        },
        {
            "VIN": {"role": "power", "voltage": "5V", "tolerance": "2%"},
            "A": {"role": "signal"},
            "GND": {"role": "ground"},
        },
        {"VIN": ["R1.1"], "A": ["R1.2", "D1.A"], "GND": ["D1.K"]},
    )
    result = worst_case(
        bare, SimRequest.model_validate({"analysis": {"kind": "op"}}), simulators
    )
    assert [c.what for c in result.compared] == ["current supplied to VIN"]


def test_a_simulated_limit_can_rest_on_kairos_default_margins_and_says_so(
    index: LibraryIndex, simulators: list[Simulator]
) -> None:
    # A divider that states no tolerance at all. With Kairo's defaults (5%
    # for each resistor, 10% for the supply) its highest output is
    #   5.5 V * 10500 / (9500 + 10500) = 2.8875 V.
    from kairo.spice.sweep import worst_case

    def unstated(margins: str) -> Grounded:
        return passive(
            index,
            {
                "R1": {"lib_id": "Device:R", "value": "10k"},
                "R2": {"lib_id": "Device:R", "value": "10k"},
            },
            {
                "VIN": {"role": "power", "voltage": "5V"},
                "MID": {"role": "signal"},
                "GND": {"role": "ground"},
            },
            {"VIN": ["R1.1"], "MID": ["R1.2", "R2.1"], "GND": ["R2.2"]},
            margins=margins,
        )

    request = SimRequest.model_validate(
        {
            "analysis": {"kind": "op"},
            "limits": [
                {"metric": "voltage", "net": "MID", "max": "2.9V"},
                {"metric": "voltage", "net": "MID", "max": "2.8V"},
            ],
        }
    )
    result = worst_case(unstated("default"), request, simulators)
    holds, breaks = result.limits
    assert holds.verdict == "pass" and "2.8875 V at the worst case" in holds.detail
    assert breaks.verdict == "fail"
    assert result.on_defaults == (
        "R1's tolerance: 5%",
        "R2's tolerance: 5%",
        "VIN's tolerance: 10%",
    )
    assert any("Kairo's own default margins" in note for note in result.notes)
    shown = result.as_dict()
    assert shown["limits_basis"] == "default margins"
    assert shown["on_default_margins"] == list(result.on_defaults)

    # Turned off, the same limits are not judged at all.
    strict = worst_case(unstated("stated"), request, simulators)
    assert [j.verdict for j in strict.limits] == ["unverified", "unverified"]
    assert strict.on_defaults == () and strict.as_dict()["limits_basis"] == (
        "stated values"
    )
