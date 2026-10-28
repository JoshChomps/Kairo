"""The facts a person has stated about their own project, kept with it.

A drawn schematic says which parts connect to which, not which net is a
supply, what a load draws or what a datasheet gives. Those facts are
stated once and kept in the project's own ``kairo`` folder, beside the
backups Kairo makes there, so they travel with the project and need not
be stated again on every call. Kairo's own data (models, the parts store,
datasheets, the library index) is not one project's and stays in Kairo's
folder.

A call's facts are laid over the kept ones: a net or a part named in the
call replaces what is kept for it, except that a part's ``params`` are
merged figure by figure; ``loads`` and ``requirements``, given at all,
replace the kept lists; ``policy`` is merged key by key.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

#: The file, within the project folder.
FACTS_FILE = Path("kairo") / "facts.json"


class FactsFileError(ValueError):
    """The kept facts cannot be read; the message starts with its code."""


def load_facts(folder: Path) -> dict[str, Any]:
    """The facts kept for a project.

    Args:
        folder: The project folder.

    Returns:
        The facts, or an empty mapping if none are kept.

    Raises:
        FactsFileError: ``E_FACTS_FILE`` if the file is not a JSON object.
    """
    path = folder / FACTS_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FactsFileError(f"E_FACTS_FILE: {path} cannot be read: {exc}") from exc
    if not isinstance(data, dict):
        raise FactsFileError(f"E_FACTS_FILE: {path} does not hold a JSON object")
    return _copy(data)


def laid_over(kept: Mapping[str, Any], given: Mapping[str, Any]) -> dict[str, Any]:
    """A call's facts laid over the kept ones, as the module describes.

    Args:
        kept: The facts kept with the project.
        given: The facts the call states.

    Returns:
        The facts to use. Neither argument is changed.
    """
    out: dict[str, Any] = _copy(dict(kept))
    for key, value in _copy(dict(given)).items():
        old, new = _mapping(out.get(key)), _mapping(value)
        if old is None or new is None or key not in ("nets", "components", "policy"):
            out[key] = value
            continue
        if key != "components":
            out[key] = {**old, **new}
            continue
        parts = dict(old)
        for ref, stated in new.items():
            before, now = _mapping(parts.get(ref)), _mapping(stated)
            if before is None or now is None:
                parts[ref] = stated
                continue
            merged = {**before, **now}
            params_before = _mapping(before.get("params"))
            params_now = _mapping(now.get("params"))
            if params_before is not None and params_now is not None:
                merged["params"] = {**params_before, **params_now}
            parts[ref] = merged
        out[key] = parts
    return out


def _mapping(value: Any) -> dict[str, Any] | None:
    """The value as a mapping of names, or ``None`` if it is not one."""
    if not isinstance(value, dict):
        return None
    found: dict[str, Any] = _copy(value)
    return found


def facts_text(facts: Mapping[str, Any]) -> str:
    """The facts as the file holds them: JSON, keys sorted, one per line."""
    return json.dumps(facts, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def _copy(value: Any) -> Any:
    """A deep copy of plain JSON data."""
    return json.loads(json.dumps(value))
