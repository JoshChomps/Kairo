"""What a calculation returns: a value that explains itself.

A result is one of three things. ``ok`` is a value computed from inputs
that were all given. ``bound`` is a one-sided limit: an input was missing,
and the term it belongs to can only move the result one way, so the value
is a proven lower or upper limit and says which. ``unverified`` is no
value at all, with the missing inputs listed. Kairo never puts a number in
place of a missing input.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from kairo.units import Quantity

if TYPE_CHECKING:
    from kairo.calc.worst_case import WorstCase


@dataclass(frozen=True, slots=True)
class Given:
    """An input a calculation used.

    Attributes:
        name: The symbol in the equation, such as ``Vin``.
        value: Its value.
        origin: Where the value came from, such as ``net VIN_5V``.
        ref: The part it belongs to, if any.
        tolerance: Its tolerance in percent, or ``None`` if none was given.
        source: Where the tolerance came from: ``user-stated``, a
            datasheet reference, or ``missing``.
    """

    name: str
    value: Quantity
    origin: str = ""
    ref: str | None = None
    tolerance: float | None = None
    source: str = "missing"

    @property
    def number(self) -> float:
        """The value as a plain number in its base unit."""
        return self.value.value


@dataclass(frozen=True, slots=True)
class Missing:
    """An input a calculation needed and did not get.

    Attributes:
        name: The symbol in the equation, such as ``Iq``.
        ref: The part it belongs to, if any.
        effect: What its absence does to the result.
        resolve: What to supply, in words the user can act on.
    """

    name: str
    ref: str | None
    effect: str
    resolve: str


@dataclass(frozen=True, slots=True)
class Accuracy:
    """How far a result can be trusted, and what would make it better.

    Attributes:
        status: ``verified`` only when the worst case over every input's
            tolerance was computed; otherwise ``unverified``.
        tolerances: Each input's tolerance and where it came from.
        worst_case: The method and scope of the worst case, and its status.
        unverified_because: One line per reason, ready to relay.
        to_resolve: One line per thing to supply, ready to relay.
    """

    status: Literal["verified", "unverified"]
    tolerances: tuple[dict[str, Any], ...]
    worst_case: dict[str, str]
    unverified_because: tuple[str, ...]
    to_resolve: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        """The block as JSON-ready data, status and what to resolve first."""
        return {
            "status": self.status,
            "to_resolve": list(self.to_resolve),
            "unverified_because": list(self.unverified_because),
            "tolerances": list(self.tolerances),
            "worst_case": dict(self.worst_case),
        }


@dataclass(frozen=True, slots=True)
class Corners:
    """A result's range over its inputs' tolerances.

    Attributes:
        status: ``computed``, or ``unverified`` when an input had no
            tolerance.
        minimum: The smallest value reached.
        maximum: The largest value reached.
        method: ``monotonic corners`` (exact) or ``search`` (the worst
            found, not proven to be the worst there is).
        enclosure: A range sure to contain every result, as its two ends.
        missing: Why it is unverified, one line per input.
        case: The full worst case, for judging a limit.
    """

    status: Literal["computed", "unverified"]
    minimum: Quantity | None = None
    maximum: Quantity | None = None
    method: str = "not computed"
    enclosure: tuple[float, float] | None = None
    missing: tuple[str, ...] = ()
    case: WorstCase | None = None

    def as_dict(self) -> dict[str, Any]:
        """The range as JSON-ready data."""
        if self.status != "computed" or self.minimum is None or self.maximum is None:
            return {"status": self.status, "missing": list(self.missing)}
        unit = self.minimum.unit
        return {
            "status": self.status,
            "min": f"{self.minimum.value:.6g} {unit}".rstrip(),
            "max": f"{self.maximum.value:.6g} {unit}".rstrip(),
            "method": self.method,
            "scope": "initial tolerance only",
            "enclosure": (
                None
                if self.enclosure is None
                else [f"{end:.6g} {unit}".rstrip() for end in self.enclosure]
            ),
        }


@dataclass(frozen=True, slots=True)
class CalcResult:
    """One computed quantity.

    Attributes:
        name: What was computed, such as ``ldo_dissipation``.
        status: ``ok``, ``bound`` or ``unverified``.
        value: The nominal value, or the limit for a bound; ``None`` when
            unverified.
        bound: For a bound, ``>=`` or ``<=``: the true value is at least,
            or at most, ``value``.
        equation: The formula, in the inputs' symbols.
        inputs: The inputs used.
        missing: The inputs that were not given.
        source: Where the formula comes from.
        warnings: What a reader should know about the result's limits.
        accuracy: How far the result can be trusted.
        corners: The range over the inputs' tolerances, when computed.
    """

    name: str
    status: Literal["ok", "bound", "unverified"]
    value: Quantity | None
    equation: str
    inputs: tuple[Given, ...]
    source: str
    accuracy: Accuracy
    bound: Literal[">=", "<="] | None = None
    missing: tuple[Missing, ...] = ()
    warnings: tuple[str, ...] = field(default=())
    corners: Corners | None = None

    @property
    def case(self) -> WorstCase | None:
        """The worst case over tolerances, if it was computed."""
        return self.corners.case if self.corners else None

    def text(self) -> str:
        """The value as a reader should see it, such as ``>= 0.512 W``."""
        if self.value is None:
            return "unverified"
        shown = f"{self.value.value:.4g} {self.value.unit}".rstrip()
        return f"{self.bound} {shown}" if self.bound else shown

    def as_dict(self) -> dict[str, Any]:
        """The result as JSON-ready data, status and accuracy first."""
        return {
            "name": self.name,
            "status": self.status,
            "value": self.text(),
            "accuracy": self.accuracy.as_dict(),
            "corners": self.corners.as_dict() if self.corners else None,
            "equation": self.equation,
            "inputs": {
                given.name: f"{given.value.value:g} {given.value.unit}".rstrip()
                + (f" ({given.origin})" if given.origin else "")
                for given in self.inputs
            },
            "missing": [
                {"input": m.name, "part": m.ref, "effect": m.effect}
                for m in self.missing
            ],
            "source": self.source,
            "warnings": list(self.warnings),
        }


def accuracy_of(inputs: tuple[Given, ...], missing: tuple[Missing, ...]) -> Accuracy:
    """Build the accuracy block for a result from what it used and lacked.

    Until the worst case over tolerances is computed, a result is
    ``unverified`` whenever an input is missing or has no tolerance, and
    the block names each one.

    Args:
        inputs: The inputs used.
        missing: The inputs not given.

    Returns:
        The accuracy block.
    """
    because: list[str] = []
    resolve: list[str] = []
    for item in missing:
        owner = f"{item.ref}: " if item.ref else ""
        because.append(f"{owner}no {item.name}; {item.effect}")
        resolve.append(item.resolve)
    tolerances: list[dict[str, Any]] = []
    for given in inputs:
        tolerances.append(
            {
                "ref": given.ref,
                "parameter": given.name,
                "tolerance": None
                if given.tolerance is None
                else f"{given.tolerance:g}%",
                "source": given.source if given.tolerance is not None else "missing",
            }
        )
        if given.tolerance is None:
            owner = f"{given.ref}: " if given.ref else ""
            because.append(f"{owner}no tolerance on {given.name}")
            resolve.append(
                f"Give the tolerance of {given.name}"
                + (f" for {given.ref}" if given.ref else "")
                + ", with where it comes from"
            )
    # The worst case over tolerances is not computed yet, so nothing is
    # verified, whatever the inputs.
    worst_case = {
        "method": "not computed",
        "scope": "none",
        "status": "unverified",
    }
    if not because:
        because.append("the worst case over the input tolerances is not computed yet")
    return Accuracy(
        "unverified", tuple(tolerances), worst_case, tuple(because), tuple(resolve)
    )
