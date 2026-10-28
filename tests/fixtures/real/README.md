# Real-board fixtures

Sixteen open-hardware KiCad projects, kept so Kairo is tested on boards
people designed to be built, not only on KiCad's demos and hand-written
snippets. Each folder holds the project's `LICENSE` and a `tree/` with its
files, byte for byte as published. No design file was altered.

## Saved by KiCad 9 and 10

Twelve projects taken straight from their own repositories on 2026-10-08,
at the commits shown. Their schematics were last saved by the KiCad
program itself (`generator "eeschema"`), in formats Kairo reads: 20250114
(KiCad 9) or 20260306 (KiCad 10). Only the schematic side was copied:
schematic sheets, the project file, the project's own symbol libraries,
library tables and worksheet files. Boards, footprints, 3D models,
documentation and everything else were left out.

| Folder | Project | Upstream | Commit | Licence | Sheets | Saved by |
|---|---|---|---|---|--:|---|
| `antmicro-artix-dc-scm` | Artix DC-SCM. Copyright (c) 2020-2025 Antmicro | <https://github.com/antmicro/artix-dc-scm> | `b9856f78179fce0739a4955dd4ed5f912619efe6` | Apache-2.0 | 11 | KiCad 9 |
| `antmicro-com-express-7-baseboard` | COM Express 7 Baseboard. Copyright (c) 2024-2025 Antmicro | <https://github.com/antmicro/com-express-7-baseboard> | `4811fd8491dbe0cbbb738e127d3f7d1d6c1f8126` | Apache-2.0 | 18 | KiCad 9 |
| `antmicro-jetson-nano-baseboard` | Jetson Nano Baseboard. Copyright (c) 2020-2025 Antmicro | <https://github.com/antmicro/jetson-nano-baseboard> | `d8d0b2d71ab4cc4bcdb204a09f78be40d4c8cda3` | Apache-2.0 | 8 | KiCad 9 |
| `antmicro-osm-l-bmc-som` | OSM-L System on Module for BMC. Copyright (c) 2025 Antmicro | <https://github.com/antmicro/osm-l-bmc-som> | `241e7d299ee2facb4149a8df5f21f0102b09e6a9` | Apache-2.0 | 31 | KiCad 9 |
| `antmicro-polarfire-som` | PolarFire SoM. Copyright (c) 2023-2025 Antmicro | <https://github.com/antmicro/polarfire-som> | `1c787f6b8bb91cfc65c87a11866b2d13cb3f11a0` | Apache-2.0 | 18 | KiCad 9 |
| `antmicro-sodimm-ddr5-tester` | SO-DIMM DDR5 Tester. Copyright (c) 2023-2025 Antmicro | <https://github.com/antmicro/sodimm-ddr5-tester> | `05b265a75b87d63862323082bfdcb62949d22647` | Apache-2.0 | 17 | KiCad 9 |
| `greatscottgadgets-hackrf-pro` | HackRF Pro. Copyright (c) 2012-2024 Great Scott Gadgets | <https://github.com/greatscottgadgets/hackrf-pro> | `359b98f2451395408bc25dae64efa9fc518e93d0` | CERN-OHL-P-2.0 | 12 | KiCad 9 |
| `sanctuary-rpi-cm5io-mini-itx` | rpi-cm5io-mini-itx, by Sanctuary Systems (the upstream states no copyright line) | <https://github.com/sanctuary-systems-com/rpi-cm5io-mini-itx> | `5edf50af1ab07099f74da347a96993baa5d759bc` | CERN-OHL-P-2.0 | 4 | KiCad 10 |
| `lsartory-clavier` | Clavier keyboard, by lsartory; the repository's `PCB` folder (the upstream states no copyright line) | <https://github.com/lsartory/Clavier> | `dbef00bbb8a9bff171d2a72315ee102eec42e3fd` | CERN-OHL-P-2.0 | 13 | KiCad 9 |
| `craigg96-vesc6-open-hardware` | VESC6 Open Hardware motor controller. Copyright (c) 2025 craigg96 | <https://github.com/craigg96/VESC6_OPEN_HARDWARE> | `1a11171913bd54e77269862e8c9f013df70e8481` | MIT | 6 | KiCad 9 |
| `pedalboard-pedalboard-hw` | Pedalboard hardware, by the pedalboard project (the upstream states no copyright line) | <https://github.com/pedalboard/pedalboard-hw> | `3850a8c2391c59a8afc3a21d9ad2627a5685ea24` | CERN-OHL-P-2.0 | 6 | KiCad 10 |
| `git4dcc-rtb-c13` | RTB C13 model-railway decoder, by git4dcc (the upstream states no copyright line) | <https://github.com/git4dcc/RTB_C13> | `731f7d53c9d30c1811da7abf7a2dbee91a61214f` | Apache-2.0 | 5 | KiCad 9 |

A project's sheet count is the number of `.kicad_sch` files; a project may
use a file more than once or not at all (the OSM-L project's netlist has
20 sheets).

## Saved by KiCad 8, by way of CopperBench

Four projects taken from the fixture set of
[CopperBench](https://github.com/copperheadhq/copperbench) (Apache-2.0) at
commit `09863d77aa87b86c8764db73964b256c7ce8b020` on 2026-10-08. CopperBench
copied each project's design files unchanged from the upstream commit
shown below; its notes on what it left out are in its own
`fixtures/<name>/fixture.json`.

| Folder | Project | Upstream | Commit | Licence |
|---|---|---|---|---|
| `antmicro-jetson-orin-baseboard` | Antmicro Jetson Orin Baseboard, ten schematic sheets. Copyright (c) 2022-2026 Antmicro | <https://github.com/antmicro/jetson-orin-baseboard> | `34f0e7f5fbca441191e20ed55d93cf8614e12bed` | Apache-2.0 |
| `antmicro-microphone-board` | Antmicro PDM Microphone Board. Copyright (c) 2024-2025 Antmicro | <https://github.com/antmicro/pdm-microphone-board> | `20813e2bc99f52837a780334b16a30c2afb7644c` | Apache-2.0 |
| `openlighting-dmxsun-baseboard` | Open Lighting Project rp2040-dmxsun, 2-slot baseboard | <https://github.com/OpenLightingProject/rp2040-dmxsun> | `f3b1749c982da85770dc9a11eada035404edc25d` | Apache-2.0 |
| `zhiayang-mikoto` | mikoto nRF52840 module, by zhiayang | <https://github.com/zhiayang/mikoto> | `86318f6c69495b8308354b6393d85229177b26ae` | Apache-2.0 |

These four also hold `baseline/erc.json`: the ERC report CopperBench
recorded with kicad-cli 10.0.6 on the unchanged project. The warnings in
it come mostly from libraries a bare checkout cannot find, not from the
designs. Unlike the eight above, these keep their boards and footprints,
except the routed Jetson Orin board (`jetson-orin-baseboard.kicad_pcb`,
35 MB), which no test reads yet. CopperBench's own files (`fixture.json`,
`README.md`, `baseline/drc.json` and `baseline/parity.json`) are not
copied.

All four were last saved by KiCad 8 (schematic format 20231120, board
format 20240108). Kairo round-trips them byte for byte but refuses to
treat them as schematics it can edit, because it reads only formats
written by KiCad 9 and 10. The KiCad-backed tests re-save a temporary copy
with `kicad-cli sch upgrade --force` and check that copy too.

## What is still missing

The ERC report saved from the KiCad program's own window for each
project. kicad-cli and the window can disagree,
and only a person at KiCad can save the window's report.
