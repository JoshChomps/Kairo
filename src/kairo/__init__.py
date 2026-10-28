"""Kairo turns a described circuit into a checked KiCad schematic.

Kairo runs as an MCP server. A language model describes the circuit; Kairo
resolves every pin against KiCad's own libraries, writes the schematic, and
checks each voltage and current twice, once by formula and once by
simulation.

Attributes:
    __version__: The installed package version, read from its metadata so it
        cannot drift from ``pyproject.toml``.
"""

from importlib.metadata import version

__version__ = version("kairo-eda")
