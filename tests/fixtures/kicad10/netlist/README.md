# Netlist fixture

`mikoto.net` is the netlist `kicad-cli` 10.0.6 wrote on 2026-10-08 for the
mikoto board in `tests/fixtures/real/zhiayang-mikoto` (Apache-2.0, see
that folder's `LICENSE`), so the netlist reader is tested against what
KiCad actually writes. It was made on an unchanged copy of the project
with:

```bash
kicad-cli sch export netlist --format kicadsexpr --output mikoto.net mikoto.kicad_sch
```

The `(source ...)` line holds the temporary folder the copy was in.
