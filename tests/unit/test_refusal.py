from __future__ import annotations

import ast
import re
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from kairo.guard import LoopError
from kairo.refusal import REMEDIES, UNLISTED, refusal

SOURCE = Path(__file__).resolve().parent.parent.parent / "src" / "kairo"
CODE = re.compile(r"E_[A-Z0-9_]+")


def _leading_text(node: ast.expr) -> str | None:
    """The text an exception's first argument starts with, if written out."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values:
        return _leading_text(node.values[0])
    if isinstance(node, ast.BinOp):
        return _leading_text(node.left)
    return None


def raised_codes() -> dict[str, set[str]]:
    """Every code a module raises, by the text it starts an error with.

    A tool's own errors start their message with the code; the lower
    layers' errors (a transaction, an edit) pass it as their first
    argument. The grounding layer's findings reach a tool's caller too,
    as the code of the refusal that lists them.
    """
    codes: dict[str, set[str]] = {}
    for path in SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                call = node.exc
            elif (
                # An error class that writes its own code (PatchError).
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "__init__"
            ):
                call = node
            elif (
                path.name == "grounding.py"
                and isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Finding"
            ):
                call = ast.Call(func=node.func, args=node.args[1:], keywords=[])
            else:
                continue
            if not call.args:
                continue
            text = _leading_text(call.args[0])
            if text is not None and (found := CODE.match(text)):
                codes.setdefault(found.group(0), set()).add(path.name)
    return codes


def test_every_code_a_tool_can_refuse_with_says_what_clears_it() -> None:
    codes = raised_codes()
    # The scan must see the layers below the tools, not only the tools.
    assert {"E_PATH_NOT_ALLOWED", "E_FILE_CHANGED", "E_PIN_UNKNOWN"} <= set(codes)
    assert {"E_FOOTPRINT_UNKNOWN", "E_DATASHEET_NO_LINK"} <= set(codes)
    assert sorted(set(codes) - set(REMEDIES)) == []
    # And the table holds nothing that is no longer raised.
    assert sorted(set(REMEDIES) - set(codes)) == []


def test_a_refusal_carries_the_code_the_error_and_what_to_do() -> None:
    answer = refusal(
        ToolError("E_PATH_NOT_ALLOWED: /nowhere is not under an allowed root")
    )
    assert answer == {
        "ok": False,
        "code": "E_PATH_NOT_ALLOWED",
        "error": "/nowhere is not under an allowed root",
        "fix": REMEDIES["E_PATH_NOT_ALLOWED"].fix,
        "retryable": False,
    }
    assert "list_projects" in answer["fix"]


def test_only_a_fault_outside_the_call_is_worth_retrying() -> None:
    assert refusal(ToolError("E_FILE_CHANGED: demo.kicad_sch changed"))["retryable"]
    assert not refusal(ToolError("E_DESIGN_INVALID: nets is a list"))["retryable"]
    assert not refusal(LoopError("E_REPEATED_CALL", "same answer"))["retryable"]
    stops = ("E_NO_PROGRESS", "E_ROUND_LIMIT", "E_REPEATED_CALL", "E_TOO_MANY_CALLS")
    assert all(REMEDIES[code].fix.startswith("Stop") for code in stops)


def test_an_error_without_a_code_is_still_a_refusal() -> None:
    answer = refusal(ToolError("something odd: with a colon"))
    assert answer["code"] == "E_TOOL_FAILED"
    assert answer["error"] == "something odd: with a colon"
    assert answer["fix"] == UNLISTED.fix
    assert refusal(ToolError("E_NEW_CODE: x"))["fix"] == UNLISTED.fix
