"""Simulation models from a part's maker, registered so they cannot change unseen.

A models folder holds the model files and ``models.json``, which records
for each one: the part it stands for, the subcircuit to use and which
symbol pin each of its nodes connects to, who published it and where, the
file's SHA-256, the dialect it is written in, the simulators it has been
run on, and its fidelity class. A file whose hash no longer matches its
record is refused until it is registered again, so a result can always
name exactly the model that produced it.

Fidelity classes follow the plan. ``vendor`` is a maker's model run
unchanged in a simulator it was written for. ``vendor-converted`` is one
run through a compatibility mode; until it has been compared with the
simulator it was written for (``conversion_checked``), nothing it
produces counts as checked.

Pins are named, never numbered: each subcircuit node is tied to a symbol
pin by the pin's name in KiCad's library.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from kairo.spice.translate import pspice_to_xyce

#: The registry's file inside a models folder.
INDEX = "models.json"
#: The subcircuit node name that means the circuit's ground.
GROUND_NODE = "0"
#: The subfolder of a models folder that holds PSpice models rewritten
#: for Xyce, each named by the hash of the file it was made from.
XYCE_FOLDER = ".xyce"

Dialect = Literal["spice3", "pspice"]
Fidelity = Literal["vendor", "vendor-converted"]


class RegistryError(ValueError):
    """A model cannot be used: its code says why.

    Attributes:
        code: ``E_MODEL_CHANGED``, ``E_MODEL_MISSING`` or
            ``E_MODEL_REGISTRY``.
    """

    def __init__(self, code: str, message: str) -> None:
        """Keep the code beside the message."""
        super().__init__(f"{code}: {message}")
        self.code = code


class ModelEntry(BaseModel):
    """One registered model.

    Attributes:
        mpn: The part number it models; matched against a part's ``mpn``.
        lib_id: The KiCad symbol it models, for parts with no ``mpn``.
        file: The model file, relative to the models folder.
        sha256: The file's hash when it was registered.
        subckt: The subcircuit to instantiate.
        nodes: For each node of the subcircuit, in order, the name of the
            symbol pin it connects to, or ``0`` for ground.
        dialect: ``pspice`` for a model that needs PSpice compatibility,
            ``spice3`` for one any SPICE reads.
        engines: The simulators it has been run on and converges in.
        fidelity: ``vendor`` or ``vendor-converted``.
        conversion_checked: For a converted model, whether its results
            have been compared with the simulator it was written for.
        manufacturer: Who published it.
        source: Where it was obtained, as a URL or document number.
        version: The model's own version or date.
        covers: What the maker says it represents, in the maker's terms.
    """

    model_config = ConfigDict(extra="forbid")

    mpn: str | None = None
    lib_id: str | None = None
    file: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    subckt: str = Field(min_length=1)
    nodes: list[str] = Field(min_length=1)
    dialect: Dialect
    engines: list[Literal["ngspice", "xyce"]] = Field(min_length=1)
    fidelity: Fidelity
    conversion_checked: bool = False
    manufacturer: str = Field(min_length=2)
    source: str = Field(min_length=4)
    version: str = Field(min_length=1)
    covers: str = Field(min_length=4)

    @model_validator(mode="after")
    def _names_a_part(self) -> ModelEntry:
        if self.mpn is None and self.lib_id is None:
            raise ValueError("a model names the part it is for, by mpn or lib_id")
        if self.fidelity == "vendor" and self.dialect == "pspice":
            raise ValueError(
                "a PSpice model run in ngspice or Xyce is vendor-converted, not vendor"
            )
        return self

    def cite(self) -> str:
        """The model's source in one line, for a result."""
        return f"{self.manufacturer}, {self.source}, {self.version}"


def file_sha256(path: Path) -> str:
    """A file's SHA-256, as lower-case hex.

    Args:
        path: The file.

    Returns:
        The hash.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ModelRegistry:
    """The models in one folder.

    Args:
        folder: The models folder. It need not exist; an absent folder
            has no models.
    """

    def __init__(self, folder: Path) -> None:
        """Remember the folder; the index is read on each lookup."""
        self.folder = folder

    def entries(self) -> list[ModelEntry]:
        """Every registered model.

        Returns:
            The entries, in file order; empty if there is no index.

        Raises:
            RegistryError: ``E_MODEL_REGISTRY`` if the index does not parse.
        """
        index = self.folder / INDEX
        if not index.is_file():
            return []
        try:
            data = json.loads(index.read_text(encoding="utf-8"))
            return [ModelEntry.model_validate(item) for item in data["models"]]
        except (OSError, ValueError, KeyError, TypeError, ValidationError) as exc:
            raise RegistryError("E_MODEL_REGISTRY", f"{index}: {exc}") from exc

    def find(self, *, mpn: str | None, lib_id: str) -> ModelEntry | None:
        """The model for a part, with its file checked against its record.

        A part number is matched first; a symbol only when the part has
        no model by number.

        Args:
            mpn: The part's number, if the design gives one.
            lib_id: The part's symbol.

        Returns:
            The entry, or ``None`` if no model is registered for the part.

        Raises:
            RegistryError: ``E_MODEL_MISSING`` if the entry's file is gone,
                ``E_MODEL_CHANGED`` if its hash differs from the record.
        """
        entries = self.entries()
        entry = next((e for e in entries if mpn is not None and e.mpn == mpn), None)
        if entry is None:
            entry = next(
                (e for e in entries if e.mpn is None and e.lib_id == lib_id), None
            )
        if entry is None:
            return None
        path = self.path(entry)
        if not path.is_file():
            raise RegistryError(
                "E_MODEL_MISSING",
                f"{entry.file} is registered and is not in {self.folder}",
            )
        actual = file_sha256(path)
        if actual != entry.sha256:
            raise RegistryError(
                "E_MODEL_CHANGED",
                f"{entry.file} has changed since it was registered (its hash is "
                f"{actual[:12]}..., the record has {entry.sha256[:12]}...). Check "
                "the file and register it again",
            )
        return entry

    def path(self, entry: ModelEntry) -> Path:
        """Where an entry's file is.

        Args:
            entry: The entry.

        Returns:
            The absolute path.
        """
        return (self.folder / entry.file).resolve()

    def for_xyce(self, entry: ModelEntry) -> tuple[Path, tuple[str, ...]]:
        """The file Xyce reads for an entry, and the edits that made it.

        A ``spice3`` model is read as it is. A ``pspice`` one is rewritten
        by `pspice_to_xyce` into the folder's `XYCE_FOLDER`, named by the
        original's hash, so a changed original can never be read through
        an old copy.

        Args:
            entry: An entry `find` returned, so its file matches its hash.

        Returns:
            The path, and the edits (empty for a file read as it is).

        Raises:
            RegistryError: ``E_MODEL_REGISTRY`` if the copy cannot be
                written.
        """
        path = self.path(entry)
        if entry.dialect != "pspice":
            return path, ()
        made = pspice_to_xyce(path.read_text(encoding="latin-1"))
        if not made.edits:
            return path, ()
        copy = (self.folder / XYCE_FOLDER / f"{entry.sha256}.lib").resolve()
        try:
            if not copy.is_file() or copy.read_text(encoding="latin-1") != made.text:
                copy.parent.mkdir(parents=True, exist_ok=True)
                partial = copy.with_suffix(".partial")
                partial.write_text(made.text, encoding="latin-1", newline="\n")
                partial.replace(copy)
        except OSError as exc:
            raise RegistryError(
                "E_MODEL_REGISTRY", f"cannot write Xyce's copy of {entry.file}: {exc}"
            ) from exc
        return copy, made.edits

    def register(self, entry_without_hash: dict[str, object]) -> ModelEntry:
        """Record a model file, replacing any record for the same part.

        Args:
            entry_without_hash: The entry's fields except ``sha256``,
                which is computed from the file as it is now.

        Returns:
            The entry as recorded.

        Raises:
            RegistryError: ``E_MODEL_MISSING`` if the file is not in the
                folder, ``E_MODEL_REGISTRY`` if the fields are not valid
                or the file does not define the subcircuit with that
                many nodes.
        """
        name = str(entry_without_hash.get("file", ""))
        path = self.folder / name
        if not name or not path.is_file():
            raise RegistryError("E_MODEL_MISSING", f"{name!r} is not in {self.folder}")
        try:
            entry = ModelEntry.model_validate(
                {**entry_without_hash, "sha256": file_sha256(path)}
            )
        except ValidationError as exc:
            raise RegistryError("E_MODEL_REGISTRY", str(exc)) from exc
        declared = subckt_nodes(path.read_text(encoding="latin-1"), entry.subckt)
        if declared is None:
            raise RegistryError(
                "E_MODEL_REGISTRY", f"{name} defines no subcircuit {entry.subckt}"
            )
        if len(declared) != len(entry.nodes):
            raise RegistryError(
                "E_MODEL_REGISTRY",
                f"{entry.subckt} has {len(declared)} nodes ({' '.join(declared)}) "
                f"and {len(entry.nodes)} pins were given",
            )
        kept = [
            e
            for e in self.entries()
            if (e.mpn, e.lib_id if e.mpn is None else None)
            != (entry.mpn, entry.lib_id if entry.mpn is None else None)
        ]
        kept.append(entry)
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / INDEX).write_text(
            json.dumps({"models": [e.model_dump() for e in kept]}, indent=1) + "\n",
            encoding="utf-8",
        )
        return entry


def subckt_nodes(text: str, name: str) -> list[str] | None:
    """The node names a model file declares for a subcircuit.

    Args:
        text: The model file's text.
        name: The subcircuit's name; case does not matter.

    Returns:
        The nodes in order, without any parameters, or ``None`` if the
        file has no such subcircuit.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        fields = line.split()
        if len(fields) < 2 or fields[0].lower() != ".subckt":
            continue
        if fields[1].lower() != name.lower():
            continue
        # A declaration can run on over continuation lines.
        rest = fields[2:]
        for more in lines[index + 1 :]:
            if not more.startswith("+"):
                break
            rest += more[1:].split()
        nodes: list[str] = []
        for field in rest:
            if field.upper().startswith("PARAMS:") or "=" in field:
                break
            nodes.append(field)
        return nodes
    return None


def main(argv: list[str] | None = None) -> int:
    """Register a model from the command line.

    ``python -m kairo.spice.registry MODELS_DIR FACTS.json`` records the
    model file named in the facts (which must already be in the models
    folder) with its hash as it is now.

    Args:
        argv: The arguments; the process's if ``None``.

    Returns:
        0 on success, 2 on a wrong call or a refused registration.
    """
    import sys

    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: python -m kairo.spice.registry MODELS_DIR FACTS.json")
        return 2
    try:
        facts = json.loads(Path(args[1]).read_text(encoding="utf-8"))
        entry = ModelRegistry(Path(args[0])).register(facts)
    except (OSError, ValueError) as exc:
        print(exc)
        return 2
    part = entry.mpn or entry.lib_id
    print(f"registered {entry.file} ({entry.sha256[:12]}...) for {part}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
