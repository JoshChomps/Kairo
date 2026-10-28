"""The simulate tool through an MCP client, on KiCad's own libraries.

Needs both KiCad, for the libraries, and ngspice. Xyce joins when it is
installed.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from kairo.server import Settings, build_server, default_cache_dir
from kairo.spice.engines import find_simulator

pytestmark = [pytest.mark.kicad, pytest.mark.spice]

DESIGNS = Path(__file__).resolve().parent.parent.parent / "evals" / "reference_designs"


def call_all(settings: Settings, calls: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    async def talk() -> list[Any]:
        out: list[Any] = []
        async with Client(build_server(settings)) as client:
            for name, arguments in calls:
                result = await client.call_tool(name, arguments)
                text = "".join(getattr(part, "text", "") for part in result.content)
                # Every response is small enough for a model to read.
                assert 0 < len(text) < 25_000, name
                out.append(text if result.is_error else result.structured_content)
        return out

    return asyncio.run(talk())


def test_a_filter_and_the_regulator_example_are_simulated_and_drawn(
    kicad_cli_path: Path, tmp_path: Path
) -> None:
    ngspice, searched = find_simulator("ngspice")
    if ngspice is None:
        pytest.skip(f"ngspice not found (searched: {'; '.join(searched)})")
    # The machine's own library index is copied nowhere: only the
    # simulations folder is redirected, by giving this run its own cache
    # that shares the index file.
    cache = tmp_path / "cache"
    cache.mkdir()
    source = default_cache_dir(os.environ)
    for item in source.glob("library-kicad*.db"):
        (cache / item.name).write_bytes(item.read_bytes())
    settings = Settings((tmp_path,), cache, kicad_cli_path, tier="propose")
    rc = json.loads((DESIGNS / "rc_lowpass" / "design.json").read_text("utf-8"))
    ldo = json.loads((DESIGNS / "ldo_3v3" / "design.json").read_text("utf-8"))
    step = {
        "analysis": {"kind": "tran", "duration": "1ms"},
        "stimuli": [{"net": "IN", "kind": "step", "value": "3.3V", "rise": "1us"}],
        "limits": [{"metric": "settling_time", "net": "OUT", "max": "400us"}],
    }
    plug = {
        "analysis": {"kind": "tran", "duration": "400us"},
        "stimuli": [
            {
                "net": "VIN_5V",
                "kind": "step",
                "value": "5V",
                "rise": "1us",
                "resistance": "50mohm",
                "inductance": "1uH",
            }
        ],
    }
    filtered, steady, plugged, bad, unknown = call_all(
        settings,
        [
            ("simulate", {"design": rc, "request": step, "nets": ["OUT"]}),
            ("simulate", {"design": ldo, "request": {"analysis": {"kind": "op"}}}),
            ("simulate", {"design": ldo, "request": plug}),
            ("simulate", {"design": rc, "request": {"analysis": {"kind": "tran"}}}),
            (
                "simulate",
                {
                    "design": rc,
                    "request": {
                        "analysis": {"kind": "op"},
                        "stimuli": [{"net": "NOPE", "value": "1V"}],
                    },
                },
            ),
        ],
    )

    # The filter: KiCad's connectors are open, everything else is ideal,
    # and the 460 us settling time breaks the 400 us asked for.
    assert next(iter(filtered)) == "status"
    assert filtered["limits"][0]["verdict"] == "fail"
    assert {m["where"] for m in filtered["metrics"]} == {"OUT"}
    rise = next(m for m in filtered["metrics"] if m["metric"] == "rise_time")
    assert rise["value"] == pytest.approx(2.197e-4, rel=2e-3)
    assert rise["status"] in ("checked", "unverified")
    folder = Path(filtered["files"]["folder"])
    assert folder.parent == cache / "simulations"
    assert filtered["files"]["saved"] == [
        "currents.svg",
        "curves.csv",
        "netlist.cir",
        "voltages.svg",
    ]
    assert (folder / "voltages.svg").read_text("utf-8").startswith("<svg ")
    assert (folder / "netlist.cir").read_text("utf-8").endswith(".end\n")

    # The regulator at rest: every net at its design voltage, and the
    # calculated currents beside the simulated ones.
    volts = {
        m["where"]: m["value"] for m in steady["metrics"] if m["metric"] == "voltage"
    }
    assert volts["VIN_5V"] == 5.0 and volts["+3V3"] == 3.3
    assert all(c["agree"] for c in steady["compared"]) and len(steady["compared"]) == 2
    assert steady["practice"] == []
    assert steady["files"]["saved"] == ["netlist.cir"]

    # Plugged in live through a cable's inductance, the 5 V input rings far
    # past 5 V. The regulator's model makes every number unverified, and
    # the practice check still says what to do about it.
    assert plugged["status"] == "unverified"
    assert any("U1 is a behavioural model" in r for r in plugged["unverified_because"])
    codes = [a["code"] for a in plugged["practice"]]
    assert "A_HOT_PLUG_RINGING" in codes
    ringing = next(a for a in plugged["practice"] if a["code"] == "A_HOT_PLUG_RINGING")
    assert ringing["from_checked_numbers"] is False and "AN88" in ringing["source"]

    assert isinstance(bad, str) and "E_SIM_REQUEST_INVALID" in bad
    assert "needs its duration" in bad
    assert isinstance(unknown, str) and "'NOPE' is not in the design" in unknown


def test_a_loop_is_measured_through_the_tool(
    kicad_cli_path: Path, tmp_path: Path
) -> None:
    from kairo.spice.registry import ModelRegistry

    if find_simulator("ngspice")[0] is None:
        pytest.skip("ngspice not found")
    cache = tmp_path / "cache"
    cache.mkdir()
    for item in default_cache_dir(os.environ).glob("library-kicad*.db"):
        (cache / item.name).write_bytes(item.read_bytes())
    # A test amplifier, registered for a three-pin regulator symbol: gain
    # -1000 behind 1 kilohm and 1 uF. With 10 kilohms and 10 nF fed back
    # to its input it is the two-pole loop of the simulation tests, whose
    # phase margin is 6.36 degrees.
    models = tmp_path / "models"
    models.mkdir()
    (models / "amp.lib").write_text(
        "* test amplifier\n.subckt TESTAMP IN OUT GND\nE1 e GND GND IN 1000\n"
        "R1 e OUT 1000\nC1 OUT GND 1e-6\n.ends TESTAMP\n",
        encoding="utf-8",
    )
    ModelRegistry(models).register(
        {
            "lib_id": "Regulator_Linear:AMS1117-3.3",
            "file": "amp.lib",
            "subckt": "TESTAMP",
            "nodes": ["VI", "VO", "GND"],
            "dialect": "spice3",
            "engines": ["ngspice", "xyce"],
            "fidelity": "vendor",
            "manufacturer": "Kairo test data",
            "source": "tests/integration/test_simulate_tool.py",
            "version": "1",
            "covers": "nothing; it is a test model",
        }
    )
    settings = Settings(
        (tmp_path,), cache, kicad_cli_path, tier="propose", models_dir=models
    )
    design = {
        "design": "loop",
        "nets": {
            "OUT": {"role": "signal"},
            "FB": {"role": "signal"},
            "GND": {"role": "ground"},
        },
        "components": {
            "U1": {"lib_id": "Regulator_Linear:AMS1117-3.3"},
            "R2": {"lib_id": "Device:R", "value": "10k"},
            "C2": {"lib_id": "Device:C", "value": "10n"},
        },
        "connections": {
            "OUT": ["U1.VO", "R2.1"],
            "FB": ["R2.2", "C2.1", "U1.VI"],
            "GND": ["U1.GND", "C2.2"],
        },
    }
    sweep = {"kind": "ac", "f_start": "10Hz", "f_stop": "1MHz"}
    measured, undirected = call_all(
        settings,
        [
            (
                "simulate",
                {
                    "design": design,
                    "request": {
                        "analysis": sweep,
                        "loop": {"pin": "U1.VI", "into": "pin"},
                    },
                },
            ),
            (
                "simulate",
                {
                    "design": design,
                    "request": {"analysis": sweep, "loop": {"pin": "U1.VI"}},
                },
            ),
        ],
    )
    assert next(iter(measured)) == "status"
    assert measured["phase_margin_deg"] == pytest.approx(6.356, abs=0.05)
    assert measured["gain_margin_db"] is None
    assert measured["signal_flows_toward"] == "pin"
    assert "Tian" in measured["method"]
    assert isinstance(undirected, str) and "give loop.into" in undirected
