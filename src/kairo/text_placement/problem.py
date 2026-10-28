"""What a text-placement problem is made of.

A problem is a set of parts (fixed boxes), the labels that belong to them
(reference designators, values, net names), and the sheet or board area the
labels must stay inside. Each label may sit at one of a finite set of
candidate spots around its part, or be left unplaced. Working from a finite
candidate list is what makes the placement exact: the solver and the scorer
price exactly the same spots.
"""

from __future__ import annotations

import enum
import functools
from dataclasses import dataclass, field

from kairo.text_placement.geometry import Box


class Side(enum.Enum):
    """The side of a part a label sits on."""

    N = "N"
    E = "E"
    S = "S"
    W = "W"


@dataclass(frozen=True, slots=True)
class Part:
    """Something labels must not cover: a symbol body or a footprint courtyard.

    Attributes:
        ref: The part's reference designator, unique within the problem.
        box: Its outline, in mm.
    """

    ref: str
    box: Box


@dataclass(frozen=True, slots=True)
class Spot:
    """One place a label can sit, relative to its part and on the page.

    Attributes:
        box: Where the text would be drawn, in mm.
        side: Which side of the part it is on.
        turned: True if the text is rotated 90° from its normal reading
            direction.
        along: Signed offset from the middle of that side, in mm.
        push: How much farther from the part than the minimum gap, in mm.
    """

    box: Box
    side: Side
    turned: bool
    along: float
    push: float

    def same_place(self, other: Spot, tolerance: float = 1e-6) -> bool:
        """Say whether two spots are the same place relative to the part.

        Used after an edit, when the part may have moved but a label that kept
        its side, turn, offset and push has not really moved.

        Args:
            other: The spot to compare with.
            tolerance: How close the offsets must be, in mm.

        Returns:
            True if side and turn match and the offsets agree.
        """
        return (
            self.side is other.side
            and self.turned == other.turned
            and abs(self.along - other.along) <= tolerance
            and abs(self.push - other.push) <= tolerance
        )


@dataclass(frozen=True, slots=True)
class Label:
    """A piece of text that belongs to one part.

    Attributes:
        owner: The ``ref`` of the part it belongs to.
        text: What it says.
        width: Text width when not turned, in mm. In Kairo this comes from
            calibration against KiCad's own output, never from a guess.
        height: Text height when not turned, in mm.
        preferred: The side it should sit on when nothing else matters.
        pinned: A spot a person chose. A pinned label is never moved and
            acts as an obstacle for every other label.
    """

    owner: str
    text: str
    width: float
    height: float
    preferred: Side = Side.N
    pinned: Spot | None = None


@dataclass(frozen=True, slots=True)
class Rules:
    """The geometry that defines the problem, shared by solver and scorer.

    Attributes:
        gap: Minimum distance between a label and its own part, in mm.
        clearance: Distance below which a label counts as too close to
            another label or part, in mm.
        step: Spacing of candidate spots along each side, in mm.
        pushes: Extra distances from the part to offer, beyond ``gap``, in mm.
        allow_turn: Whether turned (90°) spots are offered.
        max_distance: Beyond this distance from its part a label floats too
            far to be read as the part's, in mm.
    """

    gap: float = 0.20
    clearance: float = 0.15
    step: float = 0.25
    pushes: tuple[float, ...] = (0.0, 0.5, 1.0)
    allow_turn: bool = True
    max_distance: float = 2.0


@dataclass(frozen=True)
class Problem:
    """A complete text-placement problem.

    Attributes:
        parts: Every obstacle part, labelled or not.
        labels: The labels to place, in a fixed order that every result
            follows.
        area: The sheet or board outline; labels must stay inside it.
        rules: The geometry rules.
    """

    parts: tuple[Part, ...]
    labels: tuple[Label, ...]
    area: Box
    rules: Rules = field(default_factory=Rules)

    def __post_init__(self) -> None:
        """Check the problem is self-consistent.

        Raises:
            ValueError: If two parts share a reference or a label names a part
                that does not exist.
        """
        refs = [p.ref for p in self.parts]
        if len(set(refs)) != len(refs):
            raise ValueError("duplicate part references")
        missing = {lab.owner for lab in self.labels} - set(refs)
        if missing:
            raise ValueError(f"labels name unknown parts: {sorted(missing)}")

    @functools.cached_property
    def part_index(self) -> dict[str, int]:
        """Map from part reference to its position in ``parts``."""
        return {p.ref: i for i, p in enumerate(self.parts)}

    def owner_of(self, label_index: int) -> Part:
        """Return the part a label belongs to.

        Args:
            label_index: Position of the label in ``labels``.

        Returns:
            The owning part.
        """
        return self.parts[self.part_index[self.labels[label_index].owner]]

    @functools.cached_property
    def candidates(self) -> tuple[tuple[Spot, ...], ...]:
        """Candidate spots for every label, in a fixed order.

        A pinned label has exactly one candidate, its pinned spot.
        """
        return tuple(
            (lab.pinned,) if lab.pinned is not None else self._spots_for(i)
            for i, lab in enumerate(self.labels)
        )

    def _spots_for(self, label_index: int) -> tuple[Spot, ...]:
        label = self.labels[label_index]
        owner = self.owner_of(label_index).box
        r = self.rules
        spots: list[Spot] = []
        turns = (False, True) if r.allow_turn else (False,)
        for turned in turns:
            w, h = (
                (label.height, label.width) if turned else (label.width, label.height)
            )
            for push in r.pushes:
                for side in Side:
                    for along in _offsets(owner, side, r.step):
                        spots.append(
                            Spot(
                                box=_box_at(owner, side, along, push + r.gap, w, h),
                                side=side,
                                turned=turned,
                                along=along,
                                push=push,
                            )
                        )
        return tuple(spots)


def _offsets(owner: Box, side: Side, step: float) -> list[float]:
    """Offsets along a side, symmetric about its middle, always including 0."""
    half = (owner.width if side in (Side.N, Side.S) else owner.height) / 2
    count = int(half / step + 1e-9)
    return [k * step for k in range(-count, count + 1)]


def _box_at(
    owner: Box, side: Side, along: float, out: float, w: float, h: float
) -> Box:
    """The text box on ``side``, ``along`` from the middle, ``out`` away."""
    if side is Side.N:
        return Box.centred(owner.cx + along, owner.y1 + out + h / 2, w, h)
    if side is Side.S:
        return Box.centred(owner.cx + along, owner.y0 - out - h / 2, w, h)
    if side is Side.E:
        return Box.centred(owner.x1 + out + w / 2, owner.cy + along, w, h)
    return Box.centred(owner.x0 - out - w / 2, owner.cy + along, w, h)


def spot_like(problem: Problem, label_index: int, previous: Spot) -> Spot | None:
    """Find the candidate that matches a spot from an earlier layout.

    After an edit a part may have moved, so the old spot's box is stale. The
    matching candidate keeps the same side, turn and push, with the nearest
    offset along the side.

    Args:
        problem: The problem after the edit.
        label_index: Which label.
        previous: Its spot before the edit.

    Returns:
        The matching candidate, or None if no candidate has that side, turn
        and push (for example because turning is now switched off).
    """
    best: Spot | None = None
    for spot in problem.candidates[label_index]:
        if (
            spot.side is previous.side
            and spot.turned == previous.turned
            and abs(spot.push - previous.push) <= 1e-6
            and (
                best is None
                or abs(spot.along - previous.along) < abs(best.along - previous.along)
            )
        ):
            best = spot
    return best
