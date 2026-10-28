"""A board's net classes and its maker's rules, for the router and for KiCad.

The design states its classes by its own net names (``FET_MID``); KiCad
names a net inside a sheet by its path (``/power_switch/FET_MID``). This
module maps one to the other once, and hands the same figures to every
step that needs them:

- the router's classes (`kairo.pcb.specctra.NetClass`), so it routes each
  net at its width and keeps its clearance;
- each net's clearance, so the vias Kairo places itself (a plane's fanout)
  keep the same distances the router does;
- the project file's ``net_settings`` and design rules, so KiCad's DRC
  holds the board to the classes and the maker's limits. The keys are as
  KiCad 10 saves them (the cm5_minima demo project).

Where the board names an IPC-2221 column (``spacing``), the classes are
split by voltage: each net's stated range puts it in a band
`VOLTAGE_BAND` volts wide at each end, and a pair of bands keeps the
column's spacing for the largest voltage between any two of their nets
(`kairo.calc.spacing.larger_spacing`), written for KiCad as pair rules
(`pair_rules`). Two nets are compared at the same operating point, both
at their highest and both at their lowest: the taps of a battery stack
move together. A net whose voltage the design does not state gets no
pair rule, and is listed as such (`unstated`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from kairo.calc.spacing import larger_spacing
from kairo.ir import Board, Design, NetClass
from kairo.pcb.specctra import NetClass as RoutingClass

#: The width of a voltage band, in volts: two nets in neighbouring bands
#: are at most 30 V apart, where IPC-2221's spacing steps up.
VOLTAGE_BAND = 15.0


def plain(name: str) -> str:
    """A KiCad net name without its sheet path: ``/power/FET_MID`` is ``FET_MID``."""
    return name.rsplit("/", 1)[-1]


def net_volts(design: Design) -> dict[str, tuple[float, float]]:
    """Each net's lowest and highest voltage the design states, by its own name.

    A ground net is 0 V; a net with neither a voltage nor a min and max is
    left out.
    """
    out: dict[str, tuple[float, float]] = {}
    for name, net in design.nets.items():
        if net.min is not None and net.max is not None:
            out[name] = (net.min.value, net.max.value)
        elif net.voltage is not None:
            out[name] = (net.voltage.value, net.voltage.value)
        elif net.role == "ground":
            out[name] = (0.0, 0.0)
    return out


def unstated(
    board: Board, nets: Sequence[str], volts: Mapping[str, tuple[float, float]]
) -> list[str]:
    """The nets with no stated voltage, where the board asks for spacing by voltage."""
    if board.spacing is None:
        return []
    return [
        n for n in nets if not n.startswith("unconnected-") and plain(n) not in volts
    ]


def routing_classes(
    board: Board,
    nets: Sequence[str],
    volts: Mapping[str, tuple[float, float]] | None = None,
) -> list[RoutingClass]:
    """The router's classes, by KiCad's net names.

    Args:
        board: The board, with its net classes.
        nets: Every net KiCad's netlist names.
        volts: Each net's lowest and highest voltage, by the design's
            names (`net_volts`), for spacing by voltage.

    Returns:
        One class for each of the board's classes, Default last holding
        every net no other class names; empty if the board states none.
        With the board's ``spacing`` and voltages given, each class is
        split by voltage band (``Default@15-30V``), with each pair's
        spacing in ``apart``.
    """
    if not board.net_classes:
        return []
    plain_classes = _plain_classes(board, nets)
    if board.spacing is None or not volts:
        return plain_classes
    return _by_voltage(plain_classes, volts, board.spacing)


def _plain_classes(board: Board, nets: Sequence[str]) -> list[RoutingClass]:
    """The board's classes as the router takes them, before any voltage split."""
    named = [n for n in nets if not n.startswith("unconnected-")]
    out: list[RoutingClass] = []
    taken: set[str] = set()
    for c in board.net_classes:
        if c.name == "Default":
            continue
        members = tuple(n for n in named if plain(n) in c.nets)
        taken.update(members)
        out.append(RoutingClass(c.name, c.track.value, c.clearance.value, members))
    default = _default(board)
    out.append(
        RoutingClass(
            "default",
            default.track.value,
            default.clearance.value,
            tuple(n for n in named if n not in taken),
        )
    )
    return out


def _by_voltage(
    classes: Sequence[RoutingClass],
    volts: Mapping[str, tuple[float, float]],
    column: str,
) -> list[RoutingClass]:
    """Each class split by its nets' voltage bands, with the spacing each pair needs."""
    split: list[tuple[str, RoutingClass, list[str], list[tuple[float, float]]]] = []
    for c in classes:
        base = "Default" if c.name == "default" else c.name
        groups: dict[tuple[int, int] | None, list[str]] = {}
        for net in c.nets:
            v = volts.get(plain(net))
            key = (
                None
                if v is None
                else (int(v[0] // VOLTAGE_BAND), int(v[1] // VOLTAGE_BAND))
            )
            groups.setdefault(key, []).append(net)
        for key, members in groups.items():
            if key is None:
                name = c.name
            else:
                low, high = key[0] * VOLTAGE_BAND, (key[1] + 1) * VOLTAGE_BAND
                name = f"{base}@{low:g}-{high:g}V"
            ranges = [volts[plain(n)] for n in members if plain(n) in volts]
            split.append((name, c, members, ranges))
    apart: dict[str, dict[str, float]] = {name: {} for name, *_ in split}
    for k, (name, c, _, ranges) in enumerate(split):
        for other, d, _, theirs in split[k + 1 :]:
            if not ranges or not theirs:
                continue
            his = [r[1] for r in ranges]
            los = [r[0] for r in ranges]
            their_his = [r[1] for r in theirs]
            their_los = [r[0] for r in theirs]
            volts_apart = max(
                max(his) - min(their_his),
                max(their_his) - min(his),
                max(los) - min(their_los),
                max(their_los) - min(los),
                0.0,
            )
            need = larger_spacing(volts_apart, column) if volts_apart > 0 else 0.0
            if need > max(c.clearance, d.clearance) + 1e-9:
                apart[name][other] = need
    return [
        RoutingClass(
            name, c.width, c.clearance, tuple(members), tuple(apart[name].items())
        )
        for name, c, members, _ in split
    ]


def pair_rules(classes: Sequence[RoutingClass]) -> str:
    """KiCad custom rules (a ``.kicad_dru`` file) for each pair of classes' spacing.

    Written both ways round, so the rule holds whichever item KiCad's DRC
    takes as ``A``.
    """
    lines = ["(version 1)"]
    for c in classes:
        for other, gap in c.apart:
            for a, b in ((c.name, other), (other, c.name)):
                lines.append(
                    f'(rule "spacing {a} to {b}"\n'
                    f"  (constraint clearance (min {gap:g}mm))\n"
                    f"  (condition \"A.NetClass == '{a}' && B.NetClass == '{b}'\"))"
                )
    return "\n".join(lines) + "\n"


def clearances(classes: Sequence[RoutingClass]) -> dict[str, float]:
    """Each net's clearance, by KiCad's name."""
    return {net: c.clearance for c in classes for net in c.nets}


def via_of(board: Board) -> tuple[float, float] | None:
    """The Default class's via, as diameter and drill, if the design states it."""
    if not board.net_classes:
        return None
    default = _default(board)
    if default.via_diameter is None or default.via_drill is None:
        return None
    return (default.via_diameter.value, default.via_drill.value)


#: The least ``net_settings`` version whose class patterns KiCad 10 applies.
NET_SETTINGS_VERSION = 4


def apply_rules(
    project: dict[str, Any],
    board: Board,
    nets: Sequence[str],
    classes: Sequence[RoutingClass] | None = None,
) -> None:
    """Write a board's classes and its maker's rules into a project file's data.

    Each class's nets are matched by their whole KiCad name, so a pattern
    never catches a net meant for another class.

    Args:
        project: The parsed ``.kicad_pro``, changed in place.
        board: The board.
        nets: Every net KiCad's netlist names.
        classes: The router's classes (`routing_classes`), split by
            voltage, to write instead of the board's own.
    """
    if board.net_classes:
        settings = project.setdefault("net_settings", {})
        patterns: list[dict[str, str]] = []
        if classes is None:
            settings["classes"] = [_kicad_class(c, board) for c in board.net_classes]
            for c in board.net_classes:
                for net in nets:
                    if plain(net) in c.nets:
                        patterns.append({"netclass": c.name, "pattern": net})
        else:
            by_name = {c.name: c for c in board.net_classes}
            written = [_kicad_class(_default(board), board)]
            for c in classes:
                if c.name == "default":
                    continue
                base = by_name.get(c.name.split("@", 1)[0], _default(board))
                entry = _kicad_class(base, board)
                entry.update(
                    {"name": c.name, "clearance": c.clearance, "track_width": c.width}
                )
                if entry["name"] != "Default":
                    entry["priority"] = 0
                written.append(entry)
                patterns += [{"netclass": c.name, "pattern": net} for net in c.nets]
            settings["classes"] = written
        settings["netclass_patterns"] = patterns
        # KiCad 10 ignores the patterns in a version 0 block (as a new
        # project's is) and applies them from version 3 on: measured with
        # its DRC on the BMS, 0 clearance findings against 499. Version 4
        # is what KiCad 9 and most real projects save.
        meta = settings.setdefault("meta", {})
        meta["version"] = max(int(meta.get("version", 0)), NET_SETTINGS_VERSION)
    if board.rules is not None:
        r = board.rules
        rules = (
            project.setdefault("board", {})
            .setdefault("design_settings", {})
            .setdefault("rules", {})
        )
        rules.update(
            {
                "min_track_width": r.min_track.value,
                "min_clearance": r.min_clearance.value,
                "min_through_hole_diameter": r.min_drill.value,
                "min_via_diameter": r.min_via_diameter.value,
                "min_via_annular_width": r.min_annular.value,
                "min_hole_clearance": r.min_hole_clearance.value,
                "min_hole_to_hole": r.min_hole_to_hole.value,
                "min_copper_edge_clearance": r.copper_edge_clearance.value,
            }
        )


def _default(board: Board) -> NetClass:
    return next(c for c in board.net_classes if c.name == "Default")


def _kicad_class(c: NetClass, board: Board) -> dict[str, Any]:
    """A class as KiCad 10 saves it in ``net_settings.classes``."""
    default = _default(board)
    diameter = c.via_diameter or default.via_diameter
    drill = c.via_drill or default.via_drill
    entry: dict[str, Any] = {
        "bus_width": 12,
        "clearance": c.clearance.value,
        "diff_pair_gap": 0.25,
        "diff_pair_via_gap": 0.25,
        "diff_pair_width": 0.2,
        "line_style": 0,
        "microvia_diameter": 0.3,
        "microvia_drill": 0.1,
        "name": c.name,
        "pcb_color": "rgba(0, 0, 0, 0.000)",
        "priority": 2147483647 if c.name == "Default" else 0,
        "schematic_color": "rgba(0, 0, 0, 0.000)",
        "track_width": c.track.value,
        "tuning_profile": "",
        "wire_width": 6,
    }
    if diameter is not None and drill is not None:
        entry["via_diameter"] = diameter.value
        entry["via_drill"] = drill.value
    return entry
