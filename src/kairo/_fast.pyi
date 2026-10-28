"""Kairo's compiled inner loops (rust/kairo_fast/src/lib.rs)."""

from array import array
from collections.abc import Sequence

class Grazes:
    """A class's grazing diagonal steps, converted once per board."""

    def __init__(self, steps: dict[int, tuple[int, ...]]) -> None: ...

class Search:
    """A search's per-cell arrays, kept between searches."""

    def __init__(self, size: int) -> None: ...
    def find(
        self,
        nx: int,
        ny: int,
        layers: int,
        sources: list[int],
        goal: set[int],
        bbox: tuple[int, int, int, int],
        window: tuple[int, int, int, int],
        foes: list[array[int]],
        number: int,
        use_: array[int],
        history: array[float],
        present: float,
        weight: float,
        via_cost: float,
        wrong: float,
        moves: Sequence[tuple[int, int]],
        steps: tuple[float, float],
        turn: Sequence[float],
        own: set[int],
        hard: bool,
        grazes: Grazes | None,
        pad_of: dict[int, int],
        wide: Sequence[tuple[int, int]] | None,
        far: list[array[int]],
        ring: Sequence[tuple[int, int]],
        drilled: set[int],
        hole_ring: Sequence[tuple[int, int]],
    ) -> list[int] | None:
        """One A* search; the arguments are `_astar_py`'s, unpacked."""
        ...

class Arrangement:
    """The drawing annealer's pricing, over the state's own spot arrays."""

    def __init__(
        self,
        forms: list[
            list[
                tuple[
                    tuple[int, int, int, int],
                    list[
                        tuple[
                            int,
                            tuple[int, int],
                            tuple[int, int],
                            tuple[int, int] | None,
                        ]
                    ],
                ]
            ]
        ],
        members: list[list[int]],
        nets_of: list[list[int]],
        x: array[int],
        y: array[int],
        shape: array[int],
        placed: array[int],
        pad: int,
        bend: float,
        crossing: float,
        sideways: float,
        backwards: float,
        gravity: float,
        unwired: float,
    ) -> None: ...
    def fits(self, i: int, x: int, y: int, s: int, ignore: Sequence[int] = ()) -> bool:
        """Whether part i fits at a spot, clear of every placed part's room."""
        ...
    def take(self, i: int) -> None:
        """Hold part i's room where it now stands."""
        ...
    def free(self, i: int) -> None:
        """Let go of part i's room."""
        ...
    def local(self, i: int) -> float:
        """Part i's own cost and its nets'."""
        ...
    def recount(self, parts: list[int]) -> float:
        """Recompute the costs touching some parts; return the change."""
        ...

class Grazer:
    """A board's fixed copper, for measuring diagonal steps against."""

    def __init__(
        self,
        copper: list[
            tuple[
                int,
                int,
                int,
                tuple[float, float, float, float] | None,
                tuple[tuple[float, float], tuple[float, float], float] | None,
                tuple[tuple[float, float], ...] | None,
            ]
        ],
    ) -> None: ...
    def grazes(
        self,
        maps: list[array[int]],
        nx: int,
        ny: int,
        xs: list[float],
        ys: list[float],
        open_cells: set[int],
        need_of: list[float],
        own_clearance: float,
        half: float,
        reach: float,
    ) -> dict[int, tuple[int, ...]]:
        """One class's grazing steps, both ways, and the nets they pass."""
        ...
