"""The text a model reads for a tool's result: compact, like records as tables."""

from __future__ import annotations

import json

from kairo.server import response_text


def test_like_records_are_sent_as_a_table_and_others_as_they_are() -> None:
    pins = [
        {"ref": "R1", "pin": "1", "type": "passive"},
        {"ref": "R2", "pin": "2", "type": "passive"},
        {"ref": "U1", "pin": "3", "type": "power_in"},
    ]
    sent = json.loads(response_text({"net": "GND", "pins": pins, "few": pins[:2]}))
    assert sent["pins"] == {
        "columns": ["ref", "pin", "type"],
        "rows": [
            ["R1", "1", "passive"],
            ["R2", "2", "passive"],
            ["U1", "3", "power_in"],
        ],
    }
    # Two records, or records whose fields differ, are left as records.
    assert sent["few"] == pins[:2]
    mixed = [*pins, {"ref": "C1", "pin": "1"}]
    assert json.loads(response_text({"pins": mixed}))["pins"] == mixed
    # Lists inside records are tabled too; text has no spaces.
    nested = response_text({"nets": [{"name": n, "pins": pins} for n in "ABC"]})
    assert " " not in nested
    assert json.loads(nested)["nets"]["rows"][0][1]["columns"] == ["ref", "pin", "type"]
