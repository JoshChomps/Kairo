"""Print the router ledger as a Markdown table, newest last.

    uv run python evals/routing/report.py [--case NAME]

Reads ``evals/results/routing.jsonl`` (written by ``bench.py``). The table
goes into ``docs/router-log.md`` as it is printed, so the log is never
typed by hand.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

LEDGER = Path(__file__).resolve().parent.parent / "results" / "routing.jsonl"
COLUMNS = (
    ("when", "When"),
    ("commit", "Commit"),
    ("label", "Change"),
    ("settings", "Settings"),
    ("unrouted", "Unrouted"),
    ("shorts", "Shorts"),
    ("clearance", "Clearance"),
    ("edge", "Edge"),
    ("keepout", "Keep-out"),
    ("completed", "Completed"),
    ("vias", "Vias"),
    ("bends90", "90°"),
    ("bends_per_m", "Bends/m"),
    ("seconds", "Seconds"),
)


def main() -> None:
    """Print the table."""
    parser = argparse.ArgumentParser(description="Print the router ledger.")
    parser.add_argument("--case", default="bms_13s11p_signals")
    args = parser.parse_args()
    rows: list[dict[str, Any]] = [
        json.loads(line)
        for line in LEDGER.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rows = [r for r in rows if r.get("case") == args.case]
    print("| " + " | ".join(title for _, title in COLUMNS) + " |")
    print("|" + "---|" * len(COLUMNS))
    for row in rows:
        row["bends90"] = row.get("bends", {}).get("90", "")
        settings: dict[str, float] = row.get("settings") or {}
        row["settings"] = ", ".join(f"{k}={v}" for k, v in settings.items())
        cells = [str(row.get(key, "")) for key, _ in COLUMNS]
        print("| " + " | ".join(cells) + " |")


if __name__ == "__main__":
    main()
