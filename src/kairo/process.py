"""Run external programs with a timeout, and never leave any of their processes behind.

Every program Kairo drives (kicad-cli now, simulators later) runs through
`run_process`. On a timeout the whole process tree is killed, not just the
program Kairo started, and the tree is also swept after a normal exit, so a
helper the program spawned cannot outlive it.

On Windows the program starts suspended inside a Job Object that kills
everything in it when closed; it is resumed only once it is in the job, so
it cannot spawn a child that escapes. Elsewhere the program leads a new
process group, and the group is killed.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """What a finished or killed program did.

    Attributes:
        argv: The command line that was run.
        pid: The process id the program ran under.
        returncode: The program's exit code, or ``None`` if it was killed
            on timeout.
        stdout: Everything it wrote to standard output, decoded as UTF-8
            with undecodable bytes replaced.
        stderr: The same for standard error.
        timed_out: True if Kairo killed it because it ran past its timeout.
        duration_s: Wall-clock time from start to exit or kill, in seconds.
    """

    argv: tuple[str, ...]
    pid: int
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float


class ProcessError(RuntimeError):
    """Kairo could not start a program in a way that lets it clean up after it."""


def run_process(
    argv: Sequence[str],
    *,
    timeout_s: float,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> ProcessResult:
    """Run a program to completion or until its timeout, then kill its whole tree.

    Output goes to temporary files rather than pipes, so a program that
    writes a lot cannot block on a full pipe; the files are deleted when
    this returns.

    Args:
        argv: The program and its arguments.
        timeout_s: Seconds to wait before killing the program and everything
            it started.
        cwd: Folder to run in; the current folder if ``None``.
        env: Environment for the program; Kairo's own if ``None``.

    Returns:
        The exit code (``None`` after a kill), the output, and the time
        taken.

    Raises:
        FileNotFoundError: If the program does not exist.
        ProcessError: If the program could not be placed under Kairo's
            control (Windows Job Object). It is killed before this is raised.
        ValueError: If ``timeout_s`` is not positive.
    """
    if timeout_s <= 0:
        raise ValueError(f"timeout_s must be positive, got {timeout_s}")
    args = tuple(argv)
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        start = time.monotonic()
        if sys.platform == "win32":
            pid, returncode, timed_out = _run_windows(
                args, timeout_s, cwd, env, out, err
            )
        else:
            pid, returncode, timed_out = _run_posix(args, timeout_s, cwd, env, out, err)
        duration_s = time.monotonic() - start
        out.seek(0)
        err.seek(0)
        stdout = out.read().decode("utf-8", errors="replace")
        stderr = err.read().decode("utf-8", errors="replace")
    return ProcessResult(args, pid, returncode, stdout, stderr, timed_out, duration_s)


def process_alive(pid: int) -> bool:
    """Say whether a process with this id is still running.

    Args:
        pid: The process id.

    Returns:
        True if the process exists and has not exited.
    """
    if sys.platform == "win32":
        return _windows_process_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists but belongs to someone else.
        return True
    return True


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")

    _CREATE_SUSPENDED = 0x00000004
    _CREATE_NO_WINDOW = 0x08000000
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_SUSPEND_RESUME = 0x0800
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.restype = ctypes.c_long

    def _new_kill_on_close_job() -> int:
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ProcessError(f"CreateJobObjectW failed: {ctypes.get_last_error()}")
        info = _ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _kernel32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            error = ctypes.get_last_error()
            _kernel32.CloseHandle(job)
            raise ProcessError(f"SetInformationJobObject failed: {error}")
        return int(job)

    def _run_windows(
        argv: tuple[str, ...],
        timeout_s: float,
        cwd: Path | None,
        env: Mapping[str, str] | None,
        out: IO[bytes],
        err: IO[bytes],
    ) -> tuple[int, int | None, bool]:
        job = _new_kill_on_close_job()
        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=None if env is None else dict(env),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                creationflags=_CREATE_SUSPENDED | _CREATE_NO_WINDOW,
            )
            # The process cannot have started any children while suspended,
            # so once it is in the job, everything it ever starts is too.
            handle = _kernel32.OpenProcess(
                _PROCESS_TERMINATE | _PROCESS_SET_QUOTA | _PROCESS_SUSPEND_RESUME,
                False,
                proc.pid,
            )
            if not handle:
                proc.kill()
                proc.wait()
                raise ProcessError(f"OpenProcess failed: {ctypes.get_last_error()}")
            try:
                if not _kernel32.AssignProcessToJobObject(job, handle):
                    error = ctypes.get_last_error()
                    proc.kill()
                    proc.wait()
                    raise ProcessError(f"AssignProcessToJobObject failed: {error}")
                status = _ntdll.NtResumeProcess(handle)
                if status != 0:
                    _kernel32.TerminateJobObject(job, 1)
                    proc.wait()
                    raise ProcessError(f"NtResumeProcess failed: {status:#x}")
            finally:
                _kernel32.CloseHandle(handle)
            try:
                return proc.pid, proc.wait(timeout=timeout_s), False
            except subprocess.TimeoutExpired:
                _kernel32.TerminateJobObject(job, 1)
                proc.wait()
                return proc.pid, None, True
        finally:
            # Sweep anything the program left running, then release the job.
            _kernel32.TerminateJobObject(job, 1)
            _kernel32.CloseHandle(job)

    def _windows_process_alive(pid: int) -> bool:
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            _kernel32.CloseHandle(handle)

else:
    import signal

    def _run_posix(
        argv: tuple[str, ...],
        timeout_s: float,
        cwd: Path | None,
        env: Mapping[str, str] | None,
        out: IO[bytes],
        err: IO[bytes],
    ) -> tuple[int, int | None, bool]:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=None if env is None else dict(env),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
        )
        try:
            return proc.pid, proc.wait(timeout=timeout_s), False
        except subprocess.TimeoutExpired:
            _kill_group(proc.pid)
            proc.wait()
            return proc.pid, None, True
        finally:
            _kill_group(proc.pid)

    def _kill_group(pgid: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)
