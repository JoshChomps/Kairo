"""Route every demo board in the KiCad install, taken up and routed afresh.

    uv run python evals/routing/corpus.py KICAD_DEMOS_FOLDER [--max-pads N]

KiCad's demos are other people's boards of every kind, so they find what
one board alone cannot: a crash, a fault in the copper, a board the
router cannot finish. Each is read as `import_board` reads one (its pads,
without its tracks or pours), routed with the router's own settings and
audited exactly; the table gives the board's own routing beside Kairo's.
Nothing is written into the repository: the demos are read where KiCad
installed them.
"""

from __future__ import annotations

import argparse
import time
import traceback
from pathlib import Path

from import_board import project_of, read_board  # type: ignore[import-not-found]

from kairo.pcb.autoroute import Settings, audit, route_case


def main() -> None:
    """Route each demo board and print one line for each."""
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else ""
    )
    parser.add_argument("demos", help="the demos folder of a KiCad install")
    parser.add_argument("--max-pads", type=int, default=1500)
    args = parser.parse_args()
    boards = sorted(Path(args.demos).rglob("*.kicad_pcb"))
    print(
        f"{'board':34} {'pads':>5} {'conn':>5} {'unrouted':>8} {'faults':>6} "
        f"{'track mm':>9} {'vias':>5} {'s':>5}   own: track mm, vias, zones"
    )
    for board in boards:
        name = board.stem[:34]
        try:
            case, own = read_board(board.read_text(encoding="utf-8"), project_of(board))
        except (ValueError, KeyError, IndexError) as exc:
            print(f"{name:34} not read: {exc}")
            continue
        pads = len(case.shapes)
        if pads > args.max_pads:
            print(f"{name:34} {pads:5} skipped: more than {args.max_pads} pads")
            continue
        started = time.monotonic()
        try:
            stats: dict[str, object] = {}
            tracks, vias = route_case(case, Settings(), stats=stats)
            found = audit(case, tracks, vias)
        except Exception as exc:  # a crash is the finding
            print(f"{name:34} {pads:5} CRASHED: {type(exc).__name__}: {exc}")
            traceback.print_exc(limit=3)
            continue
        faults = sum(
            int(found.get(k, 0)) for k in ("shorts", "clearance", "edge", "holes")
        )
        length = sum(
            ((t.end[0] - t.start[0]) ** 2 + (t.end[1] - t.start[1]) ** 2) ** 0.5
            for t in tracks
        )
        unrouted = int(str(stats["connections"])) - int(str(stats["routed"]))
        print(
            f"{name:34} {pads:5} {stats.get('connections', '?'):>5} "
            f"{unrouted:>8} {faults:6} {length:9.0f} {len(vias):5} "
            f"{time.monotonic() - started:5.0f}   "
            f"{own['track_mm']}, {own['vias']}, {own['zones']}",
            flush=True,
        )


if __name__ == "__main__":
    main()
