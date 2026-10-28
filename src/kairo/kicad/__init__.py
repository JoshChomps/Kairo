"""Everything that reads, writes or runs KiCad.

- `kairo.kicad.sexpr`: lossless S-expression parsing and printing.
- `kairo.kicad.formats`: which file-format versions Kairo reads.
- `kairo.kicad.project`: ``.kicad_pro`` project files.
- `kairo.kicad.cli`: running ``kicad-cli`` for ERC and netlist export.

All writes to a user's project will go through one transaction module.
"""
