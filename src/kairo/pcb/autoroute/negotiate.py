"""Negotiated congestion, and the passes after it: completion, rescue, tidy.

Part of Kairo's router; the method is in the package's docstring.
"""

# The package is one module in layers: its private names are shared.
# pyright: reportPrivateUsage=false, reportUnusedFunction=false

from __future__ import annotations

import math
from array import array
from collections.abc import Mapping, Sequence
from dataclasses import replace
from itertools import pairwise
from typing import Any

from kairo.pcb.autoroute.grid import (
    Grid,
    Settings,
    _centres,
    _edge_band,
    _far_drop,
    _far_hit,
    _far_take,
    _footprint,
    _missing,
    _Net,
    _run_cells,
    _span,
)
from kairo.pcb.autoroute.search import _rejoin, _tree
from kairo.pcb.specctra import NetClass

# -- negotiation --------------------------------------------------------------


def _negotiate(
    grid: Grid,
    nets: list[_Net],
    foes: list[list[array[int]]],
    net_id: Mapping[str, int],
    classes: Sequence[NetClass],
    tune: Settings,
    via: float,
    report: dict[str, Any],
) -> tuple[dict[str, list[list[int]]], array[int], dict[str, set[int]]]:
    """Route every net, then reroute those that cross others, dearer each round.

    Each routed net claims the cells within a track and a clearance of its
    centreline (`_footprint`); two nets share when one's centreline runs
    through the other's claim. A cell's price grows with the claims on it
    now (present) and with the rounds it was contested (history).

    Returns:
        Each net's runs, the claims on every cell, and each net's own
        claimed cells.
    """
    size = grid.nx * grid.ny * len(grid.layers)
    use = array("i", bytes(4 * size))
    history = _edge_band(grid, tune.edge + tune.edge_band)
    paths: dict[str, list[list[int]]] = {}
    claimed: dict[str, set[int]] = {}
    present = 0.5
    order = sorted(nets, key=lambda n: _span(grid, n))
    todo = list(order)

    klass_of = {net.name: net.klass for net in nets}

    def crossing(name: str) -> set[int]:
        own = claimed.get(name, set())
        track, holes = _centres(grid, paths.get(name, []))
        klass = klass_of[name]
        return {
            c
            for c in track | holes
            if use[c] - (1 if c in own else 0) > 0 or _far_hit(grid, klass, c)
        }

    report["rounds"] = 0
    contested: dict[str, set[int]] = {}
    for _ in range(tune.iterations):
        report["rounds"] += 1
        for net in todo:
            for cell in claimed.pop(net.name, set()):
                use[cell] -= 1
            _far_drop(grid, net.name)
            bad = contested.get(net.name)
            if tune.granular and bad and paths.get(net.name):
                kept = [
                    run for run in paths[net.name] if not bad & _run_cells(grid, run)
                ]
                runs = kept + _rejoin(
                    grid, net, kept, foes, net_id, use, history, present, tune
                )
            else:
                runs = _tree(
                    grid,
                    net,
                    foes[net.klass],
                    net_id[net.name],
                    use,
                    history,
                    present,
                    tune,
                )
            paths[net.name] = runs
            cells = _footprint(grid, runs, classes[net.klass], tune.pitch, via)
            claimed[net.name] = cells
            for cell in cells:
                use[cell] += 1
            _far_take(grid, net.name, net.klass, runs, tune.pitch, via)
        contested = {net.name: crossing(net.name) for net in order}
        todo = [net for net in order if contested[net.name]]
        if not todo:
            break
        for cells in contested.values():
            for cell in cells:
                history[cell] += tune.history_step
        present *= tune.present_growth
    # A connection still crossing another net's is dropped, the nets with
    # the most crossing first: a gap is reported, a short is not made. Only
    # the crossing connections go; the rest of a net stays routed.
    for net in sorted(order, key=lambda n: -len(crossing(n.name))):
        bad = crossing(net.name)
        if not bad:
            continue
        for cell in claimed.pop(net.name, set()):
            use[cell] -= 1
        _far_drop(grid, net.name)
        kept = [
            run
            for run in paths[net.name]
            if not (bad & (_centres(grid, [run])[0] | _centres(grid, [run])[1]))
        ]
        paths[net.name] = kept
        cells = _footprint(grid, kept, classes[net.klass], tune.pitch, via)
        claimed[net.name] = cells
        for cell in cells:
            use[cell] += 1
        _far_take(grid, net.name, net.klass, kept, tune.pitch, via)
    # Completion: each net still in pieces tries once more, other nets'
    # routes now walls, vias at half price, from all the copper it has.
    gentle = replace(tune, via_cost=tune.via_cost / 2)
    report["completed"] = 0
    for net in order if tune.complete else ():
        kept = paths.get(net.name, [])
        if len(net.pieces) - 1 - len(kept) - len(net.unreachable) <= 0:
            continue
        for cell in claimed.pop(net.name, set()):
            use[cell] -= 1
        _far_drop(grid, net.name)
        more = _rejoin(
            grid, net, kept, foes, net_id, use, history, present, gentle, hard=True
        )
        report["completed"] += len(more)
        kept = kept + more
        paths[net.name] = kept
        cells = _footprint(grid, kept, classes[net.klass], tune.pitch, via)
        claimed[net.name] = cells
        for cell in cells:
            use[cell] += 1
        _far_take(grid, net.name, net.klass, kept, tune.pitch, via)
    report["rescued"] = 0
    if tune.rescue:
        report["rescued"] = _rescue(
            grid, order, paths, claimed, use, history, foes, net_id, classes, tune, via
        )
    report["tidied"] = 0
    if tune.tidy:
        report["tidied"] = _tidy(
            grid, order, paths, claimed, use, history, foes, net_id, classes, tune, via
        )
    return paths, use, claimed


#: A run is a detour, worth trying again, when it is this much longer than
#: the octilinear distance between its ends.
DETOUR = 1.25


def _tidy(
    grid: Grid,
    order: list[_Net],
    paths: dict[str, list[list[int]]],
    claimed: dict[str, set[int]],
    use: array[int],
    history: array[float],
    foes: list[list[array[int]]],
    net_id: Mapping[str, int],
    classes: Sequence[NetClass],
    tune: Settings,
    via: float,
) -> int:
    """Pull each detour tight, now that every route is known.

    Negotiation routes a connection round what was crowded when it was
    routed; once the board has settled, a shorter way is often free. Each
    run longer than `DETOUR` times the octilinear distance between its
    ends is lifted and its net joined again with every other route a
    wall; the new way is kept only if it is shorter, makes every join the
    old one made, and crosses nothing. A designer does the same, pulling
    a finished board's tracks tight.

    Returns:
        How many runs were replaced.
    """
    by_name = {net.name: net for net in order}

    def commit(name: str, runs: list[list[int]]) -> None:
        paths[name] = runs
        cells = _footprint(grid, runs, classes[by_name[name].klass], tune.pitch, via)
        claimed[name] = cells
        for cell in cells:
            use[cell] += 1
        _far_take(grid, name, by_name[name].klass, runs, tune.pitch, via)

    def release(name: str) -> None:
        for cell in claimed.pop(name, set()):
            use[cell] -= 1
        _far_drop(grid, name)

    replaced = 0
    for net in order:
        runs = paths.get(net.name, [])
        for run in sorted(runs, key=lambda r: -_detour(grid, r)):
            if _detour(grid, run) < DETOUR or run not in paths[net.name]:
                continue
            before = paths[net.name]
            missing = _missing(grid, net, before)
            others = [r for r in before if r is not run]
            release(net.name)
            more = _rejoin(
                grid, net, others, foes, net_id, use, history, 1.0, tune, hard=True
            )
            after = others + more
            shorter = sum(_length(grid, r, tune) for r in more) < _length(
                grid, run, tune
            )
            joined = _missing(grid, net, after) <= missing
            commit(net.name, after if shorter and joined else before)
            if shorter and joined:
                # Clear both ways, as the rescue checks (`_touching`): its
                # route in no other net's claim, no other route in its own.
                track, holes = _centres(grid, more)
                own = claimed[net.name]
                claim = _footprint(grid, more, classes[net.klass], tune.pitch, via)
                others = [o for o in order if o.name != net.name]
                if any(
                    use[c] - (1 if c in own else 0) > 0 or _far_hit(grid, net.klass, c)
                    for c in track | holes
                ) or _touching(grid, set(), claim, others, paths, claimed):
                    release(net.name)
                    commit(net.name, before)
                    continue
                replaced += 1
    return replaced


def _length(grid: Grid, run: list[int], tune: Settings) -> float:
    """A run's length in cells, a via counted at its price."""
    plane = grid.nx * grid.ny
    total = 0.0
    for a, b in pairwise(run):
        if a // plane != b // plane:
            total += tune.via_cost
            continue
        ia, ja, _ = grid.split(a)
        ib, jb, _ = grid.split(b)
        total += math.hypot(ib - ia, jb - ja)
    return total


def _detour(grid: Grid, run: list[int]) -> float:
    """How much longer a run is than the octilinear way between its ends."""
    if len(run) < 2:
        return 1.0
    ia, ja, _ = grid.split(run[0])
    ib, jb, _ = grid.split(run[-1])
    dx, dy = abs(ib - ia), abs(jb - ja)
    direct = max(dx, dy) + (math.sqrt(2.0) - 1.0) * min(dx, dy)
    plane = grid.nx * grid.ny
    walked = sum(
        math.hypot(
            *(p - q for p, q in zip(grid.split(b)[:2], grid.split(a)[:2], strict=True))
        )
        for a, b in pairwise(run)
        if a // plane == b // plane
    )
    return walked / direct if direct else 1.0


def _rescue(
    grid: Grid,
    order: list[_Net],
    paths: dict[str, list[list[int]]],
    claimed: dict[str, set[int]],
    use: array[int],
    history: array[float],
    foes: list[list[array[int]]],
    net_id: Mapping[str, int],
    classes: Sequence[NetClass],
    tune: Settings,
    via: float,
) -> int:
    """Make what negotiation and completion left, by lifting what is in the way.

    For each net still missing a join: route it with other nets' routes
    dear but passable, find the nets whose routes that path crosses, lift
    just their crossing connections, lay the path, and route the lifted
    nets again with every route a wall. The trade is kept only when no
    net ends up missing more than before; otherwise everything is put
    back as it was. A designer does the same by hand: move what blocks
    the last connection, if it can go elsewhere.

    Returns:
        The joins made.
    """
    by_name = {net.name: net for net in order}

    def release(name: str) -> None:
        for cell in claimed.pop(name, set()):
            use[cell] -= 1
        _far_drop(grid, name)

    def commit(name: str, runs: list[list[int]]) -> None:
        paths[name] = runs
        cells = _footprint(grid, runs, classes[by_name[name].klass], tune.pitch, via)
        claimed[name] = cells
        for cell in cells:
            use[cell] += 1
        _far_take(grid, name, by_name[name].klass, runs, tune.pitch, via)

    def crosses(name: str) -> bool:
        own = claimed.get(name, set())
        track, holes = _centres(grid, paths.get(name, []))
        klass = by_name[name].klass
        return any(
            use[c] - (1 if c in own else 0) > 0 or _far_hit(grid, klass, c)
            for c in track | holes
        )

    made = 0
    for net in order:
        before = _missing(grid, net, paths.get(net.name, []))
        if before <= 0:
            continue
        kept = paths.get(net.name, [])
        release(net.name)
        trial = _rejoin(grid, net, kept, foes, net_id, use, history, 1.0, tune)
        if not trial:
            commit(net.name, kept)
            continue
        cells = {c for run in trial for c in _run_cells(grid, run)}
        claim = _footprint(grid, trial, classes[net.klass], tune.pitch, via)
        blockers = _touching(
            grid, cells, claim, [o for o in order if o.name != net.name], paths, claimed
        )
        saved = {o.name: list(paths.get(o.name, [])) for o in blockers}
        missing = {o.name: _missing(grid, o, saved[o.name]) for o in blockers}
        lifted: dict[str, list[list[int]]] = {}
        klass_of = {o.name: classes[o.klass] for o in blockers}
        for other in blockers:
            release(other.name)
            # Lift every connection whose claim the path enters, or that
            # enters the path's claim: a near miss collides too.
            lifted[other.name] = [
                run
                for run in saved[other.name]
                if not cells
                & _footprint(grid, [run], klass_of[other.name], tune.pitch, via)
                and not claim & _run_cells(grid, run)
            ]
            commit(other.name, lifted[other.name])
        commit(net.name, kept + trial)
        ok = True
        for other in blockers:
            release(other.name)
            again = _rejoin(
                grid,
                other,
                lifted[other.name],
                foes,
                net_id,
                use,
                history,
                1.0,
                tune,
                hard=True,
            )
            commit(other.name, lifted[other.name] + again)
            if _missing(grid, other, paths[other.name]) > missing[other.name]:
                ok = False
                break
        # Kept only if nothing it touched now crosses anything.
        if ok and any(crosses(n) for n in [net.name, *(o.name for o in blockers)]):
            ok = False
        if ok and _missing(grid, net, paths[net.name]) < before:
            made += before - _missing(grid, net, paths[net.name])
            continue
        # Undo: every net as it was.
        release(net.name)
        for other in blockers:
            release(other.name)
        commit(net.name, kept)
        for other in blockers:
            commit(other.name, saved[other.name])
    return made


def _touching(
    grid: Grid,
    cells: set[int],
    claim: set[int],
    others: Sequence[_Net],
    paths: Mapping[str, list[list[int]]],
    claimed: Mapping[str, set[int]],
) -> list[_Net]:
    """The nets a new route comes too near, seen from either side.

    A net is touched when the route's centreline (``cells``) enters its
    claim, or its centreline enters the route's claim (``claim``). Both
    are needed: a via claims a via's width and a clearance round it, more
    than a track does, so a via can sit beside a track whose own claim
    never reaches it.
    """
    found: list[_Net] = []
    for other in others:
        theirs = claimed.get(other.name, set())
        if theirs & cells:
            found.append(other)
        elif theirs & claim:
            # Its claim holds its centreline, so only then can that meet ours.
            track, holes = _centres(grid, paths.get(other.name, []))
            if (track | holes) & claim:
                found.append(other)
    return found
