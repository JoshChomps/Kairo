"""What a check reports: one finding per problem."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class Where:
    """What a finding is about.

    Attributes:
        ref: The part's reference, if it concerns a part.
        pin: The pin's name, if it concerns a pin.
        net: The net's name, if it concerns a net.
    """

    ref: str | None = None
    pin: str | None = None
    net: str | None = None


@dataclass(frozen=True, slots=True)
class Finding:
    """One problem, with enough to fix it in one step.

    Attributes:
        layer: The verifier layer that found it, ``L0`` to ``L6``.
        code: A stable code such as ``E_PIN_UNKNOWN``.
        severity: ``error`` or ``warning``.
        message: What is wrong, naming the part, pin or net.
        where: What it is about.
        fix_hint: What would put it right, when Kairo can say.
    """

    layer: Literal["L0", "L1", "L2", "L3", "L4", "L5", "L6"]
    code: str
    severity: Literal["error", "warning"]
    message: str
    where: Where = field(default_factory=Where)
    fix_hint: str = ""

    def as_dict(self) -> dict[str, Any]:
        """The finding as JSON-ready data."""
        return asdict(self)
