# Research

How Kairo was researched, and what it took from others. Nothing on this
page is copied: each idea is described in our own words, and Kairo's
version of it was designed from Kairo's own needs and code.

## How

- **Published material first.** A project's README, documentation, tool
  descriptions and issue tracker; for a closed product, its public pages.
  Where a project's licence allows it and the owner chose to, its source
  was read for how it works, and its code was still never copied: code
  reused from anywhere would be listed in [NOTICE](../NOTICE)
  under "Borrowed code", which is empty.
- **Measure, do not trust.** Outside tools (Freerouting, KiCad itself)
  were judged by running them on Kairo's own boards and measuring what
  they produced, not by their claims.
- **Ideas get their own design.** An idea is written down in a sentence
  or two, then built from Kairo's own names, data and sources; where it
  did not fit, the idea was changed, not the shape copied.
- **Every number has a source.** Engineering rules come from datasheets,
  standards and textbooks, cited where they are used, never from another
  tool's code.

## Credits

| From | What Kairo took | Where it went |
|---|---|---|
| Konnect, by mixelpixx (AGPL-3.0); its documentation and, later, its source, read for ideas | Report which build is serving; a call log the model can read; review checks that something is present (a bulk capacitor where a supply comes in, a reset or enable input with nothing setting it) | `server_info`, `recent_calls`, `S_SUPPLY_IN_NO_BULK`, `S_CONTROL_INPUT_UNSET` |
| HeyPCB's MCP proxy (MIT) | A refusal that says what to do next and whether to retry; progress on long calls; a bounded project search | `kairo.refusal`, progress notices, `list_projects` |
| CopperBench, by Chouhan Industries (Apache-2.0) | Hashing a fixture tree to prove it unchanged; four of its open-hardware fixture boards | `kairo.hashing`, `tests/fixtures/real` |
| KiCadRoutingTools, by drandyhaas (MIT) | Compile only the inner loops of a Python router and keep the policy in Python; test every router change on real boards | `rust/kairo_fast`, the routing bench |
| KiStack (MIT) and Backplane (MIT) | Place parts by their job, in order of how little distance each connection tolerates; keep room in front of fine-pitch pins | Board placement (in part; the rest is planned) |
| The Refdes Spring Solver prototype (tscircuit) | Price candidate spots for a reference label exactly, then choose | Text placement |
| "Top Fifteen Mistakes People Make When Designing Prototype PCBs", Cosplay Light and Sound (YouTube) | On a prototype, test points on the rails, an indicator LED on each regulated rail, a link to open between a regulator and its loads and spare I/O brought out; checking that a UART's transmit and receive lines cross | `policy.prototype`, `R_UART_TX_TO_TX`, `R_UART_RX_TO_RX` |
| kicad_localizer, by AndreCox (no licence, so its idea only) | Check that a project's libraries and 3D models travel with its folder | `project_summary`'s `portability` |
| EEBench's published methodology | Grading a design by simulation, with held-out tasks never read during development | The eval harness in `evals/` |
| TraceMaker (GPL-3.0-or-later); its write-ups only | Negotiated congestion, a deterministic portfolio of strategies, work budgets | Text placement and the router |
| PathFinder (McMurchie and Ebeling, FPGA 1995), Altium's Situs documentation, TopoR and KiCad router talks | Negotiated congestion: let nets share, then raise the price of what they share until they stop | Kairo's router (`docs/router-design.md`) |

Studied and not taken: Topola (a topological router in Rust, MIT), a
different router from Kairo's rather than a faster one; kicad-mcp-pro
(MIT), whose estimates state their method and accuracy as Kairo's
calculations do; and commercial tools (Flux, Quilter, Diode, CopperPilot)
from their public pages.

The sixteen open-hardware boards used as test data are listed, each with
its licence and upstream commit, in NOTICE.
