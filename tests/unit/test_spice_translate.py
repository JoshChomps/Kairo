"""PSpice model files in Xyce's spelling, and where Kairo keeps the copy."""

from __future__ import annotations

from pathlib import Path

from kairo.spice.engines import (
    ACCURATE,
    PSPICE_NEWTON_LOOSER,
    STEPS_BY_ITERATIONS,
    commands,
)
from kairo.spice.registry import XYCE_FOLDER, ModelRegistry
from kairo.spice.request import Analysis
from kairo.spice.translate import pspice_to_xyce

# Written for these tests in PSpice's spelling; not a maker's model.
MODEL = """* test model, not a maker's
.SUBCKT T IN OUT GND
E_A A GND TABLE { V(IN, GND) }
+ ( (0,1) (100m, 1.1)(500m,1.14) )
E_B B GND TABLE { V(IN) } = (0,0) (1,2)
G_C C GND TABLE {V(IN)} ((0,0) (1,1e-3))
C_SS SS GND {1e-6*SQRT(2)} IC=0
L_1 OUT GND 1u
+ IC=1m
C_2 OUT GND 1u
R_IC IC GND 1k
.ENDS T
"""


def test_a_table_gets_its_equals_sign_and_loses_its_outer_brackets() -> None:
    made = pspice_to_xyce(MODEL)
    lines = made.text.splitlines()
    assert "E_A A GND TABLE { V(IN, GND) } = (0,1) (100m, 1.1)(500m,1.14)" in lines
    assert "G_C C GND TABLE {V(IN)} = (0,0) (1,1e-3)" in lines
    # Already in Xyce's spelling: left as written.
    assert "E_B B GND TABLE { V(IN) } = (0,0) (1,2)" in lines
    assert made.edits[0] == (
        "line 3, E_A: TABLE written with '=' and no outer parentheses"
    )
    assert made.edits[1].startswith("line 6, G_C:")


def test_an_initial_condition_on_a_capacitor_or_inductor_is_dropped() -> None:
    made = pspice_to_xyce(MODEL)
    lines = made.text.splitlines()
    assert "C_SS SS GND {1e-6*SQRT(2)}" in lines
    # Across a continuation line too, and nothing else on it is lost.
    assert "L_1 OUT GND 1u" in lines and "+" in lines
    assert "C_2 OUT GND 1u" in lines and "R_IC IC GND 1k" in lines
    assert [e.split(":")[0] for e in made.edits[2:]] == [
        "line 7, C_SS",
        "line 8, L_1",
    ]
    assert len(made.edits) == 4


def test_a_file_that_needs_nothing_comes_back_as_it_was() -> None:
    plain = "* plain\n.subckt P a b\nR1 a b 1k\n.ends P\n"
    made = pspice_to_xyce(plain)
    assert made.text == plain and made.edits == ()


def entry(registry: ModelRegistry, dialect: str) -> None:
    registry.register(
        {
            "lib_id": "Reg:T",
            "file": "t.lib",
            "subckt": "T",
            "nodes": ["IN", "OUT", "0"],
            "dialect": dialect,
            "engines": ["ngspice", "xyce"],
            "fidelity": "vendor-converted" if dialect == "pspice" else "vendor",
            "manufacturer": "Kairo test data",
            "source": "tests/unit/test_spice_translate.py",
            "version": "1",
            "covers": "nothing; it is a test model",
        }
    )


def test_xyce_reads_a_copy_named_by_the_originals_hash(tmp_path: Path) -> None:
    (tmp_path / "t.lib").write_text(MODEL, encoding="utf-8")
    registry = ModelRegistry(tmp_path)
    entry(registry, "pspice")
    found = registry.find(mpn=None, lib_id="Reg:T")
    assert found is not None
    path, edits = registry.for_xyce(found)
    assert path == (tmp_path / XYCE_FOLDER / f"{found.sha256}.lib").resolve()
    assert path.read_text(encoding="latin-1") == pspice_to_xyce(MODEL).text
    assert len(edits) == 4
    # The original is untouched, so its hash still holds.
    assert registry.find(mpn=None, lib_id="Reg:T") == found
    # A model in plain SPICE is read as it is.
    entry(registry, "spice3")
    plain = registry.find(mpn=None, lib_id="Reg:T")
    assert plain is not None
    assert registry.for_xyce(plain) == ((tmp_path / "t.lib").resolve(), ())


def test_xyce_steps_by_iterations_only_in_time_and_only_with_a_pspice_model() -> None:
    tran = Analysis.model_validate({"kind": "tran", "duration": "1ms"})
    with_model = commands("xyce", tran, ACCURATE, pspice=True)
    assert ".options timeint method=trap erroption=1" in with_model
    looser = ACCURATE.abstol * PSPICE_NEWTON_LOOSER
    assert f".options nonlin-tran reltol=0.0001 abstol={looser!r}" in with_model
    assert not any("erroption" in line for line in commands("xyce", tran, ACCURATE))
    op = Analysis.model_validate({"kind": "op"})
    assert not any(
        "erroption" in line for line in commands("xyce", op, ACCURATE, pspice=True)
    )
    assert not any(
        "erroption" in line for line in commands("ngspice", tran, ACCURATE, pspice=True)
    )
    assert "iteration count" in STEPS_BY_ITERATIONS
