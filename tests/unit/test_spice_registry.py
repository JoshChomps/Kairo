"""Registered model files: recorded with a hash, refused when they change."""

# These tests change plain JSON data, which a strict type checker cannot follow.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from minilib import ldo, make_index

from kairo.ir import Design
from kairo.library import LibraryIndex
from kairo.library.grounding import ground
from kairo.spice.circuit import build_circuit, connections, netlist
from kairo.spice.registry import (
    INDEX,
    ModelRegistry,
    RegistryError,
    file_sha256,
    subckt_nodes,
)
from kairo.spice.request import SimRequest

# Written for these tests; not any maker's model. A regulator as a 3.3 V
# source behind 10 mohm, which is enough to see it used.
MODEL = """* test regulator, not a maker's model
.subckt TESTREG IN OUT
+ GND_A GND_B PARAMS: VSET=3.3
R1 GND_A GND_B 1m
E1 mid GND_A VALUE { MIN({VSET}, V(IN, GND_A)) }
R2 mid OUT 10m
R3 IN GND_A 1k
.ends TESTREG
"""
FACTS: dict[str, Any] = {
    "lib_id": "Reg:REG-3.3",
    "file": "testreg.lib",
    "subckt": "TESTREG",
    "nodes": ["VI", "VO", "GND", "GND"],
    "dialect": "pspice",
    "engines": ["ngspice"],
    "fidelity": "vendor-converted",
    "manufacturer": "Kairo test data",
    "source": "tests/unit/test_spice_registry.py",
    "version": "1",
    "covers": "nothing; it is a test model",
}


@pytest.fixture
def models(tmp_path: Path) -> ModelRegistry:
    folder = tmp_path / "models"
    folder.mkdir()
    (folder / "testreg.lib").write_text(MODEL, encoding="utf-8")
    return ModelRegistry(folder)


@pytest.fixture
def index(tmp_path: Path) -> LibraryIndex:
    libs = tmp_path / "libs"
    libs.mkdir()
    return make_index(libs)


def test_a_registered_model_is_found_by_symbol_and_by_part_number(
    models: ModelRegistry,
) -> None:
    assert models.find(mpn=None, lib_id="Reg:REG-3.3") is None
    entry = models.register(FACTS)
    assert entry.sha256 == file_sha256(models.folder / "testreg.lib")
    found = models.find(mpn=None, lib_id="Reg:REG-3.3")
    assert found is not None and found == entry
    assert found.cite() == ("Kairo test data, tests/unit/test_spice_registry.py, 1")
    assert models.find(mpn=None, lib_id="Reg:OTHER") is None
    # A model by part number wins for a part that states that number, and
    # is not used for one that does not.
    by_number = models.register(
        {**FACTS, "lib_id": None, "mpn": "REG33-A", "version": "2"}
    )
    assert models.find(mpn="REG33-A", lib_id="Reg:REG-3.3") == by_number
    assert models.find(mpn="OTHER", lib_id="Reg:REG-3.3") == entry
    assert models.find(mpn="REG33-A", lib_id="Else:X") == by_number
    # Registering the same part again replaces its record.
    models.register({**FACTS, "version": "3"})
    assert [e.version for e in models.entries()] == ["2", "3"]


def test_a_model_file_that_changes_is_refused_until_registered_again(
    models: ModelRegistry,
) -> None:
    models.register(FACTS)
    path = models.folder / "testreg.lib"
    path.write_text(MODEL.replace("10m", "20m"), encoding="utf-8")
    with pytest.raises(
        RegistryError, match=r"E_MODEL_CHANGED: testreg\.lib has changed"
    ):
        models.find(mpn=None, lib_id="Reg:REG-3.3")
    again = models.register(FACTS)
    assert models.find(mpn=None, lib_id="Reg:REG-3.3") == again
    path.unlink()
    with pytest.raises(RegistryError, match="E_MODEL_MISSING"):
        models.find(mpn=None, lib_id="Reg:REG-3.3")


def test_a_record_that_does_not_fit_its_file_is_refused(models: ModelRegistry) -> None:
    with pytest.raises(
        RegistryError, match=r"has 4 nodes \(IN OUT GND_A GND_B\) and 3"
    ):
        models.register({**FACTS, "nodes": ["VI", "VO", "GND"]})
    with pytest.raises(RegistryError, match="defines no subcircuit NOPE"):
        models.register({**FACTS, "subckt": "NOPE"})
    with pytest.raises(RegistryError, match="E_MODEL_MISSING"):
        models.register({**FACTS, "file": "absent.lib"})
    with pytest.raises(RegistryError, match="vendor-converted, not vendor"):
        models.register({**FACTS, "fidelity": "vendor"})
    with pytest.raises(RegistryError, match="by mpn or lib_id"):
        models.register({**FACTS, "lib_id": None})
    assert models.entries() == []
    (models.folder / INDEX).write_text("{not json", encoding="utf-8")
    with pytest.raises(RegistryError, match="E_MODEL_REGISTRY"):
        models.entries()


def test_a_subcircuits_nodes_are_read_across_continuation_lines() -> None:
    assert subckt_nodes(MODEL, "testreg") == ["IN", "OUT", "GND_A", "GND_B"]
    assert subckt_nodes(".SUBCKT A 1 2 3 4\n.ends", "A") == ["1", "2", "3", "4"]
    assert subckt_nodes(".subckt B in out VDD=1 VSS=0\n.ends", "B") == ["in", "out"]
    assert subckt_nodes(MODEL, "other") is None


def test_a_part_with_a_registered_model_uses_it(
    models: ModelRegistry, index: LibraryIndex
) -> None:
    entry = models.register(FACTS)
    grounded = ground(Design.model_validate(ldo()), index)
    request = SimRequest.model_validate({"analysis": {"kind": "op"}})
    circuit = build_circuit(grounded, request, registry=models)
    names = {e.name: e for e in circuit.elements}
    # The subcircuit's four nodes on the regulator's three pins, by name.
    assert names["X_u1"].nodes == ("u1_3", "u1_2", "u1_1", "u1_1")
    assert names["X_u1"].value == "TESTREG"
    assert "B_u1" not in names and "F_u1" not in names
    assert circuit.includes == ((models.folder / "testreg.lib").resolve().as_posix(),)
    assert circuit.pspice and circuit.engines == ("ngspice",)
    assert circuit.regulated == {"+3V3": "V_u1_2"}
    note = next(n for n in circuit.models if n.ref == "U1")
    assert note.fidelity == "vendor-converted" and note.sha256 == entry.sha256
    assert note.as_dict()["sha256"] == entry.sha256
    assert "has not been compared with it" in note.unproven
    text = netlist(circuit, "ngspice", [".op"])
    assert f'.include "{circuit.includes[0]}"' in text
    assert "X_u1 u1_3 u1_2 u1_1 u1_1 TESTREG" in text
    # The connections still read back as the design's.
    graph: dict[str, set[tuple[str, str]]] = {}
    for pin in grounded.pins:
        graph.setdefault(pin.net, set()).add((pin.ref, pin.pin.number))
    assert connections(text) == graph


def test_a_model_whose_pin_is_not_connected_leaves_the_part_open(
    models: ModelRegistry, index: LibraryIndex
) -> None:
    models.register({**FACTS, "nodes": ["VI", "VO", "GND", "ENABLE"]})
    grounded = ground(Design.model_validate(ldo()), index)
    circuit = build_circuit(
        grounded,
        SimRequest.model_validate({"analysis": {"kind": "op"}}),
        registry=models,
    )
    open_parts = {u.ref: u for u in circuit.unmodelled}
    assert "needs its pin ENABLE connected" in open_parts["U1"].why
    assert circuit.includes == () and not circuit.pspice
    # A file changed behind the registry stops the build, it is not skipped.
    models.register(FACTS)
    (models.folder / "testreg.lib").write_text(MODEL + "* edited\n", encoding="utf-8")
    with pytest.raises(RegistryError, match="E_MODEL_CHANGED"):
        build_circuit(
            grounded,
            SimRequest.model_validate({"analysis": {"kind": "op"}}),
            registry=models,
        )
    assert json.loads((models.folder / INDEX).read_text("utf-8"))["models"]


def test_xyce_is_given_the_copy_and_ngspice_the_original(
    models: ModelRegistry, index: LibraryIndex
) -> None:
    (models.folder / "testreg.lib").write_text(
        MODEL.replace("R3 IN GND_A 1k", "R3 IN GND_A 1k\nC9 IN GND_A 1n IC=0"),
        encoding="utf-8",
    )
    entry = models.register({**FACTS, "engines": ["ngspice", "xyce"]})
    grounded = ground(Design.model_validate(ldo()), index)
    circuit = build_circuit(
        grounded,
        SimRequest.model_validate({"analysis": {"kind": "op"}}),
        registry=models,
    )
    original = (models.folder / "testreg.lib").resolve().as_posix()
    copy = (models.folder / ".xyce" / f"{entry.sha256}.lib").resolve().as_posix()
    assert circuit.engines == ("ngspice", "xyce")
    assert f'.include "{original}"' in netlist(circuit, "ngspice", [".op"])
    xyce = netlist(circuit, "xyce", [".op"])
    assert f'.include "{copy}"' in xyce and original not in xyce
    note = next(n for n in circuit.models if n.ref == "U1")
    assert "Xyce reads a copy changed only in spelling: line 8, C9: IC=" in note.note


def test_kicads_simulation_fields_name_the_model_or_say_why_not(
    models: ModelRegistry, index: LibraryIndex
) -> None:
    from kairo.spice.kicad_fields import sim_fields

    grounded = ground(Design.model_validate(ldo()), index)
    assert sim_fields(grounded, None) == ({}, [])
    models.register(FACTS)
    fields, skipped = sim_fields(grounded, models)
    numbers = {p.pin.name: p.pin.number for p in grounded.pins if p.ref == "U1"}
    assert fields == {
        "U1": (
            ("Sim.Library", (models.folder / "testreg.lib").resolve().as_posix()),
            ("Sim.Name", "TESTREG"),
            (
                "Sim.Pins",
                f"{numbers['VI']}=1 {numbers['VO']}=2 "
                f"{numbers['GND']}=3 {numbers['GND']}=4",
            ),
        )
    }
    assert skipped == []
    # A node tied to ground inside the registry has no pin to name.
    models.register({**FACTS, "nodes": ["VI", "VO", "GND", "0"]})
    assert sim_fields(grounded, models) == (
        {},
        [
            "U1: its model ties node 4 to ground, which KiCad's fields can only "
            "give through a pin"
        ],
    )
    models.register({**FACTS, "nodes": ["VI", "VO", "GND", "ENABLE"]})
    assert sim_fields(grounded, models)[1] == [
        "U1: its model's node 4 needs pin ENABLE connected"
    ]
    # A file changed since it was registered gives no fields, and says so.
    (models.folder / "testreg.lib").write_text(MODEL + "* edited\n", encoding="utf-8")
    fields, skipped = sim_fields(grounded, models)
    assert fields == {} and "E_MODEL_CHANGED" in skipped[0]
