"""Whether a KiCad project opens the same on another computer.

A project travels with its own folder and nothing else. Its schematic
carries its symbols, so they always travel; what may not:

- a footprint library the schematic names, unless it is one of KiCad's own
  (a ``.pretty`` folder of that name in KiCad's footprint folder) or the
  project's own table points inside the project folder;
- a 3D model on the board, unless its path starts with ``${KIPRJMOD}`` or
  KiCad's own model variable (``${KICAD<n>_3DMODEL_DIR}``). An absolute
  path, or a variable another computer may not define, does not travel. A
  relative path or another form is reported as unverified: how KiCad
  resolves it was not checked against a KiCad-saved board.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from kairo.library.tables import read_library_table

_MODEL = re.compile(r'\(model\s+"([^"]+)"')
_OWN_MODELS = re.compile(r"^\$\{(KIPRJMOD|KICAD\d+_3DMODEL_DIR)\}")
_ABSOLUTE = re.compile(r"^([A-Za-z]:[\\/]|[\\/])")
#: The most items listed of each kind; the counts stay whole.
_LISTED = 20


@dataclass(frozen=True, slots=True)
class Portability:
    """What a project needs that may not be on another computer.

    Attributes:
        status: ``portable``, ``not_portable``, or ``unverified`` when only
            forms not checked against KiCad stand in the way.
        footprint_libraries: How many footprint libraries the schematic names.
        models: How many 3D-model paths the boards hold.
        missing: Each library or model that does not travel, with why.
        unverified: Each model path whose resolution was not checked.
    """

    status: Literal["portable", "not_portable", "unverified"]
    footprint_libraries: int
    models: int
    missing: tuple[str, ...] = field(default=())
    unverified: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict[str, Any]:
        """The result as plain data, its lists cut to 20 items each."""
        out: dict[str, Any] = {
            "status": self.status,
            "footprint_libraries": self.footprint_libraries,
            "models": self.models,
        }
        if self.missing:
            out["missing"] = list(self.missing[:_LISTED])
            out["missing_count"] = len(self.missing)
        if self.unverified:
            out["unverified"] = list(self.unverified[:_LISTED])
        return out


def check_portability(
    project: Path,
    footprints: Iterable[str],
    stock_footprints: Path,
    variables: Mapping[str, str],
) -> Portability:
    """Check what a project needs against what travels with its folder.

    Args:
        project: The project's folder.
        footprints: The footprints the schematic names, as
            ``<nickname>:<name>``; empty ones are skipped.
        stock_footprints: KiCad's own footprint folder.
        variables: Path variables for the project's table (KiCad's own;
            ``KIPRJMOD`` is set here).

    Returns:
        The libraries and models that do not travel, and the status.
    """
    nicknames = sorted({f.split(":", 1)[0] for f in footprints if ":" in f})
    local: dict[str, Path] = {}
    table = project / "fp-lib-table"
    if table.is_file():
        found, _ = read_library_table(
            table, "footprint", {**variables, "KIPRJMOD": str(project)}
        )
        local = {lib.nickname: lib.path for lib in found}
    missing: list[str] = []
    for nickname in nicknames:
        if nickname in local:
            if not _inside(local[nickname], project):
                missing.append(
                    f"footprint library {nickname}: the project's table points "
                    "outside its folder"
                )
        elif not (stock_footprints / f"{nickname}.pretty").is_dir():
            missing.append(
                f"footprint library {nickname}: not one of KiCad's own and not "
                "in the project's table"
            )
    models: list[str] = []
    for board in sorted(project.glob("*.kicad_pcb")):
        models += _MODEL.findall(board.read_text(encoding="utf-8", errors="replace"))
    unverified: list[str] = []
    for path in sorted(set(models)):
        if _OWN_MODELS.match(path):
            continue
        if _ABSOLUTE.match(path):
            missing.append(f"3D model {path}: an absolute path")
        elif path.startswith(("${", "$(")):
            missing.append(f"3D model {path}: a variable another computer may lack")
        else:
            unverified.append(f"3D model {path}")
    status: Literal["portable", "not_portable", "unverified"] = (
        "not_portable" if missing else "unverified" if unverified else "portable"
    )
    return Portability(
        status, len(nicknames), len(set(models)), tuple(missing), tuple(unverified)
    )


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
    except ValueError:
        return False
    return True
