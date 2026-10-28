"""A design as a simulator's circuit: what stands in for each part, and the text."""

# These tests change plain JSON data, which a strict type checker cannot follow.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from minilib import ldo, make_index
from pydantic import ValidationError

from kairo.ir import Design
from kairo.library import LibraryIndex
from kairo.library.grounding import Grounded, ground
from kairo.parts import PartStore
from kairo.spice.circuit import (
    DROP_MODEL_LINE,
    Circuit,
    CircuitError,
    build_circuit,
    connections,
    netlist,
    scaled,
)
from kairo.spice.engines import ACCURATE, commands, find_simulator, tighter
from kairo.spice.request import SimRequest

OP: dict[str, Any] = {"analysis": {"kind": "op"}}
TRAN: dict[str, Any] = {
    "analysis": {"kind": "tran", "duration": "5ms"},
    "stimuli": [{"net": "VIN_5V", "kind": "step", "value": "5V", "rise": "100us"}],
}


@pytest.fixture
def index(tmp_path: Path) -> LibraryIndex:
    return make_index(tmp_path)


def grounded_of(index: LibraryIndex, change: Any = None) -> Grounded:
    data = ldo()
    if change is not None:
        change(data)
    return ground(Design.model_validate(data), index)


def circuit_of(
    index: LibraryIndex, request: dict[str, Any], change: Any = None
) -> Circuit:
    return build_circuit(grounded_of(index, change), SimRequest.model_validate(request))


def design_graph(grounded: Grounded) -> dict[str, set[tuple[str, str]]]:
    graph: dict[str, set[tuple[str, str]]] = {}
    for pin in grounded.pins:
        graph.setdefault(pin.net, set()).add((pin.ref, pin.pin.number))
    return graph


@pytest.mark.parametrize("engine", ["ngspice", "xyce"])
@pytest.mark.parametrize("request_data", [OP, TRAN], ids=["op", "tran"])
def test_each_dialect_reads_back_to_the_designs_connections(
    index: LibraryIndex, engine: Any, request_data: dict[str, Any]
) -> None:
    grounded = grounded_of(index)
    request = SimRequest.model_validate(request_data)
    circuit = build_circuit(grounded, request)
    text = netlist(circuit, engine, commands(engine, request.analysis, ACCURATE))
    assert connections(text) == design_graph(grounded)
    assert text.endswith(".end\n") and DROP_MODEL_LINE in text


def test_a_pin_written_onto_the_wrong_node_is_caught(index: LibraryIndex) -> None:
    grounded = grounded_of(index)
    request = SimRequest.model_validate(OP)
    text = netlist(build_circuit(grounded, request), "ngspice", [".op"])
    # R1's first pin moved from the 3.3 V net to the LED's net.
    line = next(x for x in text.splitlines() if x.startswith("V_r1_1 "))
    moved = line.replace("n__2b_3v3", "n_led_5f_a")
    assert moved != line
    assert connections(text.replace(line, moved)) != design_graph(grounded)
    with pytest.raises(CircuitError, match="no source for V_r1_1"):
        connections(text.replace(line + "\n", ""))
    with pytest.raises(CircuitError, match="not a net"):
        connections(text.replace(line, line.replace("n__2b_3v3", "elsewhere")))


def test_the_two_dialects_differ_only_where_the_simulators_do(
    index: LibraryIndex,
) -> None:
    circuit = circuit_of(index, OP)
    ours = netlist(circuit, "ngspice", [])
    theirs = netlist(circuit, "xyce", [])
    different = [
        (a, b)
        for a, b in zip(ours.splitlines(), theirs.splitlines(), strict=True)
        if a != b
    ]
    # Only the regulator's behavioural source: Xyce wants braces.
    assert len(different) == 1
    assert (
        different[0][0].startswith("B_u1 ")
        and " V = max(0, min(3.3, " in different[0][0]
    )
    assert " V={max(0, min(3.3, " in different[0][1]


def test_what_stands_in_for_each_part_is_recorded(index: LibraryIndex) -> None:
    circuit = circuit_of(index, OP)
    notes = {n.ref: n for n in circuit.models}
    assert notes["R1"].fidelity == "primitive" and "1000 ohm" in notes["R1"].note
    assert notes["U1"].fidelity == "behavioural"
    assert notes["U1"].source == "kairo:ldo_macro"
    assert "quiescent current is not given" in notes["U1"].note
    assert notes["D1"].fidelity == "behavioural" and "2 V" in notes["D1"].note
    assert "no DC-bias data" in notes["C2"].note and "no ESR" in notes["C2"].note
    assert notes["source VIN_5V"].note.startswith("an ideal 5 V source")
    assert "constant 0.3 A" in notes["load 1 on +3V3"].note
    # The small library's connector is not one of KiCad's connector
    # libraries, so Kairo does not assume it is empty inside.
    assert [u.ref for u in circuit.unmodelled] == ["J1"]
    assert "no simulation model for J1 (Device:Conn)" in circuit.unmodelled[0].why
    assert circuit.regulated == {"+3V3": "V_u1_2"}
    assert circuit.pin_source("R1", "2") == "V_r1_2"
    with pytest.raises(KeyError):
        circuit.pin_source("R1", "9")


def test_a_run_in_time_changes_the_load_and_rounds_the_regulator(
    index: LibraryIndex,
) -> None:
    circuit = circuit_of(index, TRAN)
    names = {e.name: e for e in circuit.elements}
    # 3.3 V / 0.3 A.
    assert float(names["Rload_1"].value) == pytest.approx(11.0)
    assert "Iload_1" not in names
    assert "sqrt(" in (names["B_u1"].expression or "")
    assert names["Vsrc_1"].value.startswith("PULSE(0.0 5.0 0.0 0.0001 0.0001 ")
    assert circuit.sources == {"VIN_5V": "Vsrc_1"}
    note = next(n for n in circuit.models if n.ref == "load 1 on +3V3")
    assert "11 ohm" in note.note and "below nominal" in note.note


def test_a_sine_source_is_written_and_scaled_whole(index: LibraryIndex) -> None:

    request = json.loads(json.dumps(TRAN))
    request["stimuli"][0] = {
        "net": "VIN_5V",
        "kind": "sine",
        "value": "5V",
        "amplitude": "50mV",
        "frequency": "100kHz",
    }
    circuit = circuit_of(index, request)
    names = {e.name: e for e in circuit.elements}
    assert names["Vsrc_1"].value == "SIN(5.0 0.05 100000.0)"
    # The worst case moves a source's level and its ripple together.
    moved = {e.name: e for e in scaled(circuit, {"Vsrc_1": 1.1}).elements}
    level, amplitude, frequency = moved["Vsrc_1"].value[4:-1].split()
    assert (float(level), float(amplitude), float(frequency)) == pytest.approx(
        (5.5, 0.055, 1e5)
    )
    # Two hundred steps a period at least, however long the run.
    parsed = SimRequest.model_validate(request)
    line = commands("ngspice", parsed.analysis, ACCURATE, None, parsed.stimuli)[-1]
    assert float(line.split()[-1]) == pytest.approx(1 / (100e3 * 200))


def test_a_source_carries_its_stated_resistance_and_inductance(
    index: LibraryIndex,
) -> None:
    request = json.loads(json.dumps(TRAN))
    request["stimuli"][0].update(resistance="50mohm", inductance="1uH")
    circuit = circuit_of(index, request)
    names = {e.name: e for e in circuit.elements}
    assert float(names["Rsrc_1"].value) == pytest.approx(0.05)
    assert float(names["Lsrc_1"].value) == pytest.approx(1e-6)
    # Source, resistor, inductor and link form one chain to the net.
    assert names["Vsrc_1"].nodes[0] == names["Rsrc_1"].nodes[0]
    assert names["Rsrc_1"].nodes[1] == names["Lsrc_1"].nodes[0]
    assert names["Lsrc_1"].nodes[1] == names["Vlink_1"].nodes[0]
    assert names["Vlink_1"].nodes[1] == circuit.nodes["VIN_5V"]
    note = next(n for n in circuit.models if n.ref == "source VIN_5V")
    assert "behind 0.05 ohm and 1e-06 H" in note.note


def test_missing_facts_leave_a_part_open_and_say_what_to_give(
    index: LibraryIndex,
) -> None:
    def strip(data: dict[str, Any]) -> None:
        del data["components"]["D1"]["params"]["v_f"]
        del data["components"]["U1"]["params"]["v_dropout"]
        data["components"]["C1"]["value"] = "big"

    circuit = circuit_of(index, OP, strip)
    by_ref = {u.ref: u for u in circuit.unmodelled}
    assert set(by_ref) == {"J1", "D1", "U1", "C1"}
    assert "forward voltage (v_f)" in by_ref["D1"].resolve
    assert "dropout voltage (v_dropout)" in by_ref["U1"].resolve
    assert "'big' is not a value in F" in by_ref["C1"].why
    # An open regulator makes no rail, and its pins are still in the text.
    assert circuit.regulated == {}
    # Nothing is put in its place, and the result says the rail is dead.
    assert by_ref["U1"].why.endswith("so nothing drives +3V3")
    assert not any(e.name.startswith("Vsrc__2b_3v3") for e in circuit.elements)
    assert any(e.name == "V_u1_2" for e in circuit.elements)
    assert not any(e.name.startswith(("B_", "F_", "D_")) for e in circuit.elements)
    assert not circuit.uses_drop_model
    # With U1 and D1 open, nothing sets +3V3 or the LED's anode, which R1
    # joins into one piece. It is held to ground once, so every simulator
    # can solve the same circuit, and the hold makes every number of the
    # run unverified.
    held = [e for e in circuit.elements if e.name.startswith("Rhold_")]
    assert [(e.nodes[1], float(e.value)) for e in held] == [("0", 1e12)]
    notes = {n.ref: n for n in circuit.models}
    assert notes["net +3V3, LED_A"].unproven.startswith(
        "nothing in the circuit sets the voltage of +3V3, LED_A: D1, U1 on them "
        "have no model and are left open"
    )


def test_a_stated_esr_quiescent_current_and_bias_curve_are_used(
    index: LibraryIndex, tmp_path: Path
) -> None:
    parts = tmp_path / "parts"
    parts.mkdir()
    # Made up for this test; not any manufacturer's data.
    (parts / "TESTCAP22.json").write_text(
        json.dumps(
            {
                "mpn": "TESTCAP22",
                "manufacturer": "Test data",
                "dc_bias": {
                    "points": [[0, 1.0], [5, 0.5]],
                    "conditions": "made up for a test",
                    "source": {
                        "document": "test curve, not a manufacturer's",
                        "revision": "none",
                        "page": "none",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    def add(data: dict[str, Any]) -> None:
        data["components"]["C2"]["params"] = {"mpn": "TESTCAP22", "esr": "10mohm"}
        data["components"]["C1"]["params"] = {"dielectric": "C0G"}
        data["components"]["U1"]["params"]["i_q"] = "5mA"

    circuit = build_circuit(
        grounded_of(index, add),
        SimRequest.model_validate(OP),
        store=PartStore(parts),
    )
    names = {e.name: e for e in circuit.elements}
    notes = {n.ref: n for n in circuit.models}
    # 22 uF at 3.3 V on a curve that falls from 100% to 50% over 5 V.
    assert float(names["C_c2"].value) == pytest.approx(22e-6 * (1 - 0.5 * 3.3 / 5))
    assert notes["C2"].fidelity == "bias-adjusted" and "typical" in notes["C2"].note
    assert float(names["Resr_c2"].value) == pytest.approx(0.01)
    assert names["Resr_c2"].nodes[1] == names["C_c2"].nodes[0]
    assert "does not lose capacitance" in notes["C1"].note
    assert names["Iq_u1"].value == "DC 0.005"
    assert "quiescent current of 0.005 A" in notes["U1"].note

    # A curve that stops below the operating voltage says nothing there:
    # the capacitor stays at nominal and the note says why.
    short = json.loads((parts / "TESTCAP22.json").read_text(encoding="utf-8"))
    short["dc_bias"]["points"] = [[0, 1.0], [2, 0.8]]
    (parts / "TESTCAP22.json").write_text(json.dumps(short), encoding="utf-8")
    circuit = build_circuit(
        grounded_of(index, add),
        SimRequest.model_validate(OP),
        store=PartStore(parts),
    )
    names = {e.name: e for e in circuit.elements}
    notes = {n.ref: n for n in circuit.models}
    assert float(names["C_c2"].value) == pytest.approx(22e-6)
    assert notes["C2"].fidelity == "primitive"
    assert "TESTCAP22's data cannot be used" in notes["C2"].note


def test_a_request_that_does_not_fit_is_refused(index: LibraryIndex) -> None:
    with pytest.raises(CircuitError, match="'NOWHERE' is not in the design"):
        circuit_of(
            index,
            {
                "analysis": {"kind": "op"},
                "stimuli": [{"net": "NOWHERE", "value": "1V"}],
            },
        )
    with pytest.raises(CircuitError, match="ground net"):
        circuit_of(
            index,
            {"analysis": {"kind": "op"}, "stimuli": [{"net": "GND", "value": "1V"}]},
        )
    # The regulator already sets this net; a second source would fight it.
    with pytest.raises(CircuitError, match=r"\+3V3 is made by U1"):
        circuit_of(
            index,
            {"analysis": {"kind": "op"}, "stimuli": [{"net": "+3V3", "value": "3V"}]},
        )


@pytest.mark.parametrize(
    ("request_data", "message"),
    [
        ({"analysis": {"kind": "tran"}}, "needs its duration"),
        ({"analysis": {"kind": "op", "duration": "1ms"}}, "belongs to a tran"),
        (
            {"analysis": {"kind": "ac", "f_start": "1kHz", "f_stop": "10Hz"}},
            "below f_stop",
        ),
        (
            {"analysis": {"kind": "ac", "f_start": "1Hz", "f_stop": "10Hz"}},
            "needs one stimulus of kind ac",
        ),
        (
            {
                "analysis": {"kind": "tran", "duration": "1ms"},
                "stimuli": [{"net": "A", "kind": "step", "value": "5V"}],
            },
            "needs its rise time",
        ),
        (
            {
                "analysis": {"kind": "op"},
                "stimuli": [{"net": "A", "kind": "step", "value": "5V", "rise": "1us"}],
            },
            "belongs to a tran",
        ),
        (
            {
                "analysis": {"kind": "op"},
                "stimuli": [{"net": "A", "value": "5V"}, {"net": "A", "value": "3V"}],
            },
            "one stimulus",
        ),
        (
            {"analysis": {"kind": "op"}, "stimuli": [{"net": "A", "value": "5"}]},
            "expected V",
        ),
        (
            {
                "analysis": {"kind": "op"},
                "stimuli": [{"net": "A", "value": "5V", "resistance": "0ohm"}],
            },
            "above zero",
        ),
        (
            {"analysis": {"kind": "op"}, "limits": [{"metric": "peak", "net": "A"}]},
            "needs max or min",
        ),
        (
            {
                "analysis": {"kind": "op"},
                "limits": [{"metric": "peak_current", "net": "A", "max": "1A"}],
            },
            "names a part",
        ),
        ({"analysis": {"kind": "op"}, "settle_band": "0%"}, "between 0 and 100"),
        (
            {
                "analysis": {"kind": "op"},
                "limits": [{"metric": "peak", "net": "A", "max": "3"}],
            },
            "expected V",
        ),
        (
            {
                "analysis": {"kind": "op"},
                "limits": [{"metric": "current", "net": "A", "max": "1A"}],
            },
            "names a part",
        ),
        ({"analysis": {"kind": "op"}, "surprise": 1}, "Extra inputs"),
    ],
)
def test_a_malformed_request_says_what_is_wrong(
    request_data: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        SimRequest.model_validate(request_data)


def test_commands_carry_the_profile_in_each_dialect() -> None:
    tran = SimRequest.model_validate(TRAN).analysis
    ours = commands("ngspice", tran, ACCURATE)
    assert ours[0].startswith(".options reltol=0.0001 abstol=1e-13 vntol=1e-07 ")
    assert "method=trap" in ours[0] and "temp=27.0" in ours[0]
    # 5 ms in at least 2000 steps.
    assert ours[-1] == ".tran 2.5e-06 0.005 0 2.5e-06"
    theirs = commands("xyce", tran, ACCURATE)
    assert any(
        x.startswith(".options timeint method=trap reltol=0.0001") for x in theirs
    )
    assert theirs[-1] == ours[-1]
    # Xyce refuses time-integration options outside a run in time.
    op = commands("xyce", SimRequest.model_validate(OP).analysis, ACCURATE)
    assert op[-1] == ".op" and not any("timeint" in x for x in op)
    tight = tighter(ACCURATE)
    assert (tight.reltol, tight.vntol, tight.steps) == (1e-5, 1e-8, 4000)
    assert commands("ngspice", tran, tight)[-1] == ".tran 1.25e-06 0.005 0 1.25e-06"
    ac = SimRequest.model_validate(
        {
            "analysis": {"kind": "ac", "f_start": "10Hz", "f_stop": "1MHz"},
            "stimuli": [{"net": "A", "kind": "ac"}],
        }
    ).analysis
    assert commands("ngspice", ac, ACCURATE)[-1] == ".ac dec 50 10.0 1000000.0"


def test_a_simulator_is_found_only_where_it_is_said_to_be(tmp_path: Path) -> None:
    program = tmp_path / "ngspice_con.exe"
    missing = tmp_path / "nothing.exe"
    found, searched = find_simulator("ngspice", missing, environ={})
    assert found is None and searched == (f"given path {missing}",)
    found, searched = find_simulator(
        "ngspice", environ={"KAIRO_NGSPICE": str(missing), "PATH": str(tmp_path)}
    )
    # A setting that points nowhere is final: PATH is not tried after it.
    assert found is None and searched == (f"KAIRO_NGSPICE={missing}",)
    found, searched = find_simulator("xyce", environ={"PATH": str(tmp_path)})
    assert found is None and searched == ("PATH",)
    del program


def test_how_far_a_value_moves_with_temperature_comes_from_what_the_part_states() -> (
    None
):
    from kairo.ir.models import Component
    from kairo.spice.circuit import thermal_widening

    def part(lib_id: str, **params: str) -> Component:
        return Component.model_validate({"lib_id": lib_id, "params": params})

    # A coefficient is a limit either way: 100 ppm/degC over 58 degrees is 0.58%.
    resistor = part("Device:R", tempco="±100 ppm/°C")
    assert thermal_widening(resistor, "R", 58.0) == pytest.approx(0.58)
    assert thermal_widening(part("Device:R", tempco="25ppm/K"), "R", 100.0) == (
        pytest.approx(0.25)
    )
    # EIA-198: C0G is within 30 ppm/degC; X7R within 15% over its range.
    assert thermal_widening(part("Device:C", dielectric="C0G"), "C", 100.0) == (
        pytest.approx(0.3)
    )
    assert thermal_widening(part("Device:C", dielectric="X7R"), "C", 10.0) == 15.0
    # Nothing stated, or something Kairo cannot read, is not filled in.
    assert thermal_widening(part("Device:R"), "R", 58.0) is None
    assert thermal_widening(part("Device:R", tempco="low"), "R", 58.0) is None
    assert thermal_widening(part("Device:C"), "C", 58.0) is None
    assert thermal_widening(part("Device:C", dielectric="ZZZ"), "C", 58.0) is None
    assert thermal_widening(part("Device:R", dielectric="X7R"), "R", 58.0) is None


def test_kicads_own_spice_netlist_is_compared_with_the_designs_passives(
    index: LibraryIndex,
) -> None:
    from kairo.spice.crosscheck import cross_check, kicad_passives

    # What KiCad 10.0.6 wrote for the regulator example Kairo built.
    text = (
        ".title KiCad schematic\n"
        "U1 __U1\n"
        "D1 GND /LED_A LED\n"
        "C1 VIN_5V GND 10u\n"
        "J1 __J1\n"
        "R1 +3V3 /LED_A 1k\n"
        "C2 +3V3 GND 22u\n"
        ".end\n"
    )
    assert set(kicad_passives(text)) == {"C1", "R1", "C2"}
    grounded = grounded_of(index)
    names = {
        net: net if entry.role != "signal" else f"/{net}"
        for net, entry in grounded.design.nets.items()
    }
    assert cross_check(grounded, names, text) == []
    # Each kind of difference is named, with both sides.
    assert cross_check(grounded, names, text.replace("R1 +3V3", "R1 VIN_5V")) == [
        "R1 joins +3V3 and /LED_A in the design and /LED_A and VIN_5V in KiCad's "
        "SPICE netlist"
    ]
    assert cross_check(grounded, names, text.replace("LED_A 1k", "LED_A 10k")) == [
        "R1 is 1k in the design and 10k in KiCad's SPICE netlist"
    ]
    assert cross_check(grounded, names, text.replace("C2 +3V3 GND 22u\n", "")) == [
        "KiCad's SPICE netlist has no line for C2"
    ]
    assert cross_check(grounded, names, text + "R9 +3V3 GND 1k\n") == [
        "KiCad's SPICE netlist has R9, which the design does not"
    ]
    # The same value written another way is not a difference.
    assert cross_check(grounded, names, text.replace("LED_A 1k", "LED_A 1000")) == []
