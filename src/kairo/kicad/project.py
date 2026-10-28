"""Read and write KiCad project files (``.kicad_pro``) without reformatting them.

A ``.kicad_pro`` is JSON. Measured on every project file in a KiCad 10.0.6
install (56 files, 2026-10-06): KiCad writes it exactly as Python's
``json.dumps(data, indent=2, ensure_ascii=False)`` does, followed by one
line ending, with CRLF line endings on Windows. So a project file can be
loaded as plain data and printed back byte for byte.

Kairo relies on that only after checking it: `parse_project` prints the
data it just read and refuses the file if the result is not the original
text. A file KiCad formats some other way is then reported instead of being
quietly reformatted on the next write.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast


class ProjectFormatError(ValueError):
    """A project file is not JSON, or Kairo cannot print it back exactly."""


@dataclass(slots=True)
class ProjectFile:
    """A ``.kicad_pro`` file's settings and the line ending it uses.

    Attributes:
        data: The settings, as JSON objects, lists and values.
        newline: The line ending, CRLF or LF, as in the file read.
    """

    data: dict[str, Any]
    newline: str = "\n"

    def to_text(self) -> str:
        """Print the settings the way KiCad writes them."""
        text = json.dumps(self.data, indent=2, ensure_ascii=False) + "\n"
        return text.replace("\n", self.newline) if self.newline != "\n" else text


def parse_project(text: str) -> ProjectFile:
    """Read a project file, checking it will print back unchanged.

    Args:
        text: The file's contents, with line endings untouched.

    Returns:
        The settings and line ending; ``parse_project(t).to_text() == t``.

    Raises:
        ProjectFormatError: If the text is not a JSON object, mixes line
            endings, or would not print back byte for byte.
    """
    has_crlf = "\r\n" in text
    newline = "\r\n" if has_crlf else "\n"
    if has_crlf and text.count("\n") != text.count("\r\n"):
        raise ProjectFormatError("project file mixes CRLF and LF line endings")
    try:
        data: object = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProjectFormatError(f"project file is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ProjectFormatError("project file is not a JSON object")
    project = ProjectFile(cast("dict[str, Any]", data), newline)
    if project.to_text() != text:
        raise ProjectFormatError(
            "project file is not laid out the way KiCad writes it, so Kairo "
            "could not save it without reformatting"
        )
    return project
