"""What a ceramic capacitor really delivers, and whether a regulator is stable on it."""

from __future__ import annotations

from typing import Literal

from kairo.calc.result import Accuracy, CalcResult, Given, Missing
from kairo.parts.store import BiasCurve, PartsError
from kairo.units import Quantity

#: The largest loss of capacitance over the rated temperature range, as a
#: fraction, for the class II temperature characteristic codes of EIA-198:
#: the third character gives the change (P 10%, R 15%, S 22%, T +22/-33%,
#: U +22/-56%, V +22/-82%). Only the loss matters for a minimum.
TEMPERATURE_LOSS: dict[str, float] = {
    "X5R": 0.15,
    "X7R": 0.15,
    "X8R": 0.15,
    "X6S": 0.22,
    "X7S": 0.22,
    "X7T": 0.33,
    "Z5U": 0.56,
    "Y5V": 0.82,
}
#: Kinds of capacitor that are not class II ceramics, so lose no
#: capacitance to DC bias. A design names one in ``dielectric``.
STABLE_KINDS = frozenset({"TANTALUM", "ELECTROLYTIC", "POLYMER", "FILM"})
#: Class I dielectrics, whose capacitance does not depend on DC bias.
BIAS_FREE = frozenset({"C0G", "NP0"})

_SOURCE = (
    "worst-case minimum of a ceramic capacitor: nominal less tolerance, times "
    "the manufacturer's DC-bias factor, less the dielectric's temperature "
    "limit (EIA-198 temperature characteristic code)"
)
_EQUATION = "Ceff,min = Cnom * (1 - tol) * kbias(Vdc) * (1 - dCT)"


def mlcc_effective_capacitance(
    nominal: Given,
    v_dc: Given,
    *,
    dielectric: str | None,
    curve: BiasCurve | None,
    ref: str | None = None,
    mpn: str | None = None,
) -> CalcResult:
    """The least capacitance a ceramic capacitor gives in use.

    ``Ceff,min = Cnom * (1 - tol) * kbias(Vdc) * (1 - dCT)``, with
    ``kbias`` read from the part's DC-bias curve at the operating voltage
    and ``dCT`` the dielectric's temperature limit.

    With no curve the result is ``unverified`` and names the part and
    what to supply: Kairo has no default bias factor and takes none. A
    class I dielectric (C0G, NP0) needs no curve. A result that used a
    curve is typical, not guaranteed, because manufacturers publish
    DC-bias data as typical values at stated test conditions.

    Sources: the manufacturer's DC-bias curve for the part number; EIA-198
    for the temperature characteristic codes.

    Args:
        nominal: The marked capacitance, in F, with its tolerance.
        v_dc: The DC voltage across it in use, in V.
        dielectric: The dielectric code, such as ``X5R``.
        curve: The part's DC-bias curve, if known.
        ref: The capacitor's reference, for naming what is missing.
        mpn: Its part number, if the design gives one.

    Returns:
        The minimum effective capacitance, or ``unverified`` with what is
        missing.

    Raises:
        ValueError: If an input has the wrong unit.
    """
    if nominal.value.unit != "F" or v_dc.value.unit != "V":
        raise ValueError("nominal must be in F and v_dc in V")
    who = ref or "the capacitor"
    missing: list[Missing] = []
    warnings: list[str] = []
    code = (dielectric or "").upper()

    if nominal.tolerance is None:
        missing.append(
            Missing(
                "tolerance",
                ref,
                "the minimum cannot be computed",
                f"Give {who}'s capacitance tolerance",
            )
        )
    if not code:
        missing.append(
            Missing(
                "dielectric",
                ref,
                "neither its temperature limit nor its bias behaviour is known",
                f"Give {who}'s dielectric code, for example X7R or C0G",
            )
        )
    bias = 1.0
    loss = 0.0
    if code in BIAS_FREE:
        warnings.append(
            f"{code} is a class I dielectric: no DC-bias loss is applied, and its "
            "temperature change (tens of ppm per degree) is left out"
        )
    elif code:
        if code not in TEMPERATURE_LOSS:
            missing.append(
                Missing(
                    "temperature limit",
                    ref,
                    f"Kairo has no temperature limit for the dielectric {code}",
                    f"Give {who} a dielectric from: "
                    f"{', '.join(sorted(TEMPERATURE_LOSS))}, C0G",
                )
            )
        else:
            loss = TEMPERATURE_LOSS[code]
        if curve is None:
            part = f"part number {mpn}" if mpn else "no part number"
            missing.append(
                Missing(
                    "DC-bias curve",
                    ref,
                    f"{code} loses capacitance under DC bias by an amount only "
                    f"the manufacturer's curve gives ({part})",
                    f"Give {who} a part number (mpn) and add its DC-bias curve "
                    "from the manufacturer's data to the parts folder"
                    if not mpn
                    else f"Add the manufacturer's DC-bias curve for {mpn} to "
                    "the parts folder",
                )
            )
        else:
            # The bias factor falls as the voltage rises, so it is read at
            # the highest voltage the rail's tolerance allows.
            highest = abs(v_dc.number) * (1.0 + (v_dc.tolerance or 0.0) / 100.0)
            try:
                bias = curve.fraction_at(highest)
            except PartsError as exc:
                missing.append(
                    Missing(
                        "DC-bias curve",
                        ref,
                        str(exc),
                        f"Extend the curve for {mpn or who} to {highest:g} V",
                    )
                )
            else:
                warnings.append(
                    "typical, not guaranteed: the DC-bias curve is the "
                    f"manufacturer's typical data ({curve.conditions}; "
                    f"{curve.source.cite()})"
                )
                if curve.review != "reviewed":
                    warnings.append(
                        "the DC-bias curve has not been reviewed by a person"
                    )

    inputs = (nominal, v_dc)
    if missing:
        return CalcResult(
            name="mlcc_effective_capacitance",
            status="unverified",
            value=None,
            equation=_EQUATION,
            inputs=inputs,
            missing=tuple(missing),
            source=_SOURCE,
            warnings=tuple(warnings),
            accuracy=_accuracy(inputs, missing, done=False),
        )
    tolerance = (nominal.tolerance or 0.0) / 100.0
    value = nominal.number * (1.0 - tolerance) * bias * (1.0 - loss)
    return CalcResult(
        name="mlcc_effective_capacitance",
        status="bound",
        bound=">=",
        value=Quantity(value, "F"),
        equation=_EQUATION,
        inputs=inputs,
        source=_SOURCE,
        warnings=tuple(warnings),
        accuracy=_accuracy(
            inputs,
            []
            if v_dc.tolerance is not None or code in BIAS_FREE
            else [
                Missing(
                    "tolerance on Vdc",
                    None,
                    "the bias factor is read at the nominal voltage only",
                    "Give the tolerance of the voltage across the capacitor",
                )
            ],
            done=True,
        ),
    )


def regulator_stability(
    c_effective_min: float,
    c_effective_max: float,
    esr: Given | None,
    *,
    c_min: Given | None,
    esr_min: Given | None,
    esr_max: Given | None,
    ref: str | None = None,
) -> tuple[Literal["pass", "fail", "unverified"], tuple[str, ...]]:
    """Whether an output capacitor sits inside a regulator's stable region.

    The region is the datasheet's: a least capacitance and a range of
    equivalent series resistance. With no region given, or no ESR for the
    capacitor, the answer is ``unverified``; Kairo does not assume a
    regulator is stable with any capacitor.

    Source: the regulator's datasheet stability chart or table.

    Args:
        c_effective_min: The least effective capacitance, in F.
        c_effective_max: The most, in F.
        esr: The capacitor's ESR, in ohm, with its tolerance if known.
        c_min: The least capacitance the datasheet requires, in F.
        esr_min: The lowest ESR the datasheet allows, in ohm.
        esr_max: The highest ESR the datasheet allows, in ohm.
        ref: The regulator's reference.

    Returns:
        ``pass``, ``fail`` or ``unverified``, and one line per reason.
    """
    who = ref or "the regulator"
    reasons: list[str] = []
    if c_min is None or esr_min is None or esr_max is None:
        return (
            "unverified",
            (
                f"{who}: no stable region given (least capacitance, and lowest "
                "and highest ESR, from its datasheet's stability chart or table)",
            ),
        )
    if c_effective_min < c_min.number:
        reasons.append(
            f"{who}: the output capacitance can be {c_effective_min:.3g} F, below "
            f"the {c_min.number:.3g} F its datasheet requires ({c_min.origin})"
        )
    if c_effective_max < c_effective_min:
        raise ValueError("the capacitance range is the wrong way round")
    if esr is None:
        if reasons:
            return ("fail", tuple(reasons))
        return ("unverified", (f"{who}: no ESR given for the output capacitor",))
    spread = (esr.tolerance or 0.0) / 100.0
    lowest, highest = esr.number * (1 - spread), esr.number * (1 + spread)
    if lowest < esr_min.number:
        reasons.append(
            f"{who}: the capacitor's ESR can be {lowest:.3g} ohm, below the "
            f"{esr_min.number:.3g} ohm its datasheet requires ({esr_min.origin})"
        )
    if highest > esr_max.number:
        reasons.append(
            f"{who}: the capacitor's ESR can be {highest:.3g} ohm, above the "
            f"{esr_max.number:.3g} ohm its datasheet allows ({esr_max.origin})"
        )
    if reasons:
        return ("fail", tuple(reasons))
    if esr.tolerance is None:
        return (
            "unverified",
            (f"{who}: the capacitor's ESR has no tolerance, so its range is unknown",),
        )
    return ("pass", ())


def _accuracy(
    inputs: tuple[Given, ...], missing: list[Missing], *, done: bool
) -> Accuracy:
    because = [
        f"{m.ref + ': ' if m.ref else ''}no {m.name}; {m.effect}" for m in missing
    ]
    tolerances = tuple(
        {
            "ref": g.ref,
            "parameter": g.name,
            "tolerance": None if g.tolerance is None else f"{g.tolerance:g}%",
            "source": g.source if g.tolerance is not None else "missing",
        }
        for g in inputs
    )
    worst = (
        {
            "method": "minimum by construction: every factor at its lowest",
            "scope": "initial tolerance, DC bias and temperature",
            "status": "computed",
        }
        if done
        else {"method": "not computed", "scope": "none", "status": "unverified"}
    )
    return Accuracy(
        "verified" if done and not missing else "unverified",
        tolerances,
        worst,
        tuple(because),
        tuple(m.resolve for m in missing),
    )


def least_capacitance(nominal: Given) -> float | None:
    """The least a capacitor that does not lose capacitance with bias gives.

    ``Cmin = Cnom * (1 - tol)``: a tantalum, electrolytic, polymer, film
    or class I ceramic capacitor at the bottom of its tolerance. Its
    change with temperature and age is not included, and the result says
    nothing about a class II ceramic, which `mlcc_effective_capacitance`
    is for.

    Args:
        nominal: The marked capacitance, with its tolerance.

    Returns:
        The least capacitance in F, or ``None`` if the tolerance is not
        given.
    """
    if nominal.tolerance is None:
        return None
    return nominal.number * (1 - nominal.tolerance / 100.0)


def within_stable_region(
    c_low: float,
    esr_low: float,
    esr_high: float,
    *,
    c_min: Given,
    esr_min: Given,
    esr_max: Given,
    ref: str,
) -> tuple[str, ...]:
    """Why an output capacitor is outside a regulator's stable region, if it is.

    The region is the datasheet's: at least ``c_min`` of capacitance, and
    an equivalent series resistance from ``esr_min`` to ``esr_max``. The
    capacitor is inside when its least capacitance and both ends of its
    ESR range are.

    Source: the regulator's datasheet stability chart or table.

    Args:
        c_low: The least capacitance the capacitor gives, in F.
        esr_low: The lowest its ESR can be, in ohm.
        esr_high: The highest, in ohm.
        c_min: The least capacitance the datasheet requires.
        esr_min: The lowest ESR the datasheet allows.
        esr_max: The highest ESR the datasheet allows.
        ref: The regulator's reference.

    Returns:
        One line per way it is outside; empty if it is inside.

    Raises:
        ValueError: If the ESR range is the wrong way round.
    """
    if esr_high < esr_low:
        raise ValueError("the ESR range is the wrong way round")
    reasons: list[str] = []
    if c_low < c_min.number:
        reasons.append(
            f"the output capacitance can be {c_low * 1e6:.3g} uF, below the "
            f"{c_min.number * 1e6:.3g} uF {ref}'s datasheet requires"
        )
    if esr_low < esr_min.number:
        reasons.append(
            f"the output capacitor's ESR can be {esr_low:.3g} ohm, below the "
            f"{esr_min.number:.3g} ohm {ref}'s datasheet requires"
        )
    if esr_high > esr_max.number:
        reasons.append(
            f"the output capacitor's ESR can be {esr_high:.3g} ohm, above the "
            f"{esr_max.number:.3g} ohm {ref}'s datasheet allows"
        )
    return tuple(reasons)
