"""The raw-file reader on files both simulators wrote, and the measurements."""

from __future__ import annotations

import math
import struct
from pathlib import Path

import pytest

from kairo.calc.second_order import (
    damping_from_overshoot,
    overshoot_from_damping,
    phase_margin_from_damping,
)
from kairo.spice import measure
from kairo.spice.measure import MeasureError, Waveform
from kairo.spice.plot import engineering, line_chart, thin
from kairo.spice.raw import Plot, RawError, read_raw

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "spice"
ENGINES = ("ngspice47", "xyce710")


def plot_of(name: str) -> Plot:
    return read_raw((FIXTURES / f"{name}.raw").read_bytes())[0]


@pytest.mark.parametrize("engine", ENGINES)
def test_an_operating_point_reads_the_same_from_both_simulators(engine: str) -> None:
    # 10 V across 1k and 3k: the middle node is exactly 7.5 V.
    plot = plot_of(f"{engine}_op_binary")
    assert not plot.is_complex
    assert plot.real("v(a)")[-1] == pytest.approx(10.0, abs=1e-12)
    assert plot.real("v(b)")[-1] == pytest.approx(7.5, abs=1e-12)
    assert plot.real("i(v1)")[-1] == pytest.approx(-2.5e-3, abs=1e-15)
    assert plot.kinds["i(v1)"] == "current"


@pytest.mark.parametrize("engine", ENGINES)
def test_text_and_binary_files_hold_the_same_curve(engine: str) -> None:
    binary = plot_of(f"{engine}_tran_binary")
    text = plot_of(f"{engine}_tran_ascii")
    assert binary.variables == text.variables
    assert binary.variables[0] == "time"
    assert len(binary.real("time")) == len(text.real("time")) > 5
    # Xyce's text files carry nine figures, so that is how closely they agree.
    for variable in binary.variables:
        for a, b in zip(binary.real(variable), text.real(variable), strict=True):
            assert a == pytest.approx(b, rel=1e-8, abs=1e-12)
    # An RC of 1 ms charging to 1 V has reached 1 - exp(-3) by 3 ms. The
    # fixture was run with seven coarse steps, hence the loose bound.
    assert binary.real("v(out)")[-1] == pytest.approx(1 - math.exp(-3), abs=5e-3)


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("form", ["binary", "ascii"])
def test_a_frequency_sweep_is_complex(engine: str, form: str) -> None:
    plot = plot_of(f"{engine}_ac_{form}")
    assert plot.is_complex
    frequency = plot.real("frequency")
    assert frequency[0] == pytest.approx(100.0) and len(frequency) == 5
    for f, value in zip(frequency, plot.values["v(out)"], strict=True):
        exact = 1 / (1 + 2j * math.pi * f * 1000 * 1e-7)
        assert value == pytest.approx(exact, rel=1e-7)


def test_a_file_cut_short_is_refused_not_half_read() -> None:
    whole = (FIXTURES / "ngspice47_tran_binary.raw").read_bytes()
    with pytest.raises(RawError, match="cut short"):
        read_raw(whole[:-12])
    with pytest.raises(RawError, match="header"):
        read_raw(whole[:40])
    with pytest.raises(RawError, match="no results"):
        read_raw(b"")
    text = (FIXTURES / "ngspice47_tran_ascii.raw").read_bytes()
    with pytest.raises(RawError, match=r"cut short|promises"):
        read_raw(text[: text.rindex(b"\n", 0, len(text) - 40)])


def test_two_plots_in_one_file_are_both_read() -> None:
    # A raw file can hold several analyses one after another.
    def block(name: str, values: tuple[float, ...]) -> bytes:
        head = (
            f"Title: t\nPlotname: {name}\nFlags: real\nNo. Variables: 2\n"
            f"No. Points: {len(values) // 2}\nVariables:\n\t0\ttime\ttime\n"
            "\t1\tOUT\tvoltage\nBinary:\n"
        )
        return head.encode() + struct.pack(f"={len(values)}d", *values)

    plots = read_raw(block("first", (0.0, 1.0, 1.0, 2.0)) + block("second", (0.0, 5.0)))
    assert [p.name for p in plots] == ["first", "second"]
    assert plots[0].real("v(out)") == (1.0, 2.0)
    assert plots[1].real("v(out)") == (5.0,)


def rc(tau: float = 1e-3, points: int = 4001, span: float = 10e-3) -> Waveform:
    t = tuple(span * i / (points - 1) for i in range(points))
    return Waveform(t, tuple(1 - math.exp(-x / tau) for x in t))


def ringing(zeta: float, points: int = 20001) -> Waveform:
    omega = 2 * math.pi * 1e3
    damped = omega * math.sqrt(1 - zeta * zeta)
    span = 12 / (zeta * omega)
    t = tuple(span * i / (points - 1) for i in range(points))
    return Waveform(
        t,
        tuple(
            1
            - math.exp(-zeta * omega * x)
            * (
                math.cos(damped * x)
                + zeta / math.sqrt(1 - zeta * zeta) * math.sin(damped * x)
            )
            for x in t
        ),
    )


def test_an_rc_charge_has_the_textbook_rise_and_settling_times() -> None:
    wave = rc()
    # 10% to 90% takes tau * ln(9); within 1% takes tau * ln(100).
    assert measure.rise_time(wave, 0.0, 1.0) == pytest.approx(
        1e-3 * math.log(9), rel=1e-5
    )
    assert measure.settling_time(wave, 1.0, 0.01) == pytest.approx(
        1e-3 * math.log(100), rel=1e-5
    )
    assert measure.overshoot(wave, 0.0, 1.0) == 0.0
    assert measure.crossing_time(wave, 0.5) == pytest.approx(
        1e-3 * math.log(2), rel=1e-5
    )
    assert wave.at(1e-3) == pytest.approx(1 - math.exp(-1), abs=1e-7)
    assert measure.has_settled(wave, 0.01)
    # The same curve stopped after two time constants has not settled.
    short = Waveform(wave.t[:801], wave.y[:801])
    assert not measure.has_settled(short, 0.01 * short.last)


@pytest.mark.parametrize("zeta", [0.2, 0.42, 0.6])
def test_a_ringing_step_has_the_textbook_overshoot(zeta: float) -> None:
    wave = ringing(zeta)
    exact = math.exp(-math.pi * zeta / math.sqrt(1 - zeta * zeta))
    assert measure.overshoot(wave, 0.0, 1.0) == pytest.approx(exact, abs=2e-5)
    assert measure.time_of_peak(wave) == pytest.approx(
        math.pi / (2 * math.pi * 1e3 * math.sqrt(1 - zeta * zeta)), rel=2e-3
    )
    # It leaves a 2% band for the last time at one of its later swings.
    settle = measure.settling_time(wave, 1.0, 0.02)
    assert abs(wave.at(settle) - 1.0) == pytest.approx(0.02, abs=1e-6)
    assert all(
        abs(y - 1.0) <= 0.02 + 1e-12
        for t, y in zip(wave.t, wave.y, strict=True)
        if t > settle
    )


def test_a_falling_step_is_measured_like_a_rising_one() -> None:
    up = ringing(0.3)
    down = Waveform(up.t, tuple(1 - y for y in up.y))
    assert measure.overshoot(down, 1.0, 0.0) == pytest.approx(
        measure.overshoot(up, 0.0, 1.0), rel=1e-12
    )
    assert measure.rise_time(down, 1.0, 0.0) == pytest.approx(
        measure.rise_time(up, 0.0, 1.0), rel=1e-9
    )


def test_what_cannot_be_measured_is_refused() -> None:
    wave = rc()
    with pytest.raises(MeasureError, match="never crosses"):
        measure.crossing_time(wave, 2.0)
    with pytest.raises(MeasureError, match="no step"):
        measure.overshoot(wave, 1.0, 1.0)
    with pytest.raises(MeasureError, match="ends outside"):
        measure.settling_time(wave, 2.0, 0.01)
    with pytest.raises(MeasureError, match="outside the record"):
        wave.at(1.0)
    with pytest.raises(MeasureError, match="two or more"):
        Waveform((0.0,), (1.0,))
    with pytest.raises(MeasureError, match="must not decrease"):
        Waveform((0.0, 2.0, 1.0), (0.0, 0.0, 0.0))


def test_area_spread_gain_and_cutoff() -> None:
    ramp = Waveform((0.0, 1.0, 3.0), (0.0, 2.0, 2.0))
    assert measure.integral(ramp) == pytest.approx(1.0 + 4.0)
    assert measure.peak_to_peak(ramp) == 2.0
    assert measure.peak_to_peak(ramp, after=1.0) == 0.0
    assert measure.gain_db(0.5, 1) == pytest.approx(-6.0206, abs=1e-4)
    assert measure.phase_degrees(-1j, 1) == pytest.approx(-90.0)
    frequency = tuple(10 ** (1 + i / 50) for i in range(251))
    gains = tuple(measure.gain_db(1 / (1 + 1j * f / 1591.549), 1) for f in frequency)
    cutoff = measure.frequency_at_gain(frequency, gains, 10 * math.log10(0.5))
    assert cutoff == pytest.approx(1591.549, rel=1e-4)
    with pytest.raises(MeasureError, match="never crosses"):
        measure.frequency_at_gain(frequency, gains, -200.0)


def test_overshoot_damping_and_phase_margin_agree_with_the_textbook() -> None:
    # Franklin, Powell and Emami-Naeini: 16% overshoot at a damping ratio
    # of 0.5 and 5% at 0.7; phase margin is about 100 times the damping
    # ratio up to 0.6, and exactly 51.8 and 59.2 degrees at 0.5 and 0.6.
    assert overshoot_from_damping(0.5) == pytest.approx(0.163, abs=5e-4)
    assert overshoot_from_damping(0.7) == pytest.approx(0.046, abs=5e-4)
    assert phase_margin_from_damping(0.5) == pytest.approx(51.8, abs=0.05)
    assert phase_margin_from_damping(0.6) == pytest.approx(59.2, abs=0.05)
    for zeta in (0.1, 0.3, 0.5, 0.8):
        assert damping_from_overshoot(overshoot_from_damping(zeta)) == pytest.approx(
            zeta
        )
    with pytest.raises(ValueError, match="above 0"):
        damping_from_overshoot(0.0)
    with pytest.raises(ValueError, match="below 1"):
        overshoot_from_damping(1.0)
    with pytest.raises(ValueError, match="above 0"):
        phase_margin_from_damping(0.0)


def test_a_chart_keeps_a_narrow_spike_and_is_well_formed() -> None:
    from xml.etree import ElementTree

    x = tuple(float(i) for i in range(100_000))
    y = tuple(50.0 if i == 43_210 else math.sin(i / 5000) for i in range(100_000))
    thinned = thin((x, y))
    assert len(thinned[0]) <= 1300 and max(thinned[1]) == 50.0
    assert thinned[0][0] == 0.0 and thinned[0][-1] == 99_999.0
    svg = line_chart(
        {"a <net>": (x, y), "flat": ((0.0, 1.0), (2.0, 2.0))},
        title="spike & co",
        x_unit="s",
        y_unit="V",
    )
    root = ElementTree.fromstring(svg)
    assert root.tag.endswith("svg")
    assert sum(1 for e in root.iter() if e.tag.endswith("polyline")) == 2
    assert "a &lt;net&gt;" in svg and "spike &amp; co" in svg
    log = line_chart(
        {"gain": ((10.0, 100.0, 1000.0), (0.0, -3.0, -20.0))},
        title="t",
        x_unit="Hz",
        y_unit="dB",
        log_x=True,
    )
    assert "1 kHz" in log
    with pytest.raises(ValueError, match="above zero"):
        line_chart(
            {"g": ((0.0, 1.0), (0.0, 1.0))},
            title="t",
            x_unit="Hz",
            y_unit="dB",
            log_x=True,
        )
    with pytest.raises(ValueError, match="at least one"):
        line_chart({}, title="t", x_unit="s", y_unit="V")
    assert engineering(2.2e-4, "s") == "220 us"
    assert (
        engineering(1591.5, "Hz") == "1.591 kHz"
        or engineering(1591.5, "Hz") == "1.592 kHz"
    )
    assert engineering(0.0, "V") == "0 V"
