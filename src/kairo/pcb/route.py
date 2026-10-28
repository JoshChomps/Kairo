"""Routing a placed board with Freerouting, run as a separate program.

Freerouting (GPL-3.0, github.com/freerouting/freerouting) is never imported:
Kairo writes the board as a Specctra design (`kairo.pcb.specctra`), runs the
router's jar with Java in a scratch folder, and reads its session back as
tracks and vias. It runs headless, with its usage analytics, its update
check and its API server all off, so no part of a design leaves the
machine.

What it is given decides what it can do well: each net's class (track
width and clearance), the pours as planes it may connect to, and the
layers it may route on. A layer that carries a whole-board plane is given
to it as a power layer, which it does not route signals across.
"""

from __future__ import annotations

import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from kairo.library.index import LibraryIndex
from kairo.pcb.board_writer import PlacedPart, Stackup, Track, Via, Zone
from kairo.pcb.layout import Router
from kairo.pcb.specctra import NetClass, read_ses, write_dsn
from kairo.process import run_process

#: Freerouting releases from 2.2 on are built for Java 25 (their manifests'
#: release); 2.1.0 runs on older Java but hung or threw on the drafted BMS
#: board where 2.5.0 finished (measured 2026-10-23). The newest jar found
#: is used, with a Java at least this new.
JAVA_FOR_FREEROUTING = 25


class RoutingError(RuntimeError):
    """The router could not be run, or wrote no session."""


@dataclass(frozen=True, slots=True)
class Freerouting:
    """How to run Freerouting.

    Attributes:
        java: The Java executable.
        jar: Freerouting's jar file.
        passes: The most autorouter passes to run.
        timeout_s: How long it may take, in seconds.
        threads: Optimiser threads; one keeps a run repeatable.
        min_track: The board maker's least track width, in mm; JLCPCB asks
            0.2 mm on 2 oz outer copper (its copper weight guidance).
    """

    java: str
    jar: Path
    passes: int = 30
    timeout_s: float = 1800.0
    threads: int = 1
    min_track: float = 0.2

    def command(self, design: Path, session: Path) -> list[str]:
        """The command line for one run, headless and private."""
        return [
            self.java,
            "-jar",
            str(self.jar),
            "-de",
            str(design),
            "-do",
            str(session),
            "-mp",
            str(self.passes),
            "-mt",
            str(self.threads),
            "--gui.enabled=false",
            "--usage_and_diagnostic_data.disable_analytics=true",
            "--api_server.enabled=false",
        ]


def find_freerouting(tools: Path, **settings: float) -> Freerouting | None:
    """The newest Freerouting jar in a folder, with a Java that runs it.

    Looks for ``freerouting-<version>.jar`` and a JDK folder named
    ``jdk-<major>...`` with ``bin/java`` (or ``java.exe``) in ``tools``.

    Args:
        tools: The folder holding the jar and the JDK.
        **settings: `Freerouting` fields to set, such as ``passes``.

    Returns:
        How to run it, or None if no jar or no new enough Java is there.
    """
    jars = sorted(
        tools.glob("freerouting-*.jar"),
        key=lambda p: tuple(int(v) for v in _numbers(p.stem)),
    )
    javas = [
        exe
        for folder in sorted(tools.glob("jdk-*"), reverse=True)
        if _numbers(folder.name)
        and int(_numbers(folder.name)[0]) >= JAVA_FOR_FREEROUTING
        for exe in (folder / "bin" / "java.exe", folder / "bin" / "java")
        if exe.is_file()
    ]
    if not jars or not javas:
        return None
    # A pass on a 250-part, four-layer board took up to 150 s (the BMS,
    # 2026-10-23); a run is given 240 s a pass and 10 minutes over, so it
    # ends by its passes, not by being stopped with nothing written.
    passes = int(settings.get("passes", Freerouting.passes))
    settings.setdefault("timeout_s", 600.0 + 240.0 * passes)
    return Freerouting(str(javas[0]), jars[-1], **settings)  # type: ignore[arg-type]


def _numbers(text: str) -> list[str]:
    """The dotted version in a name: ``freerouting-2.5.0`` gives 2, 5, 0."""
    tail = text.split("-", 1)[-1]
    out: list[str] = []
    for part in tail.replace("+", ".").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        if not digits:
            break
        out.append(digits)
    return out


def router_for(
    tool: Freerouting,
    index: LibraryIndex,
    name: str,
    *,
    log: Path | None = None,
    areas: bool = False,
) -> Router:
    """A router for `kairo.pcb.layout.lay_out_board` that runs Freerouting.

    Args:
        tool: How to run Freerouting.
        index: The library index, for each footprint's pads.
        name: The board's name.
        log: Where to keep the router's output, for reading afterwards.
        areas: Give the pours on layers they share as copper areas to
            join, not as planes: for a second stage given the islands
            KiCad filled.

    Returns:
        A callable taking the placed parts, outline, pours and stackup and
        giving the tracks and vias Freerouting laid.
    """

    def route(
        parts: Sequence[PlacedPart],
        outline: Sequence[tuple[float, float]],
        zones: Sequence[Zone],
        stackup: Stackup,
        fixed: Sequence[Via],
        fixed_tracks: Sequence[Track],
        classes: Sequence[NetClass],
    ) -> tuple[list[Track], list[Via]]:
        # A layer whose copper is all one net's is a plane, whether given
        # as one zone or as the islands KiCad filled.
        planes = {
            zone.layer
            for zone in zones
            if len({z.net for z in zones if z.layer == zone.layer}) == 1
        }
        text = write_dsn(
            name,
            parts,
            outline,
            index,
            layers=stackup.copper,
            classes=classes,
            zones=[z for z in zones if z.layer in planes] if areas else zones,
            areas=[z for z in zones if z.layer not in planes] if areas else (),
            power_layers=tuple(sorted(planes)),
            fixed_vias=fixed,
            fixed_tracks=fixed_tracks,
        )
        with tempfile.TemporaryDirectory(prefix="kairo-route-") as scratch:
            design = Path(scratch) / f"{name}.dsn"
            session = Path(scratch) / f"{name}.ses"
            design.write_text(text, encoding="utf-8")
            run = run_process(
                tool.command(design, session),
                timeout_s=tool.timeout_s,
                cwd=Path(scratch),
            )
            if log is not None:
                log.write_text(
                    f"$ {' '.join(run.argv)}\nexit {run.returncode}\n"
                    f"--- stdout\n{run.stdout}\n--- stderr\n{run.stderr}\n",
                    encoding="utf-8",
                )
                log.with_suffix(".dsn").write_text(text, encoding="utf-8")
            if not session.is_file():
                raise RoutingError(
                    f"Freerouting wrote no session (exit {run.returncode}): "
                    f"{(run.stderr or run.stdout)[-800:]}"
                )
            tracks, vias = read_ses(
                session.read_text(encoding="utf-8"), min_width=tool.min_track
            )
            # The stitching vias came back as the router kept them; they are
            # already on the board.
            fixed_at = {(v.at, v.net) for v in fixed}
            fixed_runs = {(t.start, t.end, t.net) for t in fixed_tracks}
            return (
                [t for t in tracks if (t.start, t.end, t.net) not in fixed_runs],
                [v for v in vias if (v.at, v.net) not in fixed_at],
            )

    return route
