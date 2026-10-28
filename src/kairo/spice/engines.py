"""Find and run the simulators: ngspice and Xyce, as subprocesses only.

Each run happens in a fresh temporary folder, in batch mode, under a
timeout, and returns the results read by Kairo's own raw-file reader with
the settings that produced them. A run that fails returns the simulator's
own words; it does not raise through the caller as a crash.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from kairo.process import ProcessError, run_process
from kairo.spice.circuit import Engine
from kairo.spice.raw import Plot, RawError, read_raw
from kairo.spice.request import Analysis, Stimulus

#: The environment variable naming each simulator's executable.
ENV_VARS: dict[Engine, str] = {"ngspice": "KAIRO_NGSPICE", "xyce": "KAIRO_XYCE"}
#: The names each simulator's executable goes by on ``PATH``.
_NAMES: dict[Engine, tuple[str, ...]] = {
    "ngspice": ("ngspice_con", "ngspice"),
    "xyce": ("Xyce", "xyce"),
}
#: The versions whose results on the accuracy set are recorded in
#: ``evals/accuracy``. Another version still runs, marked not validated.
VALIDATED: dict[Engine, tuple[str, ...]] = {"ngspice": ("47",), "xyce": ("7.10.0",)}
#: Seconds one analysis may take before it is killed.
TIMEOUT_S = 120.0
#: The temperature every run is made at, in degrees Celsius: SPICE's own
#: nominal, so models with no temperature data are at their stated values.
TEMPERATURE = 27.0
#: Steps in a DC sweep.
DC_POINTS = 200
#: Points per decade of an AC sweep.
POINTS_PER_DECADE = 50
#: The least number of time steps in one period of a sine source. A
#: sample at most half a step from a peak reads it low by about
#: (pi / N)^2 / 2 of the amplitude: 0.012% at 200, where 10 points a
#: period, which a long run gave, read a filter's ripple 4% low.
POINTS_PER_PERIOD = 200
#: How much looser Xyce's Newton current tolerance is, in a transient
#: with a PSpice model, than the profile's. Measured on TI's LM1117
#: model with time steps chosen by iteration count: at the profile's
#: 1e-13 A the run did not finish in two minutes, at 1e-12 A it took
#: 57,186 steps, and from 1e-11 A to 1e-6 A it took 2,005 steps with
#: the same output to the last printed digit.
PSPICE_NEWTON_LOOSER = 1000.0
#: How a run that used those settings names them among its aids.
STEPS_BY_ITERATIONS = (
    "time steps chosen by iteration count, not error estimate, and a "
    f"Newton current tolerance {PSPICE_NEWTON_LOOSER:g} times the profile's"
)


@dataclass(frozen=True, slots=True)
class Profile:
    """The numerical settings of a run.

    Attributes:
        name: What the profile is called in results.
        reltol: The relative error tolerance.
        abstol: The absolute current tolerance, in A.
        vntol: The absolute voltage tolerance, in V.
        chgtol: The absolute charge tolerance, in C.
        method: The integration method, ``trap`` or ``gear``.
        steps: The least number of time steps in a transient run; the
            largest step allowed is the duration divided by this.
    """

    name: str
    reltol: float
    abstol: float
    vntol: float
    chgtol: float
    method: str
    steps: int

    def as_dict(self) -> dict[str, object]:
        """The settings as plain data, for the record of a run."""
        return {
            "profile": self.name,
            "reltol": self.reltol,
            "abstol": self.abstol,
            "vntol": self.vntol,
            "chgtol": self.chgtol,
            "method": self.method,
            "least_steps": self.steps,
            "nominal_temperature_degC": TEMPERATURE,
        }


#: The profile every result is computed with. Its values were chosen from
#: the accuracy set's measurements, recorded in ``evals/accuracy/results``:
#: the loosest of the settings tried at which every circuit of the set is
#: inside its error budget on both simulators.
ACCURATE = Profile(
    "accurate",
    reltol=1e-4,
    abstol=1e-13,
    vntol=1e-7,
    chgtol=1e-15,
    method="trap",
    steps=2000,
)


def tighter(profile: Profile) -> Profile:
    """The profile a result is checked against: ten times tighter.

    Args:
        profile: The profile a result was computed with.

    Returns:
        The same with every tolerance divided by ten and the largest time
        step halved.
    """
    return replace(
        profile,
        name=f"{profile.name}, tightened",
        reltol=profile.reltol / 10,
        abstol=profile.abstol / 10,
        vntol=profile.vntol / 10,
        chgtol=profile.chgtol / 10,
        steps=profile.steps * 2,
    )


@dataclass(frozen=True, slots=True)
class Simulator:
    """An installed simulator.

    Attributes:
        engine: Which one.
        path: Its executable.
        version: The version it reports, or ``unknown``.
    """

    engine: Engine
    path: Path
    version: str

    @property
    def validated(self) -> bool:
        """Whether this version's accuracy-set results are on record."""
        return self.version in VALIDATED[self.engine]


def find_simulator(
    engine: Engine,
    explicit: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[Simulator | None, tuple[str, ...]]:
    """Find one simulator on this machine.

    The search order is: ``explicit``; the engine's environment variable
    (`ENV_VARS`); its usual names on ``PATH``. A path given explicitly or
    by the variable is final, so a mistyped setting never quietly runs
    another install.

    Args:
        engine: Which simulator.
        explicit: A path the caller chose.
        environ: The environment to read; the process's if ``None``.

    Returns:
        The simulator, or ``None``, and the places searched.
    """
    env = os.environ if environ is None else environ
    searched: list[str] = []
    path: Path | None = None
    if explicit is not None:
        searched.append(f"given path {explicit}")
        path = explicit if explicit.is_file() else None
    elif env.get(ENV_VARS[engine]):
        candidate = Path(env[ENV_VARS[engine]])
        searched.append(f"{ENV_VARS[engine]}={candidate}")
        path = candidate if candidate.is_file() else None
    else:
        searched.append("PATH")
        for name in _NAMES[engine]:
            found = shutil.which(name, path=env.get("PATH"))
            if found is not None:
                path = Path(found)
                break
    if path is None:
        return None, tuple(searched)
    return Simulator(engine, path, _version(engine, path)), tuple(searched)


_VERSIONS: dict[Engine, re.Pattern[str]] = {
    "ngspice": re.compile(r"ngspice-(\d+(?:\.\d+)*)"),
    "xyce": re.compile(r"Release (\d+(?:\.\d+)*)"),
}


def _version(engine: Engine, path: Path) -> str:
    """Ask a simulator its version."""
    flag = "--version" if engine == "ngspice" else "-v"
    try:
        result = run_process([str(path), flag], timeout_s=30.0)
    except (OSError, ProcessError):
        return "unknown"
    found = _VERSIONS[engine].search(result.stdout + result.stderr)
    return found[1] if found is not None else "unknown"


def commands(
    engine: Engine,
    analysis: Analysis,
    profile: Profile,
    source: str | None = None,
    stimuli: Sequence[Stimulus] = (),
    *,
    pspice: bool = False,
) -> list[str]:
    """The option and analysis lines of a netlist, in one simulator's dialect.

    Args:
        engine: Whose dialect to write.
        analysis: The analysis to run.
        profile: The numerical settings.
        source: For a ``dc`` analysis, the source element to sweep.
        stimuli: The run's sources; a sine among them bounds the time
            step by `POINTS_PER_PERIOD`.
        pspice: Whether the circuit holds a model written for PSpice. Xyce
            then chooses its time steps by how many iterations each
            takes, not by its error estimate; see the comment below.

    Returns:
        The lines, in order.

    Raises:
        ValueError: If a ``dc`` analysis is given no source.
    """
    temperature = (
        analysis.temperature.value if analysis.temperature is not None else TEMPERATURE
    )
    if engine == "ngspice":
        lines = [
            f".options reltol={profile.reltol!r} abstol={profile.abstol!r} "
            f"vntol={profile.vntol!r} chgtol={profile.chgtol!r} "
            f"method={profile.method} temp={temperature!r} tnom={TEMPERATURE!r}"
        ]
    else:
        # Xyce splits what ngspice calls reltol between its time integrator
        # and its nonlinear solver, so the one value is given to each, and
        # the voltage tolerance is the integrator's absolute tolerance.
        lines = [
            f".options nonlin reltol={profile.reltol!r} abstol={profile.abstol!r}",
            f".options device temp={temperature!r} tnom={TEMPERATURE!r}",
        ]
        if analysis.kind == "tran" and pspice:
            # PSpice models switch with ideal steps (IF in a behavioural
            # source), which ngspice and PSpice step across. Xyce's error
            # estimate covers every unknown, including the voltages a
            # step jumps, so it shrinks its step until it gives up
            # (measured on TI's LM1117 model, at its under-voltage
            # comparator). ERROPTION=1 chooses steps by iteration count;
            # the largest step is still the profile's. Its Newton
            # tolerance is loosened by PSPICE_NEWTON_LOOSER.
            lines += [
                f".options timeint method={profile.method} erroption=1",
                f".options nonlin-tran reltol={profile.reltol!r} "
                f"abstol={profile.abstol * PSPICE_NEWTON_LOOSER!r}",
            ]
        elif analysis.kind == "tran":
            # Xyce refuses time-integration options on any other analysis.
            lines += [
                f".options timeint method={profile.method} "
                f"reltol={profile.reltol!r} abstol={profile.vntol!r}",
                f".options nonlin-tran reltol={profile.reltol!r} "
                f"abstol={profile.abstol!r}",
            ]
    if analysis.kind == "op":
        lines.append(".op")
    elif analysis.kind == "tran":
        assert analysis.duration is not None
        stop = analysis.duration.value
        step = min(
            [
                stop / profile.steps,
                *(
                    1 / (s.frequency.value * POINTS_PER_PERIOD)
                    for s in stimuli
                    if s.kind == "sine" and s.frequency is not None
                ),
            ]
        )
        lines.append(f".tran {step!r} {stop!r} 0 {step!r}")
    elif analysis.kind == "ac":
        assert analysis.f_start is not None and analysis.f_stop is not None
        lines.append(
            f".ac dec {POINTS_PER_DECADE} {analysis.f_start.value!r} "
            f"{analysis.f_stop.value!r}"
        )
    else:
        assert analysis.v_start is not None and analysis.v_stop is not None
        if source is None:
            raise ValueError("a dc analysis needs the source element to sweep")
        start, stop = analysis.v_start.value, analysis.v_stop.value
        lines.append(f".dc {source} {start!r} {stop!r} {(stop - start) / DC_POINTS!r}")
    return lines


@dataclass(frozen=True, slots=True)
class Run:
    """What one run of a simulator gave.

    Attributes:
        simulator: Which simulator ran.
        profile: The settings it ran with.
        plot: The results, or ``None`` if the run failed.
        error: Why it failed, in the simulator's words; empty on success.
        aids: Convergence aids the simulator said it used.
        duration_s: How long the run took, in seconds.
    """

    simulator: Simulator
    profile: Profile
    plot: Plot | None
    error: str
    aids: tuple[str, ...]
    duration_s: float

    def as_dict(self) -> dict[str, object]:
        """The record of the run, as the accuracy block wants it."""
        return {
            "name": self.simulator.engine,
            "version": self.simulator.version,
            "validated": self.simulator.validated,
            "settings": self.profile.as_dict(),
            "convergence_aids": list(self.aids),
        }


_AIDS = re.compile(r"(gmin stepping|source stepping)", re.IGNORECASE)
_ERRORS = re.compile(
    r"^.*(?:error|singular matrix|time ?step too small|abort|fatal).*$",
    re.IGNORECASE | re.MULTILINE,
)


def run_netlist(
    simulator: Simulator,
    text: str,
    profile: Profile,
    *,
    timeout_s: float = TIMEOUT_S,
    pspice: bool = False,
) -> Run:
    """Run one netlist in a fresh folder and read its results.

    Args:
        simulator: The simulator to run.
        text: The netlist, in that simulator's dialect.
        profile: The settings the netlist was written with, for the record.
        timeout_s: Seconds before the run is killed.
        pspice: Whether the netlist reads a model written for PSpice.
            ngspice is then run in its PSpice compatibility mode
            (``ngbehavior=psa``).

    Returns:
        The results, or the reason there are none.
    """
    folder = Path(tempfile.mkdtemp(prefix="kairo-sim-"))
    try:
        (folder / "deck.cir").write_text(text, encoding="utf-8", newline="\n")
        if simulator.engine == "ngspice":
            # ngspice reads .spiceinit from the folder it runs in. This one
            # fixes the raw format, which an install's own settings can
            # change, and keeps the user's home settings out of the run.
            setup = "set filetype=binary\n"
            if pspice:
                setup += "set ngbehavior=psa\n"
            (folder / ".spiceinit").write_text(setup, encoding="utf-8", newline="\n")
            argv = [str(simulator.path), "-b", "-r", "out.raw", "deck.cir"]
        else:
            argv = [str(simulator.path), "-r", "out.raw", "deck.cir"]
        try:
            result = run_process(argv, timeout_s=timeout_s, cwd=folder)
        except (OSError, ProcessError) as exc:
            return Run(simulator, profile, None, f"could not run: {exc}", (), 0.0)
        log = result.stdout + result.stderr
        aids = tuple(sorted({m.lower() for m in _AIDS.findall(log)}))
        if simulator.engine == "xyce" and "erroption=1" in text:
            aids += (STEPS_BY_ITERATIONS,)
        if result.timed_out:
            return Run(
                simulator,
                profile,
                None,
                f"killed after {timeout_s:g} s without finishing",
                aids,
                result.duration_s,
            )
        raw = folder / "out.raw"
        complaint = "; ".join(m.strip() for m in _ERRORS.findall(log)[:4])
        if not complaint:
            # Nothing reads as an error, so the last lines are the best clue.
            complaint = "; ".join(
                line.strip() for line in log.splitlines()[-6:] if line.strip()
            )
        if result.returncode != 0 or not raw.is_file():
            return Run(
                simulator,
                profile,
                None,
                complaint or f"exit code {result.returncode} and no results",
                aids,
                result.duration_s,
            )
        try:
            plot = read_raw(raw.read_bytes())[0]
        except RawError as exc:
            return Run(
                simulator,
                profile,
                None,
                f"{exc}; {complaint}" if complaint else str(exc),
                aids,
                result.duration_s,
            )
        return Run(simulator, profile, plot, "", aids, result.duration_s)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
