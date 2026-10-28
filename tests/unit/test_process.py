from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from kairo.process import process_alive, run_process

PY = sys.executable

# A parent that starts a sleeping child, records both ids, then sleeps too.
SPAWN_AND_SLEEP = """
import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
open(sys.argv[1], "w").write(f"{os.getpid()} {child.pid}")
time.sleep(120)
"""

# A parent that starts a sleeping child, records it, and exits at once.
SPAWN_AND_EXIT = """
import subprocess, sys
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
open(sys.argv[1], "w").write(str(child.pid))
"""


def wait_until_dead(pids: list[int], within_s: float = 10.0) -> list[int]:
    """Return the pids still alive after waiting, which should be none."""
    deadline = time.monotonic() + within_s
    alive = pids
    while alive and time.monotonic() < deadline:
        alive = [pid for pid in alive if process_alive(pid)]
        time.sleep(0.05)
    return alive


def read_pids(path: Path) -> list[int]:
    return [int(p) for p in path.read_text(encoding="utf-8").split()]


def test_captures_output_and_exit_code() -> None:
    script = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)"
    result = run_process([PY, "-c", script], timeout_s=30)
    assert result.returncode == 7
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"
    assert not result.timed_out
    assert result.pid > 0


def test_runs_in_the_given_folder(tmp_path: Path) -> None:
    result = run_process(
        [PY, "-c", "import os; print(os.getcwd())"], timeout_s=30, cwd=tmp_path
    )
    assert Path(result.stdout.strip()).resolve() == tmp_path.resolve()


def test_timeout_kills_the_program_and_its_children(tmp_path: Path) -> None:
    pid_file = tmp_path / "pids.txt"
    result = run_process([PY, "-c", SPAWN_AND_SLEEP, str(pid_file)], timeout_s=3)
    assert result.timed_out
    assert result.returncode is None
    pids = read_pids(pid_file)
    assert len(pids) == 2
    assert wait_until_dead(pids) == []


def test_children_left_behind_after_a_normal_exit_are_killed(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid.txt"
    result = run_process([PY, "-c", SPAWN_AND_EXIT, str(pid_file)], timeout_s=30)
    assert result.returncode == 0
    assert wait_until_dead(read_pids(pid_file)) == []


def test_missing_program_raises() -> None:
    with pytest.raises(FileNotFoundError):
        run_process(["kairo-no-such-program-here"], timeout_s=5)


def test_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError):
        run_process([PY, "-c", "pass"], timeout_s=0)


def test_process_alive_is_false_for_a_finished_process() -> None:
    result = run_process([PY, "-c", "pass"], timeout_s=30)
    assert wait_until_dead([result.pid]) == []
