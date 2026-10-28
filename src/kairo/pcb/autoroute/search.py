"""A* over the grid, and the trees a net grows from its copper.

Part of Kairo's router; the method is in the package's docstring.
"""

# The package is one module in layers: its private names are shared.
# pyright: reportPrivateUsage=false, reportUnusedFunction=false

from __future__ import annotations

import heapq
import math
from array import array
from collections.abc import Mapping
from itertools import pairwise

from kairo.pcb.autoroute import grid as grid_module
from kairo.pcb.autoroute.grid import (
    _MOVES,
    _STEP,
    _TURN,
    Grid,
    Settings,
    _centres,
    _merged,
    _Net,
)


def _rejoin(
    grid: Grid,
    net: _Net,
    kept: list[list[int]],
    foes: list[list[array[int]]],
    net_id: Mapping[str, int],
    use: array[int],
    history: array[float],
    present: float,
    tune: Settings,
    hard: bool = False,
) -> list[list[int]]:
    """Join what a net still has apart, its kept runs counted as copper."""
    pieces = [
        (set(piece), set(anchor))
        for k, (piece, anchor) in enumerate(zip(net.pieces, net.anchors, strict=True))
        if k not in net.unreachable
    ]
    pieces += [(set(run), set(run)) for run in kept]
    merged = _merged(pieces)
    if len(merged) < 2:
        return []
    part = _Net(
        net.name,
        net.klass,
        [m[0] for m in merged],
        [m[1] for m in merged],
        escape=net.escape,
        exact=net.exact,
    )
    plane = grid.nx * grid.ny
    holes = {cell % plane for cell in _centres(grid, kept)[1]}
    return _tree(
        grid,
        part,
        foes[net.klass],
        net_id[net.name],
        use,
        history,
        present,
        tune,
        hard=hard,
        drilled=holes,
    )


def _tree(
    grid: Grid,
    net: _Net,
    foes: list[array[int]],
    number: int,
    use: array[int],
    history: array[float],
    present: float,
    tune: Settings,
    hard: bool = False,
    drilled: set[int] | None = None,
) -> list[list[int]]:
    """Join a net's pieces one at a time, nearest first, from all it has joined.

    With ``hard``, other nets' routed copper is a wall rather than a price.
    ``drilled`` holds the cells of the vias the net already has, which a
    new via keeps its spacing from.
    """
    # The search reads the net's class's ring, and the holes this tree
    # has drilled, from the scratch space.
    assert grid.scratch is not None
    grid.scratch.ring = grid.rings[net.klass] if grid.rings else ((0, 0),)
    grid.scratch.far = (
        [grid.far_use[j] for j in grid.far_of[net.klass]] if grid.far_of else []
    )
    plane = grid.nx * grid.ny
    grid.scratch.drilled = set(drilled or ())
    own = {cell for piece in net.pieces for cell in piece} | net.escape
    grid.scratch.pad_of = {
        cell: k for k, piece in enumerate(net.pieces) for cell in piece
    }
    tree = set(net.anchors[0])
    left = [
        set(a) for k, a in enumerate(net.anchors[1:], 1) if k not in net.unreachable
    ]
    runs: list[list[int]] = []
    while left:
        path = _search(
            grid, tree, left, foes, number, use, history, present, tune, own, hard
        )
        if path is None:
            if hard:
                break
            # Nothing reached even across the whole board: other nets only
            # raise prices, so what blocks is fixed copper, and no later
            # round will do better.
            for a in left:
                net.unreachable.add(
                    next(k for k, b in enumerate(net.anchors) if b == a)
                )
            break
        runs.append(path)
        tree.update(path)
        own.update(path)
        grid.scratch.drilled.update(
            b % plane for a, b in pairwise(path) if a // plane != b // plane
        )
        end = path[-1]
        reached = next(a for a in left if end in a)
        left.remove(reached)
        tree |= reached
    return runs


def _search(
    grid: Grid,
    sources: set[int],
    targets: list[set[int]],
    foes: list[array[int]],
    number: int,
    use: array[int],
    history: array[float],
    present: float,
    tune: Settings,
    own: set[int],
    hard: bool = False,
) -> list[int] | None:
    """A* from the tree to the nearest piece, first near the pins, then anywhere."""
    goal = {cell for piece in targets for cell in piece}
    spots = [grid.split(c) for c in goal]
    src = [grid.split(c) for c in sources]
    box = (
        min(s[0] for s in spots),
        min(s[1] for s in spots),
        max(s[0] for s in spots),
        max(s[1] for s in spots),
    )
    m = int(tune.margin / tune.pitch)
    window = (
        max(min(box[0], *(s[0] for s in src)) - m, 0),
        max(min(box[1], *(s[1] for s in src)) - m, 0),
        min(max(box[2], *(s[0] for s in src)) + m, grid.nx - 1),
        min(max(box[3], *(s[1] for s in src)) + m, grid.ny - 1),
    )
    whole = (0, 0, grid.nx - 1, grid.ny - 1)
    for area in (window, whole) if window != whole else (whole,):
        found = _astar(
            grid,
            sources,
            goal,
            box,
            area,
            foes,
            number,
            use,
            history,
            present,
            tune,
            own,
            hard,
        )
        if found is not None:
            return found
    return None


def _astar(
    grid: Grid,
    sources: set[int],
    goal: set[int],
    box: tuple[int, int, int, int],
    window: tuple[int, int, int, int],
    foes: list[array[int]],
    number: int,
    use: array[int],
    history: array[float],
    present: float,
    tune: Settings,
    own: set[int],
    hard: bool = False,
) -> list[int] | None:
    """One A* search, compiled where it is built, else `_astar_py`.

    The compiled search (``rust/kairo_fast``) is `_astar_py` line for line,
    over the same arrays read in place, and returns the same path; it is
    about thirty times faster.
    """
    fast = grid_module._FAST
    if fast is None:
        return _astar_py(
            grid, sources, goal, box, window, foes, number, use, history,
            present, tune, own, hard,
        )  # fmt: skip
    scratch = grid.scratch
    assert scratch is not None
    search = scratch.fast
    if search is None:
        search = scratch.fast = fast.Search(len(scratch.cost))
    graze = grid.grazes.get(number)
    compiled = None
    if graze:
        # Converted once per board: a class's table is shared by its nets.
        held = scratch.grazes.get(id(graze))
        if held is None or held[0] is not graze:
            held = scratch.grazes[id(graze)] = (graze, fast.Grazes(graze))
        compiled = held[1]
    return search.find(
        grid.nx, grid.ny, len(grid.layers), list(sources), goal, box, window,
        foes, number, use, history, present, tune.greed, tune.via_cost,
        tune.wrong_way, _MOVES, _STEP, _TURN, own, hard, compiled,
        scratch.pad_of, scratch.ring if len(scratch.ring) > 1 else None,
        scratch.far, grid.via_ring, scratch.drilled, scratch.hole_ring,
    )  # fmt: skip


def _astar_py(
    grid: Grid,
    sources: set[int],
    goal: set[int],
    box: tuple[int, int, int, int],
    window: tuple[int, int, int, int],
    foes: list[array[int]],
    number: int,
    use: array[int],
    history: array[float],
    present: float,
    tune: Settings,
    own: set[int],
    hard: bool = False,
) -> list[int] | None:
    """One A* search: a cell's state is the cell; its arrival heading prices turns.

    Keeping the heading out of the state (eight times fewer states) makes
    the turn cost approximate: a cell keeps the heading of its cheapest
    arrival only.
    """
    scratch = grid.scratch
    assert scratch is not None
    scratch.run += 1
    run = scratch.run
    cost, parent, head, stamp = (
        scratch.cost,
        scratch.parent,
        scratch.head,
        scratch.stamp,
    )
    nx, ny, layers = grid.nx, grid.ny, len(grid.layers)
    plane = nx * ny
    ring = grid.via_ring
    wide = scratch.ring if len(scratch.ring) > 1 else None
    far = scratch.far
    vias = foes[layers]
    x0, y0, x1, y1 = box
    wl, wt, wr, wb = window
    weight = tune.greed
    diag = math.sqrt(2.0) - 1.0
    via_cost = tune.via_cost
    turn_cost = _TURN
    wrong = tune.wrong_way
    graze = grid.grazes.get(number, {})
    pad_of = scratch.pad_of
    heap: list[tuple[float, float, int]] = []
    push, pop = heapq.heappush, heapq.heappop
    for node in sources:
        stamp[node] = run
        cost[node] = 0.0
        parent[node] = -1
        head[node] = 8
        rest = node % plane
        j, i = divmod(rest, nx)
        dx = max(x0 - i, 0, i - x1)
        dy = max(y0 - j, 0, j - y1)
        push(heap, (weight * (max(dx, dy) + diag * min(dx, dy)), 0.0, node))
    while heap:
        _, spent, node = pop(heap)
        if spent > cost[node]:
            continue
        if node in goal and parent[node] != -1:
            path = [node]
            while parent[path[-1]] != -1:
                path.append(parent[path[-1]])
            path.reverse()
            return path
        layer, rest = divmod(node, plane)
        j, i = divmod(rest, nx)
        foe = foes[layer]
        heading = head[node]
        for d, (di, dj) in enumerate(_MOVES):
            ii, jj = i + di, j + dj
            if ii < wl or ii > wr or jj < wt or jj > wb:
                continue
            if heading != 8:
                bend = (d - heading) % 8
                bend = min(bend, 8 - bend)
                if bend > 2:
                    continue
                extra = turn_cost[bend]
            else:
                extra = 0.0
            step_cell = rest + dj * nx + di
            nxt = node + dj * nx + di
            held = foe[step_cell]
            if held != 0 and held != number:
                if nxt not in own:
                    continue
                # A pad's own cells are open to its net past other copper,
                # but a step from one pad straight into another crosses the
                # gap between them, beside that copper: not allowed.
                into = pad_of.get(nxt)
                if into is not None and pad_of.get(node, into) != into:
                    continue
            if wide is None:
                crowd = use[nxt]
            else:
                crowd = 0
                base = nxt - step_cell
                for wi, wj in wide:
                    si, sj = ii + wi, jj + wj
                    if 0 <= si < nx and 0 <= sj < ny:
                        cell = base + sj * nx + si
                        if use[cell] > crowd and cell not in own:
                            crowd = use[cell]
            # Classes it must keep far from (the voltage between them).
            for counts in far:
                if counts[nxt] > crowd:
                    crowd = counts[nxt]
            if hard and crowd and nxt not in own:
                continue
            if di and dj:
                # A diagonal may not cut between two blocked cells, nor
                # graze copper beside it closer than any track could pass.
                a = foe[rest + di]
                b = foe[rest + dj * nx]
                if a != 0 and a != number and b != 0 and b != number:
                    continue
                near = graze.get(node * 8 + d)
                if near is not None and any(n != number for n in near):
                    continue
            step = _STEP[d & 1]
            # Even layers run across (dj == 0), odd layers down (di == 0).
            if wrong != 1.0 and not (di and dj) and (layer % 2 == 0) == (di == 0):
                step *= wrong
            total = (
                spent + step * (1.0 + history[nxt]) * (1.0 + present * crowd) + extra
            )
            if stamp[nxt] != run or total < cost[nxt]:
                stamp[nxt] = run
                cost[nxt] = total
                parent[nxt] = node
                head[nxt] = d
                dx = max(x0 - ii, 0, ii - x1)
                dy = max(y0 - jj, 0, jj - y1)
                push(
                    heap,
                    (total + weight * (max(dx, dy) + diag * min(dx, dy)), total, nxt),
                )
        if layers > 1:
            held = vias[rest]
            drilled = scratch.drilled
            if drilled and any(
                0 <= i + hi < nx
                and 0 <= j + hj < ny
                and (j + hj) * nx + i + hi in drilled
                for hi, hj in scratch.hole_ring
            ):
                held = -1
            if held == 0 or held == number:
                # Other nets' routing anywhere round the via, on any layer.
                crowd = 0
                for di, dj in ring:
                    ii, jj = i + di, j + dj
                    if 0 <= ii < nx and 0 <= jj < ny:
                        spot = jj * nx + ii
                        for k in range(layers):
                            cell = spot + k * plane
                            if use[cell] > crowd and cell not in own:
                                crowd = use[cell]
                for counts in far:
                    for k in range(layers):
                        if counts[rest + k * plane] > crowd:
                            crowd = counts[rest + k * plane]
                if hard and crowd:
                    continue
                for other in range(layers):
                    if other == layer:
                        continue
                    held = foes[other][rest]
                    if held != 0 and held != number:
                        continue
                    nxt = rest + other * plane
                    total = spent + via_cost * (1.0 + history[nxt]) * (
                        1.0 + present * crowd
                    )
                    if stamp[nxt] != run or total < cost[nxt]:
                        stamp[nxt] = run
                        cost[nxt] = total
                        parent[nxt] = node
                        head[nxt] = 8
                        dx = max(x0 - i, 0, i - x1)
                        dy = max(y0 - j, 0, j - y1)
                        push(
                            heap,
                            (
                                total + weight * (max(dx, dy) + diag * min(dx, dy)),
                                total,
                                nxt,
                            ),
                        )
    return None
