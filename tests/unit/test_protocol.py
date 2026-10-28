"""The steps Kairo gives on its keyword."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from mcp import Client

from kairo import protocol
from kairo.server import TIERS, TOOL_DOCS, Settings, build_server


def offered(tier: str) -> set[str]:
    """The tools a server at this tier offers."""
    allowed = TIERS[: TIERS.index(tier) + 1]
    return {name for name, doc in TOOL_DOCS.items() if doc.tier in allowed}


@pytest.mark.parametrize("tier", TIERS)
def test_steps_name_only_tools_the_tier_offers(tier: str) -> None:
    text = " ".join(protocol.steps(tier))
    named = {name for name in TOOL_DOCS if re.search(rf"\b{name}\b", text)}
    assert named, "the steps name no tool at all"
    assert named <= offered(tier)


def test_a_higher_tier_is_told_to_check_before_it_writes() -> None:
    steps = protocol.steps("write")
    plan = next(i for i, s in enumerate(steps) if "plan_design" in s)
    write = next(i for i, s in enumerate(steps) if "apply_design" in s)
    assert plan < write
    assert not any("apply_design" in s for s in protocol.steps("propose"))


@pytest.mark.parametrize("tier", TIERS)
def test_every_tier_asks_before_assuming_a_value(tier: str) -> None:
    assert "Never choose one yourself" in protocol.steps(tier)[0]


def test_go_numbers_the_steps_and_returns_the_request() -> None:
    answer = protocol.go("propose", "  a 3.3 V rail from 5 V  ")
    assert answer["request"] == "a 3.3 V rail from 5 V"
    assert answer["tier"] == "propose"
    assert [s.split(".")[0] for s in answer["steps"]] == [
        str(n) for n in range(1, len(protocol.steps("propose")) + 1)
    ]


def test_the_server_offers_go_as_a_tool_and_a_prompt(tmp_path: Path) -> None:
    server = build_server(
        Settings(allowed_roots=(tmp_path,), cache_dir=tmp_path / "cache", tier="write")
    )

    async def ask() -> tuple[str, list[str], dict[str, object], str]:
        async with Client(server) as client:
            prompts = await client.list_prompts()
            called = await client.call_tool("go", {"request": "an LED on 5 V"})
            got = await client.get_prompt("go", {"request": "an LED on 5 V"})
            message = got.messages[0].content
            return (
                client.instructions or "",
                [p.name for p in prompts.prompts],
                called.structured_content or {},
                getattr(message, "text", ""),
            )

    instructions, prompts, called, message = asyncio.run(ask())
    for keyword in protocol.KEYWORDS:
        assert f'"{keyword}"' in instructions
    assert prompts == ["go"]
    assert called["request"] == "an LED on 5 V"
    assert called["tier"] == "write"
    assert "an LED on 5 V" in message
    assert "apply_design" in message


def test_the_board_step_names_what_the_board_summary_asks_a_model_to_act_on() -> None:
    from kairo.pcb.make import ACTED_ON

    board = next(s for s in protocol.steps("write") if "make_board" in s)
    for key in ACTED_ON:
        assert key in board, key
    # Blocks are where a model's choice decides placement.
    assert "block of the IC" in board
