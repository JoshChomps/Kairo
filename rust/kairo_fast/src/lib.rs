//! The inner loops of Kairo, compiled.
//!
//! `Search::find` is `kairo.pcb.autoroute.search._astar_py` in Rust: the
//! same A* over the same arrays, read in place through the buffer protocol,
//! with the same arithmetic in the same order, so it returns the same path.
//! The moves, step lengths and turn prices come from Python, so the two
//! cannot drift apart.
//!
//! `Arrangement` is the pricing of `kairo.kicad.arrange`'s drawing
//! annealer: whether a part fits, and the cost of its wires, crossings and
//! turns after a move. The annealer's moves and their chances stay in
//! Python; only their pricing is here.
//!
//! `Grazer` measures the diagonal steps that pass fixed copper too
//! near (`grazes.rs`), once per board for every class.
//!
//! The Python versions stay the reference, and tests compare the two.

use std::cmp::Ordering;
use std::collections::BinaryHeap;

use pyo3::buffer::PyBuffer;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

mod grazes;
use rustc_hash::{FxHashMap, FxHashSet};

/// One entry of the open list, ordered as Python orders the tuple
/// `(estimate, spent, node)`, but reversed so the max-heap pops the least.
#[derive(Clone, Copy)]
struct Entry {
    estimate: f64,
    spent: f64,
    node: i64,
}

impl Ord for Entry {
    fn cmp(&self, other: &Self) -> Ordering {
        other
            .estimate
            .partial_cmp(&self.estimate)
            .unwrap_or(Ordering::Equal)
            .then_with(|| {
                other
                    .spent
                    .partial_cmp(&self.spent)
                    .unwrap_or(Ordering::Equal)
            })
            .then_with(|| other.node.cmp(&self.node))
    }
}

impl PartialOrd for Entry {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl PartialEq for Entry {
    fn eq(&self, other: &Self) -> bool {
        self.cmp(other) == Ordering::Equal
    }
}

impl Eq for Entry {}

/// A class's grazing diagonal steps, converted once per board: a step's
/// key (its cell times 8 plus its move) and the nets it passes too near.
#[pyclass(frozen)]
struct Grazes {
    steps: FxHashMap<i64, Vec<i64>>,
}

#[pymethods]
impl Grazes {
    #[new]
    fn new(steps: FxHashMap<i64, Vec<i64>>) -> Self {
        Grazes { steps }
    }
}

/// A search's per-cell arrays, kept between searches. A cell's entries are
/// valid only where its stamp is the current search's, so nothing is
/// cleared between searches.
#[pyclass]
struct Search {
    cost: Vec<f64>,
    parent: Vec<i64>,
    head: Vec<u8>,
    stamp: Vec<u64>,
    run: u64,
}

/// A buffer's items as a plain slice.
///
/// Sound while the caller holds the GIL and does not call into Python
/// before dropping the slice: no Python code can then write to the array,
/// and an `array.array` refuses to resize while its buffer is exported,
/// which `buffer` keeps it for as long as the slice lives.
pub(crate) fn cells<T: pyo3::buffer::Element>(buffer: &PyBuffer<T>) -> PyResult<&[T]> {
    let items = buffer
        .as_slice_ptr()
        .ok_or_else(|| PyValueError::new_err("a router array is not one contiguous buffer"))?;
    // SAFETY: see above; `find` holds the GIL and makes no Python calls.
    Ok(unsafe { items.as_ref() })
}

#[pymethods]
impl Search {
    #[new]
    fn new(size: usize) -> Self {
        Search {
            cost: vec![0.0; size],
            parent: vec![0; size],
            head: vec![8; size],
            stamp: vec![0; size],
            run: 0,
        }
    }

    /// One A* search; the arguments are `_astar_py`'s, unpacked.
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (
        nx, ny, layers, sources, goal, bbox, window, foes, number, use_, history,
        present, weight, via_cost, wrong, moves, steps, turn, own, hard, grazes,
        pad_of, wide, far, ring, drilled, hole_ring
    ))]
    fn find(
        &mut self,
        nx: i64,
        ny: i64,
        layers: i64,
        sources: Vec<i64>,
        goal: FxHashSet<i64>,
        bbox: (i64, i64, i64, i64),
        window: (i64, i64, i64, i64),
        foes: Vec<PyBuffer<i32>>,
        number: i32,
        use_: PyBuffer<i32>,
        history: PyBuffer<f64>,
        present: f64,
        weight: f64,
        via_cost: f64,
        wrong: f64,
        moves: Vec<(i64, i64)>,
        steps: (f64, f64),
        turn: Vec<f64>,
        own: FxHashSet<i64>,
        hard: bool,
        grazes: Option<PyRef<'_, Grazes>>,
        pad_of: FxHashMap<i64, i64>,
        wide: Option<Vec<(i64, i64)>>,
        far: Vec<PyBuffer<i32>>,
        ring: Vec<(i64, i64)>,
        drilled: FxHashSet<i64>,
        hole_ring: Vec<(i64, i64)>,
    ) -> PyResult<Option<Vec<i64>>> {
        let foe_maps = foes
            .iter()
            .map(|b| cells(b))
            .collect::<PyResult<Vec<_>>>()?;
        let far_maps = far.iter().map(|b| cells(b)).collect::<PyResult<Vec<_>>>()?;
        let used = cells(&use_)?;
        let past = cells(&history)?;
        let graze = grazes.as_ref().map(|g| &g.steps);
        self.run += 1;
        let run = self.run;
        let plane = nx * ny;
        let (x0, y0, x1, y1) = bbox;
        let (wl, wt, wr, wb) = window;
        let diag = 2.0_f64.sqrt() - 1.0;
        let guess = |i: i64, j: i64| -> f64 {
            let dx = (x0 - i).max(0).max(i - x1);
            let dy = (y0 - j).max(0).max(j - y1);
            weight * (dx.max(dy) as f64 + diag * dx.min(dy) as f64)
        };
        let mut heap = BinaryHeap::new();
        for &node in &sources {
            let u = node as usize;
            self.stamp[u] = run;
            self.cost[u] = 0.0;
            self.parent[u] = -1;
            self.head[u] = 8;
            let rest = node % plane;
            heap.push(Entry {
                estimate: guess(rest % nx, rest / nx),
                spent: 0.0,
                node,
            });
        }
        while let Some(Entry { spent, node, .. }) = heap.pop() {
            let u = node as usize;
            if spent > self.cost[u] {
                continue;
            }
            if self.parent[u] != -1 && goal.contains(&node) {
                let mut path = vec![node];
                let mut at = self.parent[u];
                while at != -1 {
                    path.push(at);
                    at = self.parent[at as usize];
                }
                path.reverse();
                return Ok(Some(path));
            }
            let layer = node / plane;
            let rest = node % plane;
            let (j, i) = (rest / nx, rest % nx);
            let foe = foe_maps[layer as usize];
            let heading = self.head[u];
            for (d, &(di, dj)) in moves.iter().enumerate() {
                let (ii, jj) = (i + di, j + dj);
                if ii < wl || ii > wr || jj < wt || jj > wb {
                    continue;
                }
                let extra = if heading != 8 {
                    let bend = (d as i64 - heading as i64).rem_euclid(8);
                    let bend = bend.min(8 - bend);
                    if bend > 2 {
                        continue;
                    }
                    turn[bend as usize]
                } else {
                    0.0
                };
                let step_cell = rest + dj * nx + di;
                let nxt = node + dj * nx + di;
                let held = foe[step_cell as usize];
                if held != 0 && held != number {
                    if !own.contains(&nxt) {
                        continue;
                    }
                    // A pad's own cells are open to its net past other
                    // copper, but not a step from one pad into another.
                    if let Some(&into) = pad_of.get(&nxt) {
                        if pad_of.get(&node).copied().unwrap_or(into) != into {
                            continue;
                        }
                    }
                }
                let mut crowd = match &wide {
                    None => used[nxt as usize],
                    Some(offsets) => {
                        let mut most = 0;
                        let base = nxt - step_cell;
                        for &(wi, wj) in offsets {
                            let (si, sj) = (ii + wi, jj + wj);
                            if 0 <= si && si < nx && 0 <= sj && sj < ny {
                                let cell = base + sj * nx + si;
                                let claims = used[cell as usize];
                                if claims > most && !own.contains(&cell) {
                                    most = claims;
                                }
                            }
                        }
                        most
                    }
                };
                for counts in &far_maps {
                    let claims = counts[nxt as usize];
                    if claims > crowd {
                        crowd = claims;
                    }
                }
                if hard && crowd != 0 && !own.contains(&nxt) {
                    continue;
                }
                if di != 0 && dj != 0 {
                    let a = foe[(rest + di) as usize];
                    let b = foe[(rest + dj * nx) as usize];
                    if a != 0 && a != number && b != 0 && b != number {
                        continue;
                    }
                    if let Some(near) = graze.and_then(|g| g.get(&(node * 8 + d as i64))) {
                        if near.iter().any(|&n| n != number as i64) {
                            continue;
                        }
                    }
                }
                let mut step = if d & 1 == 1 { steps.1 } else { steps.0 };
                // Even layers run across (dj == 0), odd layers down (di == 0).
                if wrong != 1.0 && !(di != 0 && dj != 0) && ((layer % 2 == 0) == (di == 0)) {
                    step *= wrong;
                }
                let total = spent
                    + step * (1.0 + past[nxt as usize]) * (1.0 + present * crowd as f64)
                    + extra;
                let v = nxt as usize;
                if self.stamp[v] != run || total < self.cost[v] {
                    self.stamp[v] = run;
                    self.cost[v] = total;
                    self.parent[v] = node;
                    self.head[v] = d as u8;
                    heap.push(Entry {
                        estimate: total + guess(ii, jj),
                        spent: total,
                        node: nxt,
                    });
                }
            }
            if layers > 1 {
                let mut held = foe_maps[layers as usize][rest as usize];
                if !drilled.is_empty()
                    && hole_ring.iter().any(|&(hi, hj)| {
                        let (a, b) = (i + hi, j + hj);
                        0 <= a && a < nx && 0 <= b && b < ny && drilled.contains(&(b * nx + a))
                    })
                {
                    held = -1;
                }
                if held == 0 || held == number {
                    // Other nets' routing anywhere round the via, on any layer.
                    let mut crowd = 0;
                    for &(di, dj) in &ring {
                        let (ii, jj) = (i + di, j + dj);
                        if 0 <= ii && ii < nx && 0 <= jj && jj < ny {
                            let spot = jj * nx + ii;
                            for k in 0..layers {
                                let cell = spot + k * plane;
                                let claims = used[cell as usize];
                                if claims > crowd && !own.contains(&cell) {
                                    crowd = claims;
                                }
                            }
                        }
                    }
                    for counts in &far_maps {
                        for k in 0..layers {
                            let claims = counts[(rest + k * plane) as usize];
                            if claims > crowd {
                                crowd = claims;
                            }
                        }
                    }
                    if hard && crowd != 0 {
                        continue;
                    }
                    for other in 0..layers {
                        if other == layer {
                            continue;
                        }
                        let held = foe_maps[other as usize][rest as usize];
                        if held != 0 && held != number {
                            continue;
                        }
                        let nxt = rest + other * plane;
                        let v = nxt as usize;
                        let total =
                            spent + via_cost * (1.0 + past[v]) * (1.0 + present * crowd as f64);
                        if self.stamp[v] != run || total < self.cost[v] {
                            self.stamp[v] = run;
                            self.cost[v] = total;
                            self.parent[v] = node;
                            self.head[v] = 8;
                            heap.push(Entry {
                                estimate: total + guess(i, j),
                                spent: total,
                                node: nxt,
                            });
                        }
                    }
                }
            }
        }
        Ok(None)
    }
}

type Segment = (i64, i64, i64, i64);
type Cell = (i64, i64);

/// 1 if a horizontal and a vertical segment cross inside both, else 0
/// (`arrange._cross`).
fn cross(a: &Segment, b: &Segment) -> u64 {
    let (h, v) = if a.1 == a.3 && b.0 == b.2 {
        (a, b)
    } else if a.0 == a.2 && b.1 == b.3 {
        (b, a)
    } else {
        return 0;
    };
    u64::from(h.0 < v.0 && v.0 < h.2 && v.1 < h.1 && h.1 < v.3)
}

/// How many bends a wire between two pin ends needs (`arrange.bends`).
fn bends(p: Cell, o: Cell, q: Cell, r: Cell) -> i64 {
    let (vx, vy) = (q.0 - p.0, q.1 - p.1);
    if vx == 0 && vy == 0 {
        return 0;
    }
    let ahead_p = o.0 * vx + o.1 * vy;
    let ahead_q = -(r.0 * vx + r.1 * vy);
    if o.0 == -r.0 && o.1 == -r.1 {
        if ahead_p > 0 && ahead_q > 0 {
            let across = o.1 * vx - o.0 * vy;
            return if across == 0 { 0 } else { 2 };
        }
        return 4;
    }
    if o == r {
        return 2;
    }
    let corner = if o.1 == 0 { (q.0, p.1) } else { (p.0, q.1) };
    let to_corner_p = o.0 * (corner.0 - p.0) + o.1 * (corner.1 - p.1);
    let to_corner_q = r.0 * (corner.0 - q.0) + r.1 * (corner.1 - q.1);
    if to_corner_p > 0 && to_corner_q > 0 {
        1
    } else {
        3
    }
}

/// One pin of a part placed one way (`arrange.PinEnd`), its net numbered
/// as the state numbers wired nets (-1 for any other).
struct Pin {
    net: i64,
    at: Cell,
    out: Cell,
    points: Option<Cell>,
}

/// One way to place a part (`arrange.Shape`): its room and pins.
struct Form {
    room: Segment,
    pins: Vec<Pin>,
}

type PinArg = (i64, Cell, Cell, Option<Cell>);

/// What a block is made of, fixed while it is arranged.
struct Layout {
    forms: Vec<Vec<Form>>,
    members: Vec<Vec<usize>>,
    nets_of: Vec<Vec<usize>>,
    pad: i64,
    bend: f64,
    crossing: f64,
    sideways: f64,
    backwards: f64,
    gravity: f64,
    unwired: f64,
}

/// The state's arrays of spots, as plain slices.
struct Spots<'a> {
    x: &'a [i64],
    y: &'a [i64],
    shape: &'a [i64],
    placed: &'a [i8],
}

/// The state's arrays of spots, shared with Python.
struct Buffers {
    x: PyBuffer<i64>,
    y: PyBuffer<i64>,
    shape: PyBuffer<i64>,
    placed: PyBuffer<i8>,
}

impl Buffers {
    fn spots(&self) -> PyResult<Spots<'_>> {
        Ok(Spots {
            x: cells(&self.x)?,
            y: cells(&self.y)?,
            shape: cells(&self.shape)?,
            placed: cells(&self.placed)?,
        })
    }
}

/// What changes as parts move: their rooms, the costs kept up to date and
/// each net's estimated wire.
struct Live {
    rects: Vec<Option<Segment>>,
    net_cost: Vec<f64>,
    part_cost: Vec<f64>,
    segments: Vec<Vec<Segment>>,
    boxes: Vec<Option<Segment>>,
    touched: Vec<bool>,
}

impl Layout {
    fn form(&self, at: &Spots<'_>, i: usize) -> &Form {
        &self.forms[i][at.shape[i] as usize]
    }

    fn rect(&self, i: usize, x: i64, y: i64, s: usize) -> Segment {
        let (left, top, right, bottom) = self.forms[i][s].room;
        let pad = self.pad;
        (
            x + left - pad,
            y + top - pad,
            x + right + pad,
            y + bottom + pad,
        )
    }

    /// Net k's wired pin ends on placed parts, in the members' order.
    fn ends(&self, at: &Spots<'_>, k: usize) -> Vec<(Cell, Cell)> {
        let mut ends = Vec::new();
        for &i in &self.members[k] {
            if at.placed[i] == 0 {
                continue;
            }
            let (x, y) = (at.x[i], at.y[i]);
            for p in &self.form(at, i).pins {
                if p.net == k as i64 && p.points.is_none() {
                    ends.push(((x + p.at.0, y + p.at.1), p.out));
                }
            }
        }
        ends
    }

    /// `_State._net`.
    fn net(&self, at: &Spots<'_>, k: usize) -> f64 {
        let ends = self.ends(at, k);
        if ends.len() < 2 {
            return 0.0;
        }
        if ends.len() == 2 {
            let ((p, o), (q, r)) = (ends[0], ends[1]);
            return ((p.0 - q.0).abs() + (p.1 - q.1).abs()) as f64
                + self.bend * bends(p, o, q, r) as f64;
        }
        let xs = ends.iter().map(|e| e.0 .0);
        let ys = ends.iter().map(|e| e.0 .1);
        let (x0, x1) = (xs.clone().min().unwrap_or(0), xs.clone().max().unwrap_or(0));
        let (y0, y1) = (ys.clone().min().unwrap_or(0), ys.clone().max().unwrap_or(0));
        let mut cost = (x1 - x0 + y1 - y0) as f64;
        let n = ends.len() as f64;
        let mx = xs.sum::<i64>() as f64 / n;
        let my = ys.sum::<i64>() as f64 / n;
        for &((px, py), (ox, oy)) in &ends {
            if ox as f64 * (mx - px as f64) + oy as f64 * (my - py as f64) < 0.0 {
                cost += self.bend;
            }
        }
        cost
    }

    /// `_State._part`.
    fn part(&self, at: &Spots<'_>, i: usize) -> f64 {
        let form = self.form(at, i);
        let mut cost = 0.0;
        for pin in &form.pins {
            let Some(points) = pin.points else { continue };
            if pin.out == points {
                continue;
            }
            if pin.out == (-points.0, -points.1) {
                cost += self.backwards;
            } else {
                cost += self.sideways;
            }
        }
        let (left, top, right, bottom) = form.room;
        let cx = at.x[i] as f64 + (left + right) as f64 / 2.0;
        let cy = at.y[i] as f64 + (top + bottom) as f64 / 2.0;
        let pull = if self.nets_of[i].is_empty() {
            self.unwired
        } else {
            self.gravity
        };
        cost + pull * (cx.abs() + cy.abs())
    }
}

impl Live {
    /// `_State._wire`: a net's estimated wire, as segments, and their box.
    fn wire(&mut self, layout: &Layout, at: &Spots<'_>, k: usize) {
        let ends = layout.ends(at, k);
        let mut segments: Vec<Segment> = Vec::new();
        if ends.len() >= 2 {
            let hub = if ends.len() == 2 {
                ends[1].0
            } else {
                let n = ends.len() as f64;
                let sx = ends.iter().map(|e| e.0 .0).sum::<i64>() as f64;
                let sy = ends.iter().map(|e| e.0 .1).sum::<i64>() as f64;
                // Python's round: halves to even.
                (
                    (sx / n).round_ties_even() as i64,
                    (sy / n).round_ties_even() as i64,
                )
            };
            let leads = if ends.len() == 2 { 1 } else { ends.len() };
            for &((px, py), (ox, _)) in &ends[..leads] {
                let corner = if ox != 0 { (hub.0, py) } else { (px, hub.1) };
                for (a, b) in [((px, py), corner), (corner, hub)] {
                    if a != b {
                        segments.push((a.0.min(b.0), a.1.min(b.1), a.0.max(b.0), a.1.max(b.1)));
                    }
                }
            }
        }
        self.boxes[k] = segments
            .iter()
            .copied()
            .reduce(|a, b| (a.0.min(b.0), a.1.min(b.1), a.2.max(b.2), a.3.max(b.3)));
        self.segments[k] = segments;
    }

    /// How often net k's wire crosses net j's (`_State._crossings`).
    fn pair(&self, k: usize, j: usize) -> u64 {
        let (Some(a), Some(b)) = (self.boxes[k], self.boxes[j]) else {
            return 0;
        };
        if b.0 > a.2 || a.0 > b.2 || b.1 > a.3 || a.1 > b.3 {
            return 0;
        }
        let mut count = 0;
        for s in &self.segments[k] {
            for t in &self.segments[j] {
                count += cross(s, t);
            }
        }
        count
    }

    /// The crossings of some nets' wires with every other net's and with
    /// each other's, each pair counted once.
    fn crossings(&mut self, nets: &[usize]) -> u64 {
        for &k in nets {
            self.touched[k] = true;
        }
        let mut total = 0;
        for (n, &k) in nets.iter().enumerate() {
            for j in 0..self.segments.len() {
                if !self.touched[j] {
                    total += self.pair(k, j);
                }
            }
            for &j in &nets[n + 1..] {
                total += self.pair(k, j);
            }
        }
        for &k in nets {
            self.touched[k] = false;
        }
        total
    }
}

/// The drawing annealer's pricing (`arrange._State`): its fit test, its
/// costs and their recount after a move, over the state's own arrays of
/// spots, read in place.
#[pyclass]
struct Arrangement {
    layout: Layout,
    buffers: Buffers,
    live: Live,
}

#[pymethods]
impl Arrangement {
    #[new]
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (
        forms, members, nets_of, x, y, shape, placed, pad, bend, crossing,
        sideways, backwards, gravity, unwired
    ))]
    fn new(
        forms: Vec<Vec<(Segment, Vec<PinArg>)>>,
        members: Vec<Vec<usize>>,
        nets_of: Vec<Vec<usize>>,
        x: PyBuffer<i64>,
        y: PyBuffer<i64>,
        shape: PyBuffer<i64>,
        placed: PyBuffer<i8>,
        pad: i64,
        bend: f64,
        crossing: f64,
        sideways: f64,
        backwards: f64,
        gravity: f64,
        unwired: f64,
    ) -> PyResult<Self> {
        let parts = forms.len();
        let nets = members.len();
        let buffers = Buffers {
            x,
            y,
            shape,
            placed,
        };
        {
            let at = buffers.spots()?;
            if [at.x.len(), at.y.len(), at.shape.len(), at.placed.len()] != [parts; 4] {
                return Err(PyValueError::new_err(
                    "one spot array entry per part is needed",
                ));
            }
        }
        let forms = forms
            .into_iter()
            .map(|shapes| {
                shapes
                    .into_iter()
                    .map(|(room, pins)| Form {
                        room,
                        pins: pins
                            .into_iter()
                            .map(|(net, at, out, points)| Pin {
                                net,
                                at,
                                out,
                                points,
                            })
                            .collect(),
                    })
                    .collect()
            })
            .collect();
        Ok(Arrangement {
            layout: Layout {
                forms,
                members,
                nets_of,
                pad,
                bend,
                crossing,
                sideways,
                backwards,
                gravity,
                unwired,
            },
            buffers,
            live: Live {
                rects: vec![None; parts],
                net_cost: vec![0.0; nets],
                part_cost: vec![0.0; parts],
                segments: vec![Vec::new(); nets],
                boxes: vec![None; nets],
                touched: vec![false; nets],
            },
        })
    }

    /// Whether part i fits at a spot, clear of every placed part's room but
    /// those ignored (`_State._fits`).
    #[pyo3(signature = (i, x, y, s, ignore = Vec::new()))]
    fn fits(&self, i: usize, x: i64, y: i64, s: usize, ignore: Vec<usize>) -> bool {
        let (left, top, right, bottom) = self.layout.rect(i, x, y, s);
        for (j, rect) in self.live.rects.iter().enumerate() {
            let Some((a, b, c, d)) = *rect else { continue };
            if j == i || ignore.contains(&j) {
                continue;
            }
            if left <= c && a <= right && top <= d && b <= bottom {
                return false;
            }
        }
        true
    }

    /// Hold part i's room where it now stands.
    fn take(&mut self, i: usize) -> PyResult<()> {
        let at = self.buffers.spots()?;
        self.live.rects[i] = Some(self.layout.rect(i, at.x[i], at.y[i], at.shape[i] as usize));
        Ok(())
    }

    /// Let go of part i's room.
    fn free(&mut self, i: usize) {
        self.live.rects[i] = None;
    }

    /// Part i's own cost and its nets' (`_State._local`).
    fn local(&self, i: usize) -> PyResult<f64> {
        let at = self.buffers.spots()?;
        let mut total = 0.0;
        for &k in &self.layout.nets_of[i] {
            total += self.layout.net(&at, k);
        }
        Ok(total + self.layout.part(&at, i))
    }

    /// Recompute the costs touching some parts; return the change
    /// (`_State.recount`).
    fn recount(&mut self, parts: Vec<usize>) -> PyResult<f64> {
        let at = self.buffers.spots()?;
        let layout = &self.layout;
        let live = &mut self.live;
        let mut nets: Vec<usize> = parts
            .iter()
            .flat_map(|&i| layout.nets_of[i].iter().copied())
            .collect();
        nets.sort_unstable();
        nets.dedup();
        let mut before = 0.0;
        let mut after = 0.0;
        before += layout.crossing * live.crossings(&nets) as f64;
        for &k in &nets {
            before += live.net_cost[k];
            live.net_cost[k] = layout.net(&at, k);
            after += live.net_cost[k];
            live.wire(layout, &at, k);
        }
        after += layout.crossing * live.crossings(&nets) as f64;
        for &i in &parts {
            before += live.part_cost[i];
            live.part_cost[i] = layout.part(&at, i);
            after += live.part_cost[i];
        }
        Ok(after - before)
    }
}

#[pymodule]
#[pyo3(name = "_fast")]
fn kairo_fast(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Search>()?;
    m.add_class::<Grazes>()?;
    m.add_class::<Arrangement>()?;
    m.add_class::<grazes::Grazer>()?;
    Ok(())
}
