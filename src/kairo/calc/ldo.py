"""Linear regulator dissipation, junction temperature, efficiency and headroom."""

from __future__ import annotations

from kairo.calc.result import CalcResult, Given, Missing, accuracy_of
from kairo.units import Quantity

_POWER_SOURCE = (
    "conservation of power: a linear regulator passes its load current from "
    "input to output and draws its quiescent current from the input"
)
_THETA_SOURCE = (
    "definition of the junction-to-ambient thermal resistance, "
    "theta_JA = (T_J - T_A) / P_D (JEDEC JESD51-2A)"
)
_THETA_WARNING = (
    "theta_JA is measured on the JEDEC test board in still air; the real "
    "board's copper changes it, so this is not the temperature on this board"
)


def ldo_thermal(
    v_in: Given,
    v_out: Given,
    i_out: Given,
    *,
    theta_ja: Given | None = None,
    t_ambient: Given | None = None,
    i_q: Given | None = None,
    v_dropout: Given | None = None,
    ref: str | None = None,
) -> dict[str, CalcResult]:
    """Compute a linear regulator's dissipation and what follows from it.

    The equations are::

        P_D = (V_in - V_out) * I_out + V_in * I_q
        T_J = T_A + P_D * theta_JA
        efficiency = V_out * I_out / (V_in * (I_out + I_q))
        headroom = V_in - V_out - V_dropout

    With no quiescent current given, the term ``V_in * I_q`` is left out.
    It cannot be negative, so the dissipation and junction temperature
    returned are proven lower bounds and the efficiency a proven upper
    bound, each marked as a bound. Nothing is put in its place.

    Sources: conservation of power for the dissipation and efficiency;
    the definition of theta_JA in JEDEC JESD51-2A for the junction
    temperature.

    Args:
        v_in: Input voltage, in V.
        v_out: Output voltage, in V.
        i_out: Load current, in A.
        theta_ja: Junction-to-ambient thermal resistance, in degC/W.
        t_ambient: Ambient temperature, in degC.
        i_q: Quiescent (ground pin) current, in A.
        v_dropout: Dropout voltage at this load, in V.
        ref: The regulator's reference, for naming what is missing.

    Returns:
        ``ldo_dissipation``, ``ldo_junction_temperature``,
        ``ldo_efficiency`` and ``ldo_dropout_headroom``, each with its
        inputs, what was missing, and its accuracy block.

    Raises:
        ValueError: If an input has the wrong unit, or the values are not
            those of a working regulator (output above input, negative
            current).
    """
    _expect(v_in, "V")
    _expect(v_out, "V")
    _expect(i_out, "A")
    for given, unit in ((theta_ja, "degC/W"), (t_ambient, "degC"), (i_q, "A")):
        if given is not None:
            _expect(given, unit)
    if v_dropout is not None:
        _expect(v_dropout, "V")
    if v_out.number > v_in.number:
        raise ValueError("the output voltage is above the input voltage")
    if i_out.number < 0 or (i_q is not None and i_q.number < 0):
        raise ValueError("a current is negative")

    no_iq = Missing(
        "Iq",
        ref,
        "the omitted term Vin*Iq is not negative, so the value is a limit, "
        "not the value itself",
        f"Give {ref or 'the regulator'}'s quiescent current (i_q) from its "
        "datasheet, with the page",
    )
    drop = v_in.number - v_out.number
    power = drop * i_out.number + (v_in.number * i_q.number if i_q else 0.0)
    power_inputs = (v_in, v_out, i_out) + ((i_q,) if i_q else ())
    power_missing = () if i_q else (no_iq,)
    results: dict[str, CalcResult] = {}
    results["ldo_dissipation"] = CalcResult(
        name="ldo_dissipation",
        status="ok" if i_q else "bound",
        bound=None if i_q else ">=",
        value=Quantity(power, "W"),
        equation="P = (Vin - Vout) * Iout + Vin * Iq",
        inputs=power_inputs,
        missing=power_missing,
        source=_POWER_SOURCE,
        accuracy=accuracy_of(power_inputs, power_missing),
    )

    thermal_missing = list(power_missing)
    if theta_ja is None:
        thermal_missing.append(
            Missing(
                "theta_JA",
                ref,
                "the junction temperature cannot be computed",
                f"Give {ref or 'the regulator'}'s theta_ja from its datasheet, "
                "with the page",
            )
        )
    if t_ambient is None:
        thermal_missing.append(
            Missing(
                "TA",
                None,
                "the junction temperature cannot be computed",
                "State the ambient temperature",
            )
        )
    if theta_ja is None or t_ambient is None:
        present = tuple(g for g in (*power_inputs, theta_ja, t_ambient) if g)
        results["ldo_junction_temperature"] = CalcResult(
            name="ldo_junction_temperature",
            status="unverified",
            value=None,
            equation="TJ = TA + P * theta_JA",
            inputs=present,
            missing=tuple(thermal_missing),
            source=_THETA_SOURCE,
            warnings=(_THETA_WARNING,),
            accuracy=accuracy_of(present, tuple(thermal_missing)),
        )
    else:
        thermal_inputs = (*power_inputs, theta_ja, t_ambient)
        results["ldo_junction_temperature"] = CalcResult(
            name="ldo_junction_temperature",
            status="ok" if i_q else "bound",
            bound=None if i_q else ">=",
            value=Quantity(t_ambient.number + power * theta_ja.number, "degC"),
            equation="TJ = TA + P * theta_JA",
            inputs=thermal_inputs,
            missing=tuple(thermal_missing),
            source=_THETA_SOURCE,
            warnings=(_THETA_WARNING,),
            accuracy=accuracy_of(thermal_inputs, tuple(thermal_missing)),
        )

    if v_in.number > 0 and (i_out.number > 0 or (i_q and i_q.number > 0)):
        total = i_out.number + (i_q.number if i_q else 0.0)
        efficiency = 100.0 * v_out.number * i_out.number / (v_in.number * total)
        results["ldo_efficiency"] = CalcResult(
            name="ldo_efficiency",
            status="ok" if i_q else "bound",
            bound=None if i_q else "<=",
            value=Quantity(efficiency, "%"),
            equation="efficiency = Vout * Iout / (Vin * (Iout + Iq))",
            inputs=power_inputs,
            missing=power_missing,
            source=_POWER_SOURCE,
            accuracy=accuracy_of(power_inputs, power_missing),
        )

    if v_dropout is None:
        no_dropout = (
            Missing(
                "Vdropout",
                ref,
                "the dropout headroom cannot be computed",
                f"Give {ref or 'the regulator'}'s dropout voltage (v_dropout) at "
                "this load from its datasheet, with the page",
            ),
        )
        results["ldo_dropout_headroom"] = CalcResult(
            name="ldo_dropout_headroom",
            status="unverified",
            value=None,
            equation="headroom = Vin - Vout - Vdropout",
            inputs=(v_in, v_out),
            missing=no_dropout,
            source="Kirchhoff's voltage law across the regulator",
            accuracy=accuracy_of((v_in, v_out), no_dropout),
        )
    else:
        headroom_inputs = (v_in, v_out, v_dropout)
        results["ldo_dropout_headroom"] = CalcResult(
            name="ldo_dropout_headroom",
            status="ok",
            value=Quantity(drop - v_dropout.number, "V"),
            equation="headroom = Vin - Vout - Vdropout",
            inputs=headroom_inputs,
            source="Kirchhoff's voltage law across the regulator",
            accuracy=accuracy_of(headroom_inputs, ()),
        )
    return results


def _expect(given: Given, unit: str) -> None:
    if given.value.unit != unit:
        raise ValueError(
            f"{given.name} is in {given.value.unit or 'no unit'}; expected {unit}"
        )
