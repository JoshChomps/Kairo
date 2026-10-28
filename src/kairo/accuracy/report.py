"""A design's accuracy, gathered in one place.

Kairo cannot control what a model tells the user, so the accuracy of a
design is carried in the data: every tolerance in use with where it came
from, every result that is not verified with what would resolve it, and
counts. The same content is written as Markdown beside the design, so a
person sees it in the project folder whether or not the model relays it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kairo.ir import Design
from kairo.verify.requirements import RequirementResult

#: The file the report is saved as, inside the project folder.
REPORT_FILE = "kairo/accuracy-report.md"


@dataclass(frozen=True, slots=True)
class AccuracyReport:
    """What is and is not established about a design.

    Attributes:
        design: The design's name.
        status: ``verified`` only if every requirement and check passed.
        counts: How many results pass, fail and are unverified.
        tolerances: Every part tolerance in use, with its source, and
            every part that has none where one is needed.
        to_resolve: What to supply, each with the results it would resolve.
        results: One line per requirement or check.
        policies: The project's own choices that results rest on, a line
            each. A pass under one is a pass by the project's reckoning,
            which the line spells out.
        simulation: What a simulation of the design would rest on
            (models, simulators, thresholds), when it was asked for.
    """

    design: str
    status: str
    counts: dict[str, int]
    tolerances: tuple[dict[str, Any], ...]
    to_resolve: tuple[dict[str, Any], ...]
    results: tuple[dict[str, Any], ...]
    policies: tuple[str, ...] = ()
    simulation: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        """The report as JSON-ready data, status and what to resolve first."""
        out: dict[str, Any] = {
            "design": self.design,
            "status": self.status,
            "to_resolve": list(self.to_resolve),
            "counts": dict(self.counts),
            "results": list(self.results),
            "tolerances": list(self.tolerances),
            "policies": list(self.policies),
        }
        if self.simulation is not None:
            out["simulation"] = self.simulation
        return out

    def as_markdown(self) -> str:
        """The report as a Markdown page for the project folder."""
        lines = [
            f"# Accuracy report: {self.design}",
            "",
            f"Status: **{self.status}**. "
            f"{self.counts['pass']} pass, {self.counts['fail']} fail, "
            f"{self.counts['unverified']} unverified.",
            "",
            "A result is `pass` only when its worst case over every input's "
            "tolerance is proven. `unverified` means an input was missing; it "
            "is never a pass.",
            "",
            "## To resolve",
            "",
        ]
        if self.to_resolve:
            for item in self.to_resolve:
                count = len(item["resolves"])
                lines.append(
                    f"- {item['supply']} (resolves {count} "
                    f"result{'s' if count != 1 else ''}: "
                    f"{', '.join(item['resolves'])})"
                )
        else:
            lines.append("Nothing: every result is established.")
        lines += ["", "## Results", "", "| Check | Status | Found |", "|---|---|---|"]
        for result in self.results:
            lines.append(
                f"| {result['check']} | {result['status']} | "
                f"{result['summary'].replace('|', '/')} |"
            )
        lines += [
            "",
            "## Tolerances in use",
            "",
            "| Part | Parameter | Tolerance | Source |",
            "|---|---|---|---|",
        ]
        for row in self.tolerances:
            lines.append(
                f"| {row['ref']} | {row['parameter']} | "
                f"{row['tolerance'] or 'none given'} | {row['source']} |"
            )
        if self.policies:
            lines += ["", "## Project policies in use", ""]
            lines += [f"- {policy}" for policy in self.policies]
        return "\n".join(lines) + "\n"


def accuracy_report(design: Design, results: list[RequirementResult]) -> AccuracyReport:
    """Gather a design's accuracy from its requirement results.

    Args:
        design: The design.
        results: The results of `evaluate_requirements`.

    Returns:
        The report.
    """
    counts = {"pass": 0, "fail": 0, "unverified": 0}
    counts["pass_on_default_margins"] = 0
    defaults: list[str] = []
    lines: list[dict[str, Any]] = []
    resolve: dict[str, list[str]] = {}
    for result in results:
        counts[result.status] += 1
        check = (
            f"requirement {result.index + 1}: {result.type}"
            if result.index >= 0
            else result.type
        )
        subject = result.where.ref or result.where.net
        if subject:
            check += f" ({subject})"
        lines.append(
            {
                "check": check,
                "status": result.status,
                "basis": result.basis,
                "summary": result.summary,
            }
        )
        for line in result.on_defaults:
            if line not in defaults:
                defaults.append(line)
        if result.status == "pass" and result.on_defaults:
            counts["pass_on_default_margins"] += 1
        for supply in result.to_resolve:
            resolve.setdefault(supply, [])
            if check not in resolve[supply]:
                resolve[supply].append(check)

    tolerances: list[dict[str, Any]] = []
    for ref, part in design.components.items():
        stated = {
            "tolerance": "value",
            "v_out_tolerance": "Vout",
            "v_f_tolerance": "Vf",
        }
        for parameter, label in stated.items():
            quantity = part.quantity(parameter)
            if quantity is not None:
                tolerances.append(
                    {
                        "ref": ref,
                        "parameter": label,
                        "tolerance": f"{quantity.value:g}%",
                        "source": part.source,
                    }
                )
    seen = {(row["ref"], row["parameter"]) for row in tolerances}
    for result in results:
        for calculation in result.results:
            for row in calculation.accuracy.tolerances:
                if row["tolerance"] is None and row["ref"] is not None:
                    key = (row["ref"], row["parameter"])
                    if key not in seen:
                        seen.add(key)
                        tolerances.append(
                            {
                                "ref": row["ref"],
                                "parameter": row["parameter"],
                                "tolerance": None,
                                "source": "missing",
                            }
                        )
    policies: list[str] = []
    if defaults:
        policies.append(
            "Default margins: where the design states no tolerance, Kairo "
            "reckoned with its own generous one (" + "; ".join(defaults) + "). "
            "A pass on these holds for any ordinary part and is not a pass "
            "against a datasheet's figure; state the real ones, or set the "
            "policy's margins to stated, for the check before a board is "
            "ordered."
        )
    elif design.policy.margins == "stated":
        policies.append(
            "Margins: only tolerances the design states were used; Kairo's "
            "default margins are turned off."
        )
    margin = design.policy.theta_ja_margin
    if margin is not None:
        policies.append(
            "Thermal: a junction temperature is reckoned with the datasheet's "
            f"theta_JA, a typical value on the JEDEC test board, plus {margin.value:g}%"
            ", a margin this project chose. A pass under it is not a pass against "
            "a datasheet limit."
        )
    status = (
        "failed"
        if counts["fail"]
        else "unverified"
        if counts["unverified"]
        else "verified"
    )
    return AccuracyReport(
        design.design,
        status,
        counts,
        tuple(tolerances),
        tuple(
            {"supply": supply, "resolves": checks}
            for supply, checks in sorted(resolve.items(), key=lambda kv: -len(kv[1]))
        ),
        tuple(lines),
        tuple(policies),
    )
