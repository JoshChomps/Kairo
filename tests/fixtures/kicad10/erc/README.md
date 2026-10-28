# ERC report fixtures

Real ERC reports written by `kicad-cli` 10.0.6 on 2026-10-06, so the ERC
classifier is tested against what KiCad actually writes. The classifier's
tests for malformed reports start from these files and break them in one
place each.

Each report was made on a copy of one of the demo projects that ship with
KiCad (`share/kicad/demos/<name>` in the install), run from the project
folder with:

```bash
kicad-cli sch erc --format json --severity-all --units mm --exit-code-violations -o <name>.json <schematic>
```

| File | Demo | Exit code | Sheets | What it has |
|---|---|--:|--:|---|
| `pic_programmer.json` | pic_programmer | 0 | 2 | No violations |
| `complex_hierarchy.json` | complex_hierarchy | 0 | 3 | No violations |
| `interf_u.json` | interf_u | 5 | 1 | 1 warning (`footprint_link_issues`) |
| `sonde_xilinx.json` | sonde xilinx | 5 | 1 | 2 warnings |
| `video.json` | video | 5 | 8 | 3 warnings |
| `multichannel.json` | multichannel | 5 | 5 | 4 errors (`power_pin_not_driven`, `pin_not_connected`) and 32 warnings |

The footprint warnings depend on the footprint libraries installed on the
machine that ran ERC, so a fresh run elsewhere can list different
warnings. That is why these are saved files and not regenerated in tests.

## Reports from real boards

No demo has excluded (waived) violations, so three more reports were made
the same way on 2026-10-08, on unchanged copies of real projects:

| File | Project | Exit code | Sheets | What it has |
|---|---|--:|--:|---|
| `artix-dc-scm.json` | `tests/fixtures/real/antmicro-artix-dc-scm` | 5 | 11 | 1413 warnings and 15 excluded warnings |
| `polarfire-som.json` | `tests/fixtures/real/antmicro-polarfire-som` | 5 | 18 | 1331 warnings and 19 excluded violations, one of them with an empty severity because its check (`pin_to_pin`) is set to ignore in the project |
| `baseboard_4slots.json` | The 4-slot baseboard of <https://github.com/OpenLightingProject/rp2040-dmxsun> at `f3b1749c982da85770dc9a11eada035404edc25d` (Apache-2.0; a KiCad 8 project that is not itself in this repository) | 5 | 1 | 1 error, 221 warnings, and 2 excluded errors and 1 excluded warning |

They show three things about what kicad-cli 10.0.6 writes:

- An excluded violation keeps its severity and gains `"excluded": true`.
- A violation of a check the project has set to ignore is still listed
  when it is also excluded, with `"severity": ""`.
- Item positions are one hundredth of the schematic's, although the report
  says its units are mm: the global label `ETH_~{RESET}` in
  `artix-dc-scm` is at (189.23, 121.92) mm in `ethernet.kicad_sch` and at
  (1.8923, 1.2192) in the report. Checked on every reported item that
  states its own position in four of the real boards (61 labels and bus
  entries): each is exactly one hundredth, in both axes. Lengths inside
  descriptions are affected too (`Vertical Bus, length 0.0254 mm` for a
  2.54 mm bus). The text report (`--format report`) of the same run gives
  the true values. This is KiCad issue 25582, listed as fixed in the
  10.0.7 release notes.

Still not measured: kicad-cli's exit code when every violation in a
report is excluded.
