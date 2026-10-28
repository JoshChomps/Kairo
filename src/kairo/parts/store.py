"""Facts about parts that the KiCad libraries do not hold, keyed by part number.

A part's file gives datasheet facts (ratings, thermal resistance, a
regulator's stable region) and, for a ceramic capacitor, its DC-bias
curve as points sampled from the manufacturer's published data. Every
value records where it came from. A value with no source is refused when
the file is read: Kairo would rather report ``unverified`` than use a
number nobody can check.

Values taken from a document by a program are ``unreviewed`` until a
person marks them ``reviewed``, and a result that used one says so.
"""

from __future__ import annotations

import itertools
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class PartsError(ValueError):
    """A part's file cannot be used."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(_Strict):
    """Where a value came from.

    Attributes:
        document: The document's title or number, and its publisher.
        revision: Its revision or date.
        page: The page, table or figure.
        url: Where it was read, if it was read online.
    """

    document: str = Field(min_length=3)
    revision: str = Field(min_length=1)
    page: str = Field(min_length=1)
    url: str | None = None

    def cite(self) -> str:
        """The source in one line."""
        return f"{self.document}, {self.revision}, {self.page}"


class Fact(_Strict):
    """One value from a datasheet.

    Attributes:
        value: The value with its unit, as text.
        source: Where it came from.
        obtained: ``typed`` by a person or ``extracted`` by a program.
        review: ``reviewed`` once a person has checked it.
        condition: The conditions the datasheet gives it under.
        limit: ``min`` or ``max`` if the value is the datasheet's limit
            on that side; ``None`` for a typical or nominal value.
    """

    value: str
    source: Source
    obtained: Literal["typed", "extracted"] = "typed"
    review: Literal["reviewed", "unreviewed"] = "unreviewed"
    condition: str = ""
    limit: Literal["min", "max"] | None = None


class BiasCurve(_Strict):
    """A ceramic capacitor's capacitance against DC bias.

    Attributes:
        points: Pairs of DC voltage and the capacitance at that voltage
            as a fraction of nominal, in rising order of voltage. The
            first point is at zero volts.
        conditions: The test conditions the manufacturer states.
        source: Where the curve came from.
        review: ``reviewed`` once a person has checked the points.
    """

    points: list[tuple[float, float]] = Field(min_length=2)
    conditions: str = Field(min_length=3)
    source: Source
    review: Literal["reviewed", "unreviewed"] = "unreviewed"

    @model_validator(mode="after")
    def _ordered(self) -> BiasCurve:
        volts = [point[0] for point in self.points]
        if volts[0] != 0 or any(b <= a for a, b in itertools.pairwise(volts)):
            raise ValueError("points must start at 0 V and rise in voltage")
        if any(not 0 < fraction <= 1.5 for _, fraction in self.points):
            raise ValueError("each fraction of nominal must be above 0 and at most 1.5")
        return self

    def fraction_at(self, volts: float) -> float:
        """The fraction of nominal capacitance at a DC bias.

        Straight lines join the sampled points. A voltage beyond the last
        point is refused: the curve says nothing there.

        Args:
            volts: The DC bias, as a magnitude.

        Returns:
            The fraction of nominal.

        Raises:
            PartsError: If the voltage is beyond the curve.
        """
        volts = abs(volts)
        if volts > self.points[-1][0]:
            raise PartsError(
                f"the curve ends at {self.points[-1][0]:g} V; {volts:g} V is beyond it"
            )
        for (v0, f0), (v1, f1) in itertools.pairwise(self.points):
            if volts <= v1:
                return f0 + (f1 - f0) * (volts - v0) / (v1 - v0)
        return self.points[-1][1]


class Part(_Strict):
    """What is known about one part number.

    Attributes:
        mpn: The manufacturer's part number.
        manufacturer: Who makes it.
        facts: Datasheet values by parameter name.
        dc_bias: The DC-bias curve, for a ceramic capacitor.
    """

    mpn: str = Field(min_length=2)
    manufacturer: str = Field(min_length=2)
    facts: dict[str, Fact] = Field(default_factory=dict[str, Fact])
    dc_bias: BiasCurve | None = None


class PartStore:
    """The parts in one folder, one JSON file per part number.

    Args:
        folder: The folder to read from; it need not exist.
    """

    def __init__(self, folder: Path) -> None:
        """Remember the folder; files are read when asked for."""
        self.folder = folder

    def get(self, mpn: str) -> Part | None:
        """Read a part's file.

        Args:
            mpn: The part number, in any letter case: a drawing's
                field may spell it otherwise than its maker does.

        Returns:
            The part, or ``None`` if there is no file for it.

        Raises:
            PartsError: If the file is not valid, for example because a
                value has no source.
        """
        wanted = f"{_safe(mpn)}.json".casefold()
        # Looked for by name in the folder, so that the answer is the
        # same where the file system tells letter cases apart.
        path = next(
            (
                found
                for found in sorted(self.folder.glob("*.json"))
                if found.name.casefold() == wanted
            ),
            None,
        )
        if path is None:
            return None
        try:
            part = Part.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, ValidationError) as exc:
            raise PartsError(f"{path.name}: {exc}") from exc
        if part.mpn.casefold() != mpn.casefold():
            raise PartsError(f"{path.name} is for {part.mpn!r}, not {mpn!r}")
        return part

    def put(self, part: Part) -> Path:
        """Write a part's file, replacing the one there.

        Args:
            part: The part.

        Returns:
            The file written.
        """
        self.folder.mkdir(parents=True, exist_ok=True)
        # A file for the same number in another letter case is the same
        # part's, and is the one replaced.
        wanted = f"{_safe(part.mpn)}.json"
        path = next(
            (
                found
                for found in sorted(self.folder.glob("*.json"))
                if found.name.casefold() == wanted.casefold()
            ),
            self.folder / wanted,
        )
        text = json.dumps(part.model_dump(mode="json"), indent=2, ensure_ascii=False)
        # Written beside and moved into place, so a reader never sees half.
        beside = path.with_name(path.name + ".writing")
        beside.write_text(text + "\n", encoding="utf-8")
        os.replace(beside, path)
        return path


def _safe(mpn: str) -> str:
    """A part number as a file name."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in mpn)
