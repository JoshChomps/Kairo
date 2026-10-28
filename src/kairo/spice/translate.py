"""PSpice model files rewritten so Xyce reads them as ngspice does.

Kairo runs a PSpice model in ngspice's PSpice compatibility mode. For
two simulators to check each other on such a model, Xyce must read the
same file the same way. Xyce 7.10 reads most of PSpice's language as it
stands; this module makes the edits it needs and no others. A construct
not listed is left as written, for Xyce to accept or refuse; a refusal
reaches the result as that run's error.

- A ``TABLE`` source. PSpice and ngspice accept
  ``TABLE {expr} ((x1,y1) (x2,y2))``, with the ``=`` and the outer
  parentheses optional. Measured: Xyce 7.10 refuses the line without
  the ``=`` ("Required "=" sign missing after the expression in the
  TABLE") and refuses the outer parentheses as a syntax error, so it is
  written ``TABLE {expr} = (x1,y1) (x2,y2)``.
- ``IC=`` on a capacitor or an inductor is dropped. ngspice uses a
  device's ``IC=`` only with ``UIC``, which Kairo never gives, so its
  operating point ignores it; Xyce applies it while solving the
  operating point. Measured on TI's LM1117 model, whose soft-start
  capacitor states ``IC=0``: Xyce gives 0.9 mV at the output at rest
  with it and 3.297279 V without it, the value ngspice gives. Whether
  PSpice itself holds the capacitor at its ``IC=`` during the bias point
  is not established here; it decides whether such a model starts at
  rest or ramps up, and it is one reason a converted model counts as
  unchecked until compared with PSpice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: A TABLE's expression followed by its pairs, on one logical line.
_TABLE = re.compile(
    r"(?P<head>\bTABLE\s*\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})\s*(?P<eq>=)?\s*(?P<pairs>\(.*\))\s*$",
    re.IGNORECASE,
)
#: An ``IC=`` value on an element line.
_IC = re.compile(r"\s+IC\s*=\s*\S+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Translation:
    """A model file in Xyce's spelling.

    Attributes:
        text: The rewritten file.
        edits: What was changed, one line each, naming the file's line
            and element.
    """

    text: str
    edits: tuple[str, ...]


def pspice_to_xyce(text: str) -> Translation:
    """Rewrite a PSpice model file for Xyce.

    Args:
        text: The file as its maker wrote it.

    Returns:
        The rewritten file and the list of edits. A file that needs none
        comes back unchanged with no edits.
    """
    lines = text.splitlines()
    out: list[str] = []
    edits: list[str] = []
    index = 0
    while index < len(lines):
        start = index
        group = [lines[index]]
        index += 1
        while index < len(lines) and lines[index].lstrip().startswith("+"):
            group.append(lines[index])
            index += 1
        changed, why = _rewrite(group)
        if why:
            name = group[0].split(maxsplit=1)[0]
            edits += [f"line {start + 1}, {name}: {w}" for w in why]
        out += changed
    ending = "\n" if text.endswith(("\n", "\r")) else ""
    return Translation("\n".join(out) + ending, tuple(edits))


def _rewrite(group: list[str]) -> tuple[list[str], list[str]]:
    """One element, as its physical lines, in Xyce's spelling, and why."""
    first = group[0].lstrip()
    if not first or first[0] in "*.;":
        return group, []
    kind = first[0].upper()
    why: list[str] = []
    if kind in "EG" and "TABLE" in " ".join(group).upper():
        joined = " ".join([group[0], *(line.lstrip()[1:] for line in group[1:])])
        found = _TABLE.search(joined)
        if found is None:
            return group, []
        pairs = found["pairs"].strip()
        inner = pairs[1:-1].strip()
        # The outer parentheses wrap the whole list only if the text
        # inside them is itself a list of parenthesised pairs.
        if inner.startswith("(") and _balanced_list(inner):
            pairs = inner
        elif found["eq"] is not None:
            return group, []
        line = f"{joined[: found.start()]}{found['head']} = {pairs}"
        why.append("TABLE written with '=' and no outer parentheses")
        return [line], why
    if kind in "CL" and _IC.search(" ".join(group)):
        rewritten = [_IC.sub("", line) for line in group]
        why.append("IC= dropped, as ngspice ignores it without UIC")
        return rewritten, why
    return group, why


def _balanced_list(text: str) -> bool:
    """Whether the text is a run of parenthesised groups and nothing else."""
    depth = 0
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
        elif depth == 0 and not char.isspace() and char != ",":
            return False
    return depth == 0
