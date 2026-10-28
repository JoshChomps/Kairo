"""Read the raw files ngspice and Xyce write.

Both simulators write the SPICE3 raw format: a text header naming the
variables, then the values, either as text (``Values:``) or as 8-byte
floats in the machine's byte order (``Binary:``). Complex data, from an AC
analysis, is two floats a value. Measured on ngspice 47 and Xyce 7.10; the
two differ only in how they name variables, which `_name` evens out.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass


class RawError(ValueError):
    """A raw file could not be read."""


@dataclass(frozen=True, slots=True)
class Plot:
    """One analysis's results.

    Attributes:
        name: The analysis, as the simulator names it, for example
            ``Transient Analysis``.
        is_complex: True for AC results.
        variables: The variable names in file order, lower case, in the
            form ``v(node)`` or ``i(source)``; the first is the sweep
            (``time``, ``frequency``, or ``sweep`` for a DC sweep's source
            value) when the analysis has one.
        kinds: What each variable is (``voltage``, ``current``, ``time``,
            ``frequency``), by name.
        values: Each variable's values, by name.
    """

    name: str
    is_complex: bool
    variables: tuple[str, ...]
    kinds: dict[str, str]
    values: dict[str, tuple[complex, ...]]

    def real(self, variable: str) -> tuple[float, ...]:
        """One variable's values as real numbers.

        Args:
            variable: Its name, such as ``v(n_out)``.

        Returns:
            The real parts. For a real analysis these are the values.

        Raises:
            KeyError: If the plot has no such variable.
        """
        return tuple(value.real for value in self.values[variable.lower()])


_BRANCH = re.compile(r"^(?P<name>.+)#branch$")


def _name(raw: str, kind: str) -> str:
    """One spelling for a variable, whichever simulator wrote it.

    ngspice writes ``v(n_out)`` and ``i(v1)``; Xyce writes ``N_OUT`` and
    ``V1#branch``.
    """
    name = raw.strip().lower()
    if name == "v(v-sweep)":
        # ngspice's name for the swept source's value in a DC sweep.
        return "sweep"
    if name in ("time", "frequency", "sweep") or name.startswith(("v(", "i(")):
        return name
    branch = _BRANCH.match(name)
    if branch is not None:
        return f"i({branch['name']})"
    return f"i({name})" if kind == "current" else f"v({name})"


def read_raw(data: bytes) -> list[Plot]:
    """Read every plot in a raw file.

    Args:
        data: The file's bytes.

    Returns:
        The plots in file order.

    Raises:
        RawError: If the file is cut short or is not a raw file.
    """
    plots: list[Plot] = []
    position = 0
    while position < len(data) and data[position:].strip():
        plot, position = _read_plot(data, position)
        plots.append(plot)
    if not plots:
        raise RawError("the raw file holds no results")
    return plots


def _read_plot(data: bytes, start: int) -> tuple[Plot, int]:
    """Read one plot starting at a byte offset; also give where it ends."""
    header: dict[str, str] = {}
    names: list[str] = []
    kinds: list[str] = []
    position = start
    in_variables = False
    binary: bool | None = None
    while position < len(data):
        end = data.find(b"\n", position)
        if end < 0:
            raise RawError("the raw file's header is cut short")
        line = data[position:end].decode("utf-8", errors="replace").rstrip("\r")
        position = end + 1
        if line.startswith("Binary:"):
            binary = True
            break
        if line.startswith("Values:"):
            binary = False
            break
        if in_variables:
            fields = line.split()
            if len(fields) < 3:
                raise RawError(f"cannot read the variable line {line!r}")
            names.append(fields[1])
            kinds.append(fields[2])
        elif line.startswith("Variables:"):
            in_variables = True
        elif ":" in line:
            key, _, value = line.partition(":")
            header[key.strip()] = value.strip()
    if binary is None:
        raise RawError("the raw file has no values")
    try:
        count = int(header["No. Variables"])
    except (KeyError, ValueError) as exc:
        raise RawError("the raw file does not say how many variables it has") from exc
    if count != len(names) or count == 0:
        raise RawError(f"the header promises {count} variables and lists {len(names)}")
    is_complex = "complex" in header.get("Flags", "")
    width = 2 if is_complex else 1
    # Xyce leaves the point count blank when it stops early, so the count
    # is taken from the data when the header does not give it.
    promised = header.get("No. Points", "")
    points = int(promised) if promised.isdigit() else None

    if binary:
        size = 8 * width * count
        if points is None:
            points = (len(data) - position) // size
        need = points * size
        if position + need > len(data):
            raise RawError("the raw file's values are cut short")
        flat = struct.unpack(f"={need // 8}d", data[position : position + need])
        position += need
    else:
        flat, position = _read_text_values(data, position, points, count, width)
        points = len(flat) // (count * width)

    variables = tuple(_name(n, k) for n, k in zip(names, kinds, strict=True))
    if len(set(variables)) != len(variables):
        raise RawError("two variables in the raw file have the same name")
    columns: dict[str, tuple[complex, ...]] = {}
    for index, variable in enumerate(variables):
        if is_complex:
            reals = flat[2 * index :: 2 * count]
            imags = flat[2 * index + 1 :: 2 * count]
            columns[variable] = tuple(
                complex(r, i) for r, i in zip(reals, imags, strict=True)
            )
        else:
            columns[variable] = tuple(complex(v) for v in flat[index::count])
    return (
        Plot(
            header.get("Plotname", ""),
            is_complex,
            variables,
            dict(zip(variables, kinds, strict=True)),
            columns,
        ),
        position,
    )


def _read_text_values(
    data: bytes, position: int, points: int | None, count: int, width: int
) -> tuple[tuple[float, ...], int]:
    """Read text values up to the next plot's header or the end of the file."""
    end = data.find(b"Title:", position)
    if end < 0:
        end = len(data)
    text = data[position:end].decode("utf-8", errors="replace")
    numbers: list[float] = []
    per_point = count * width
    for line in text.splitlines():
        fields = line.replace(",", " ").split()
        if not fields:
            continue
        # The first line of each point starts with the point's index.
        if len(numbers) % per_point == 0:
            fields = fields[1:]
        try:
            numbers.extend(float(field) for field in fields)
        except ValueError as exc:
            raise RawError(f"cannot read the value line {line!r}") from exc
    if len(numbers) % per_point != 0:
        raise RawError("the raw file's values are cut short")
    if points is not None and len(numbers) != points * per_point:
        raise RawError(
            f"the header promises {points} points and the file holds "
            f"{len(numbers) // per_point}"
        )
    return tuple(numbers), end
