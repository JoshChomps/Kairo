"""The verifier's result: what each layer found, in order.

Layers run in order and a blocking layer that finds an error stops the
run, so the first thing a caller sees is a root cause, not its
consequences. A layer that was not reached is ``not run``, and a layer
whose referee could not be run is ``unavailable``; neither is a pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from kairo.verify.findings import Finding

#: The layers this report covers, in the order they run.
LAYERS = ("L0", "L1", "L2", "L3", "L4")
#: Findings returned per page, to protect a model's context.
FINDINGS_PER_PAGE = 20

LayerStatus = Literal["pass", "fail", "unavailable", "not run"]
_UNAVAILABLE = frozenset(
    {"E_NETLIST_UNAVAILABLE", "E_ERC_UNAVAILABLE", "E_KICAD_TEMPLATE_UNAVAILABLE"}
)


@dataclass(frozen=True, slots=True)
class Report:
    """What the verifier found.

    Attributes:
        findings: Every finding, by layer and then errors before warnings.
        reached: The layers that ran, in order.
    """

    findings: tuple[Finding, ...]
    reached: tuple[str, ...]

    @property
    def passed(self) -> bool:
        """True only if every layer ran and none found an error."""
        return self.reached == LAYERS and not any(
            finding.severity == "error" for finding in self.findings
        )

    def status(self, layer: str) -> LayerStatus:
        """How one layer came out.

        Args:
            layer: ``L0`` to ``L4``.

        Returns:
            ``pass``, ``fail``, ``unavailable`` or ``not run``.
        """
        if layer not in self.reached:
            return "not run"
        errors = [
            f for f in self.findings if f.layer == layer and f.severity == "error"
        ]
        if any(f.code in _UNAVAILABLE for f in errors):
            return "unavailable"
        return "fail" if errors else "pass"

    @property
    def score(self) -> float:
        """A number from 0 to 1 for a repair loop to watch, never a verdict.

        Each layer counts equally: 1 if it passed, 0 if it was not reached
        or its referee was unavailable, and otherwise one over one plus
        its number of errors, so fewer errors score higher.
        """
        total = 0.0
        for layer in LAYERS:
            status = self.status(layer)
            if status == "pass":
                total += 1.0
            elif status == "fail":
                errors = sum(
                    1
                    for f in self.findings
                    if f.layer == layer and f.severity == "error"
                )
                total += 1.0 / (1 + errors)
        return round(total / len(LAYERS), 4)

    def as_dict(self, page: int = 1) -> dict[str, Any]:
        """The report as JSON-ready data, one page of findings at a time.

        Args:
            page: Which page of findings, from 1.

        Returns:
            Whether it passed, each layer's status and counts, the score,
            and at most `FINDINGS_PER_PAGE` findings.

        Raises:
            ValueError: If the page does not exist.
        """
        pages = max(
            (len(self.findings) + FINDINGS_PER_PAGE - 1) // FINDINGS_PER_PAGE, 1
        )
        if not 1 <= page <= pages:
            raise ValueError(f"findings has pages 1 to {pages}; got page {page}")
        start = (page - 1) * FINDINGS_PER_PAGE
        return {
            "passed": self.passed,
            "layers": {
                layer: {
                    "status": self.status(layer),
                    "errors": sum(
                        1
                        for f in self.findings
                        if f.layer == layer and f.severity == "error"
                    ),
                    "warnings": sum(
                        1
                        for f in self.findings
                        if f.layer == layer and f.severity == "warning"
                    ),
                }
                for layer in LAYERS
            },
            "score": self.score,
            "score_note": "a diagnostic for repair; passed is the verdict",
            "findings_total": len(self.findings),
            "findings_page": page,
            "findings_pages": pages,
            "findings": [
                f.as_dict() for f in self.findings[start : start + FINDINGS_PER_PAGE]
            ],
        }


def make_report(findings: list[Finding], reached: list[str]) -> Report:
    """Order findings and build the report.

    Args:
        findings: Everything found, in any order.
        reached: The layers that ran.

    Returns:
        The report, findings sorted by layer and then errors first.
    """
    ordered = sorted(
        findings,
        key=lambda f: (
            LAYERS.index(f.layer) if f.layer in LAYERS else 9,
            f.severity != "error",
        ),
    )
    return Report(tuple(ordered), tuple(layer for layer in LAYERS if layer in reached))
