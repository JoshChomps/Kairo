//! The grazing diagonal steps of `kairo.pcb.autoroute.grid._grazes`.
//!
//! The copper is held once per board, in the same 1 mm buckets as
//! `grid._Near`, and each step's distances are measured once for every
//! class. The distances are `kairo.pcb.geometry`'s, line for line.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};
use rustc_hash::{FxHashMap, FxHashSet};

use pyo3::buffer::PyBuffer;

use crate::cells;

type Point = (f64, f64);

/// `geometry.point_segment`.
fn point_segment(p: Point, a: Point, b: Point) -> f64 {
    let (dx, dy) = (b.0 - a.0, b.1 - a.1);
    let length = dx * dx + dy * dy;
    let t = if length == 0.0 {
        0.0
    } else {
        (((p.0 - a.0) * dx + (p.1 - a.1) * dy) / length).clamp(0.0, 1.0)
    };
    (p.0 - (a.0 + t * dx)).hypot(p.1 - (a.1 + t * dy))
}

/// `geometry.segment_segment`.
fn segment_segment(a: Point, b: Point, c: Point, d: Point) -> f64 {
    let side = |p: Point, q: Point, r: Point| (q.0 - p.0) * (r.1 - p.1) - (q.1 - p.1) * (r.0 - p.0);
    let (d1, d2) = (side(c, d, a), side(c, d, b));
    let (d3, d4) = (side(a, b, c), side(a, b, d));
    if (d1 > 0.0) != (d2 > 0.0)
        && (d3 > 0.0) != (d4 > 0.0)
        && d1 != 0.0
        && d2 != 0.0
        && d3 != 0.0
        && d4 != 0.0
    {
        return 0.0;
    }
    point_segment(a, c, d)
        .min(point_segment(b, c, d))
        .min(point_segment(c, a, b))
        .min(point_segment(d, a, b))
}

/// `geometry.segment_box`.
fn segment_box(a: Point, b: Point, (left, top, right, bottom): (f64, f64, f64, f64)) -> f64 {
    if [a, b]
        .iter()
        .any(|p| left <= p.0 && p.0 <= right && top <= p.1 && p.1 <= bottom)
    {
        return 0.0;
    }
    let corners = [(left, top), (right, top), (right, bottom), (left, bottom)];
    (0..4)
        .map(|k| segment_segment(a, b, corners[k], corners[(k + 1) % 4]))
        .fold(f64::INFINITY, f64::min)
}

/// `geometry.inside`.
fn inside(p: Point, polygon: &[Point]) -> bool {
    let (x, y) = p;
    let mut within = false;
    let n = polygon.len();
    for k in 0..n {
        let ((ax, ay), (bx, by)) = (polygon[k], polygon[(k + 1) % n]);
        if (ay > y) != (by > y) && x < (bx - ax) * (y - ay) / (by - ay) + ax {
            within = !within;
        }
    }
    within
}

/// `geometry.segment_polygon`.
fn segment_polygon(a: Point, b: Point, polygon: &[Point]) -> f64 {
    if inside(a, polygon) || inside(b, polygon) {
        return 0.0;
    }
    let n = polygon.len();
    (0..n)
        .map(|k| segment_segment(a, b, polygon[k], polygon[(k + 1) % n]))
        .fold(f64::INFINITY, f64::min)
}

/// One net's copper (`grid._Shape`).
enum Geo {
    Box((f64, f64, f64, f64)),
    Capsule(Point, Point, f64),
    Polygon(Vec<Point>),
}

struct Copper {
    geo: Geo,
    number: i64,
    class: i64,
    /// Its routing layer's index, -1 for every layer, -2 for none routed.
    layer: i64,
}

impl Copper {
    /// `grid._bounds`.
    fn bounds(&self) -> (f64, f64, f64, f64) {
        match &self.geo {
            Geo::Box(b) => *b,
            Geo::Polygon(points) => points.iter().fold(
                (
                    f64::INFINITY,
                    f64::INFINITY,
                    f64::NEG_INFINITY,
                    f64::NEG_INFINITY,
                ),
                |(l, t, r, b), &(x, y)| (l.min(x), t.min(y), r.max(x), b.max(y)),
            ),
            Geo::Capsule(s, e, r) => (
                s.0.min(e.0) - r,
                s.1.min(e.1) - r,
                s.0.max(e.0) + r,
                s.1.max(e.1) + r,
            ),
        }
    }

    /// `grid._distance` from a line of no width.
    fn distance(&self, a: Point, b: Point) -> f64 {
        match &self.geo {
            Geo::Box(bx) => segment_box(a, b, *bx),
            Geo::Polygon(points) => segment_polygon(a, b, points),
            Geo::Capsule(s, e, r) => segment_segment(a, b, *s, *e) - r,
        }
    }
}

type CopperArg = (
    i64,
    i64,
    i64,
    Option<(f64, f64, f64, f64)>,
    Option<(Point, Point, f64)>,
    Option<Vec<Point>>,
);

/// A board's fixed copper, for measuring diagonal steps against.
#[pyclass]
pub struct Grazer {
    copper: Vec<Copper>,
    buckets: FxHashMap<(i64, i64), Vec<usize>>,
    memo: FxHashMap<i64, Vec<(i64, f64, i64)>>,
}

impl Grazer {
    /// `grid._Near.around`: the copper whose buckets lie within reach.
    fn around(&self, (left, top, right, bottom): (f64, f64, f64, f64), reach: f64) -> Vec<usize> {
        let mut seen = FxHashSet::default();
        let mut found = Vec::new();
        for bx in (left - reach).floor() as i64..=(right + reach).floor() as i64 {
            for by in (top - reach).floor() as i64..=(bottom + reach).floor() as i64 {
                if let Some(list) = self.buckets.get(&(bx, by)) {
                    for &k in list {
                        if seen.insert(k) {
                            found.push(k);
                        }
                    }
                }
            }
        }
        found
    }
}

#[pymethods]
impl Grazer {
    /// Each piece of copper: its net's number and class (-1 for none), its
    /// layer, and one of a box, a capsule (start, end, radius) or a polygon.
    #[new]
    fn new(copper: Vec<CopperArg>) -> PyResult<Self> {
        let mut held = Vec::with_capacity(copper.len());
        for (number, class, layer, rect, capsule, polygon) in copper {
            let geo = match (rect, capsule, polygon) {
                (Some(b), _, _) => Geo::Box(b),
                (None, _, Some(p)) => Geo::Polygon(p),
                (None, Some((s, e, r)), None) => Geo::Capsule(s, e, r),
                (None, None, None) => {
                    return Err(pyo3::exceptions::PyValueError::new_err(
                        "a piece of copper with no shape",
                    ))
                }
            };
            held.push(Copper {
                geo,
                number,
                class,
                layer,
            });
        }
        let mut buckets: FxHashMap<(i64, i64), Vec<usize>> = FxHashMap::default();
        for (k, c) in held.iter().enumerate() {
            let (left, top, right, bottom) = c.bounds();
            for bx in left.floor() as i64..=right.floor() as i64 {
                for by in top.floor() as i64..=bottom.floor() as i64 {
                    buckets.entry((bx, by)).or_default().push(k);
                }
            }
        }
        Ok(Grazer {
            copper: held,
            buckets,
            memo: FxHashMap::default(),
        })
    }

    /// One class's grazing steps (`grid._grazes`): by cell times 8 plus the
    /// move, both ways, the numbers of the nets they pass too near.
    #[allow(clippy::too_many_arguments)]
    fn grazes<'py>(
        &mut self,
        py: Python<'py>,
        maps: Vec<PyBuffer<i32>>,
        nx: i64,
        ny: i64,
        xs: Vec<f64>,
        ys: Vec<f64>,
        open_cells: FxHashSet<i64>,
        need_of: Vec<f64>,
        own_clearance: f64,
        half: f64,
        reach: f64,
    ) -> PyResult<Bound<'py, PyDict>> {
        let out = PyDict::new(py);
        let plane = nx * ny;
        for (layer, map) in maps.iter().enumerate() {
            let foe = cells(map)?;
            let layer = layer as i64;
            let base = layer * plane;
            for j in 1..ny - 1 {
                for i in 1..nx - 1 {
                    let cell = j * nx + i;
                    let held = foe[cell as usize];
                    if held == -1 && !open_cells.contains(&(base + cell)) {
                        continue;
                    }
                    // Two of the four diagonals from each cell; the others
                    // are the same steps taken from the far end.
                    for (d, di, dj) in [(1_i64, 1_i64, 1_i64), (3, -1, 1)] {
                        if foe[(cell + di) as usize] == 0 && foe[(cell + dj * nx) as usize] == 0 {
                            continue;
                        }
                        let far = cell + di + dj * nx;
                        let there = foe[far as usize];
                        if !open_cells.contains(&(base + far))
                            && (there == -1 || (held > 0 && there > 0 && held != there))
                        {
                            continue;
                        }
                        let key = (base + cell) * 8 + d;
                        if !self.memo.contains_key(&key) {
                            let start = (xs[i as usize], ys[j as usize]);
                            let end = (xs[(i + di) as usize], ys[(j + dj) as usize]);
                            let bounds = (
                                start.0.min(end.0),
                                start.1.min(end.1),
                                start.0.max(end.0),
                                start.1.max(end.1),
                            );
                            let measured = self
                                .around(bounds, reach)
                                .into_iter()
                                .map(|k| &self.copper[k])
                                .filter(|c| c.layer == -1 || c.layer == layer)
                                .map(|c| (c.number, c.distance(start, end), c.class))
                                .collect();
                            self.memo.insert(key, measured);
                        }
                        let mut offenders: Vec<i64> = self.memo[&key]
                            .iter()
                            .filter(|&&(_, apart, theirs)| {
                                let need = if theirs >= 0 {
                                    need_of[theirs as usize]
                                } else {
                                    own_clearance
                                };
                                apart - need < half - 1e-9
                            })
                            .map(|&(number, _, _)| number)
                            .collect();
                        if offenders.is_empty() {
                            continue;
                        }
                        offenders.sort_unstable();
                        offenders.dedup();
                        let found = PyTuple::new(py, &offenders)?;
                        out.set_item(key, &found)?;
                        out.set_item((base + far) * 8 + (d + 4) % 8, &found)?;
                    }
                }
            }
        }
        Ok(out)
    }
}
