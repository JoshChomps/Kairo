from __future__ import annotations

import pytest

from kairo.guard import CallGuard, LoopError


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def call(guard: CallGuard, tool: str, arguments: str, answer: str) -> None:
    guard.before(tool, arguments)
    guard.after(tool, arguments, answer)


def test_the_same_call_with_the_same_answer_is_refused_the_fourth_time() -> None:
    guard = CallGuard()
    for _ in range(3):
        call(guard, "plan_design", '{"design": 1}', "two findings")
    with pytest.raises(LoopError) as caught:
        guard.before("plan_design", '{"design": 1}')
    assert caught.value.code == "E_REPEATED_CALL"
    assert "tell the user" in str(caught.value)
    # It stays refused for as long as the model keeps asking.
    with pytest.raises(LoopError):
        guard.before("plan_design", '{"design": 1}')
    # Anything else is still answered, and starts the count again.
    call(guard, "plan_design", '{"design": 2}', "one finding")
    call(guard, "plan_design", '{"design": 1}', "two findings")


def test_a_call_whose_answer_changes_is_not_a_loop() -> None:
    # Checking a schematic again after the person edits it is ordinary work.
    guard = CallGuard()
    for count in range(10):
        call(guard, "run_erc", '{"project": "p"}', f"{count} errors")


def test_alternating_calls_are_caught_by_the_window_not_the_repeat_rule() -> None:
    clock = Clock()
    guard = CallGuard(calls_per_window=6, window_s=600, clock=clock)
    for count in range(6):
        clock.now += 1
        call(guard, "plan_design" if count % 2 else "calc", "{}", "same")
    with pytest.raises(LoopError) as caught:
        guard.before("calc", "{}")
    assert caught.value.code == "E_TOO_MANY_CALLS"
    assert "Calls are accepted again in 595 s" in str(caught.value)
    # A refused call is not counted, so the window clears on time.
    clock.now += 595
    call(guard, "calc", "{}", "same")


def test_the_window_can_be_turned_off_for_a_model_that_costs_nothing() -> None:
    guard = CallGuard(calls_per_window=0)
    for count in range(1000):
        call(guard, "calc", str(count), "x")


def test_one_tool_failing_the_same_way_five_times_is_refused() -> None:
    # A model that sends a wrong shape again and again, a little different
    # each time, gives no two calls alike; measured with a local model,
    # which did so 25 times.
    guard = CallGuard()
    for attempt in range(5):
        guard.before("plan_design", f'{{"design": {attempt}}}')
        guard.after(
            "plan_design", f'{{"design": {attempt}}}', "bad", error="E_DESIGN_INVALID"
        )
    with pytest.raises(LoopError) as caught:
        guard.before("plan_design", '{"design": 99}')
    assert caught.value.code == "E_REPEATED_FAILURE"
    assert "E_DESIGN_INVALID" in str(caught.value)
    assert "tell the user" in str(caught.value)
    # Another tool is still answered, and its success lets the first be tried.
    call(guard, "design_schema", "{}", "the schema")
    guard.before("plan_design", '{"design": 100}')


def test_failures_of_different_kinds_are_not_a_loop() -> None:
    guard = CallGuard()
    for attempt in range(12):
        arguments = f'{{"design": {attempt}}}'
        guard.before("plan_design", arguments)
        guard.after("plan_design", arguments, "bad", error=f"E_{attempt % 2}")
