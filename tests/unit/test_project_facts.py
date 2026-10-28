"""Facts kept with a project, and a call's facts laid over them."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kairo.kicad.project_facts import (
    FACTS_FILE,
    FactsFileError,
    facts_text,
    laid_over,
    load_facts,
)

KEPT: dict[str, Any] = {
    "nets": {"GND": {"role": "ground"}, "+5V": {"role": "power", "voltage": "5V"}},
    "components": {
        "U1": {"params": {"i_q": "5mA", "v_ref": "1.25V"}, "source": "datasheet"}
    },
    "loads": [{"net": "+5V", "current": "10mA"}],
    "policy": {"margins": "stated"},
}


def test_a_calls_facts_replace_entries_and_merge_a_parts_figures() -> None:
    given: dict[str, Any] = {
        "nets": {"+5V": {"role": "power", "voltage": "5.1V"}},
        "components": {"U1": {"params": {"i_q": "6mA"}}, "R1": {"params": {}}},
        "loads": [{"net": "+5V", "current": "20mA"}],
        "policy": {"theta_ja_margin": "20%"},
    }
    out = laid_over(KEPT, given)
    assert out["nets"] == {
        "GND": {"role": "ground"},
        "+5V": {"role": "power", "voltage": "5.1V"},
    }
    assert out["components"]["U1"] == {
        "params": {"i_q": "6mA", "v_ref": "1.25V"},
        "source": "datasheet",
    }
    assert out["components"]["R1"] == {"params": {}}
    assert out["loads"] == [{"net": "+5V", "current": "20mA"}]
    assert out["policy"] == {"margins": "stated", "theta_ja_margin": "20%"}
    # Neither input is changed, and nothing given keeps everything.
    assert KEPT["components"]["U1"]["params"]["i_q"] == "5mA"
    assert laid_over(KEPT, {}) == KEPT
    assert laid_over({}, KEPT) == KEPT


def test_the_kept_file_is_read_or_refused_with_a_reason(tmp_path: Path) -> None:
    assert load_facts(tmp_path) == {}
    path = tmp_path / FACTS_FILE
    path.parent.mkdir()
    path.write_text(facts_text(KEPT), encoding="utf-8")
    assert load_facts(tmp_path) == KEPT
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(FactsFileError, match=r"E_FACTS_FILE: .* JSON object"):
        load_facts(tmp_path)
    path.write_text("{oops", encoding="utf-8")
    with pytest.raises(FactsFileError, match="E_FACTS_FILE"):
        load_facts(tmp_path)
