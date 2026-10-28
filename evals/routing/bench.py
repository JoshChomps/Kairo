"""Route a frozen case with Kairo's router, audit it and record the result.

    uv run python evals/routing/bench.py "what changed" [--case NAME] [--rounds N]

Each run appends one line to ``evals/results/routing.jsonl``: the label, the
commit and whether the tree had uncommitted changes, the router's own
report (connections, routed, unreachable, dropped, rounds, seconds), the
exact audit (shorts, clearance, edge, keepout) and the copper's measures
(length, vias, bends by angle). The ledger is the record of what made the
router better or worse; ``docs/router-log.md`` reads it.

Order of merit, as a designer judges a board: no faults first, then the
fewest unrouted connections, then the fewest 90 degree bends and vias,
then time.
"""

from __future__ import annotations

import argparse
import gzip
import json
import subprocess
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kairo.pcb.autoroute import Settings, audit, case_from_json, route_case
from kairo.pcb.evaluate import measure

HERE = Path(__file__).resolve().parent
LEDGER = HERE.parent / "results" / "routing.jsonl"


def commit() -> tuple[str, bool]:
    """The checkout's commit, and whether it has uncommitted changes."""
    head = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain", "src"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    )
    return head, dirty


def main() -> None:
    """Run the benchmark once and append its line to the ledger."""
    parser = argparse.ArgumentParser(description="Benchmark Kairo's router.")
    parser.add_argument("label", help="what changed since the last run")
    parser.add_argument("--case", default="bms_13s11p_signals")
    parser.add_argument("--rounds", type=int, default=15)
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="override a router setting, such as wrong_way=1.5",
    )
    parser.add_argument(
        "--route-all",
        action="store_true",
        help="route the poured nets too, as tracks, before any pour",
    )
    parser.add_argument(
        "--keep", type=Path, help="save the routed tracks and vias to this JSON file"
    )
    args = parser.parse_args()
    overrides = {
        name: float(value) for name, value in (item.split("=", 1) for item in args.set)
    }
    # A case with filled pours runs to a megabyte; it is kept gzipped.
    path = HERE / "cases" / f"{args.case}.json"
    raw = (
        gzip.decompress(path.with_suffix(".json.gz").read_bytes())
        if not path.exists()
        else path.read_bytes()
    )
    case = case_from_json(json.loads(raw.decode("utf-8")))
    if args.route_all:
        case = replace(case, skip=frozenset())
    stats: dict[str, Any] = {}
    start = time.perf_counter()
    tune = replace(Settings(iterations=args.rounds), **overrides)
    tracks, vias = route_case(case, tune, stats)
    seconds = round(time.perf_counter() - start, 1)
    examples: dict[str, list[str]] = {}
    faults = audit(case, tracks, vias, examples=examples)
    copper = measure(
        [(t.net, t.layer, t.start, t.end) for t in tracks], [v.net for v in vias]
    )
    head, dirty = commit()
    row = {
        "when": datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ"),
        "label": args.label,
        "case": args.case,
        "commit": head + ("+" if dirty else ""),
        "rounds_asked": args.rounds,
        "settings": overrides,
        "route_all": args.route_all,
        **stats,
        "unrouted": stats["connections"] - stats["routed"],
        **faults,
        "track_mm": copper.length,
        "vias": copper.vias,
        "bends": copper.bends,
        "bends_per_m": copper.bends_per_m,
        "seconds": seconds,
        "examples": examples,
    }
    if args.keep is not None:
        args.keep.write_text(
            json.dumps(
                {
                    "tracks": [
                        [t.net, t.layer, t.width, *t.start, *t.end] for t in tracks
                    ],
                    "vias": [[v.net, *v.at, v.size] for v in vias],
                }
            ),
            encoding="utf-8",
        )
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as ledger:
        ledger.write(json.dumps(row) + "\n")
    print(json.dumps(row))


if __name__ == "__main__":
    main()
