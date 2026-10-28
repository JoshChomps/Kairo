# Kairo

Kairo is an MCP server that turns a described circuit into a KiCad schematic
you can open, review and build on. A language model describes the circuit;
Kairo resolves every pin against KiCad's own libraries, writes the
schematic, and checks each voltage and current twice: once with formulas
whose sources are cited, and once by simulation with ngspice and Xyce. Every
number comes with what it depends on and how far it can be trusted, and
anything Kairo could not check is marked unverified rather than guessed.

Kairo designs and checks schematics, simulates them, reviews drawn
projects, and makes boards with its own router, judged by KiCad's own
checks. It is young: what it cannot do yet is said where it applies.

```{toctree}
:maxdepth: 2

getting-started
text-placement
board-layout
router-design
router-log
research
reference/index
development
assumptions
changelog
```
