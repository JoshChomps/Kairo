"""A design drawn on several sheets: what the IR accepts and refuses."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from minilib import ldo
from pydantic import ValidationError

from kairo.ir import Design


def split() -> dict[str, Any]:
    data = ldo()
    data["placement"] = {"order": []}
    data["sheets"] = {
        "supply": {"title": "Supply", "blocks": ["power_in", "regulator"]},
        "light": {"title": "Indicator", "blocks": ["indicator"]},
    }
    return data


def test_each_part_has_one_sheet() -> None:
    design = Design.model_validate(split())
    assert design.sheet_of() == {
        "J1": "supply",
        "C1": "supply",
        "U1": "supply",
        "C2": "supply",
        "R1": "light",
        "D1": "light",
    }


#: Each change to a good split, and what the refusal says.
CHANGES: list[tuple[Callable[[dict[str, Any]], object], str]] = [
    (lambda d: d["sheets"]["light"]["blocks"].clear(), "at least 1 item"),
    (
        lambda d: d["sheets"]["supply"]["blocks"].pop(),
        "block regulator is on no sheet",
    ),
    (
        lambda d: d["sheets"]["light"]["blocks"].append("regulator"),
        "block regulator is on both sheet supply and light",
    ),
    (lambda d: d["blocks"]["indicator"].remove("D1"), "D1 is in no block"),
    (
        lambda d: d["placement"].update(order=["power_in"]),
        "placement.order is not used with sheets",
    ),
    (
        lambda d: d["sheets"].update(Bad={"title": "x", "blocks": ["indicator"]}),
        "sheet name 'Bad'",
    ),
]


@pytest.mark.parametrize(("change", "message"), CHANGES)
def test_a_sheet_that_does_not_add_up_is_refused(
    change: Callable[[dict[str, Any]], object], message: str
) -> None:
    data = split()
    change(data)
    with pytest.raises(ValidationError, match=message):
        Design.model_validate(data)


def test_a_design_without_sheets_is_as_before() -> None:
    design = Design.model_validate(ldo())
    assert design.sheets == {}
    assert design.title_block.revision == ""
