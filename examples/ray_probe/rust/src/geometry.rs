//! Pure geometry in the event's lattice; no host requests or SDK state.
use std::collections::HashSet;
use vizi::Position;
pub type Cell = [i64; 3];
pub type Point = [f64; 3];
pub type Bounds = (Cell, Cell); // inclusive centers
pub const MAX_VOXELS: u64 = 1 << 26;
pub fn level0(c: f64, s: f64) -> f64 {
    (c + 0.5) * s - 0.5
}
pub fn crop(edges: [[f64; 2]; 3], scale: Point, shape: Cell) -> Option<Bounds> {
    let lo = std::array::from_fn(|a| (((edges[a][0] + 0.5) / scale[a] - 0.5).ceil() as i64).max(0));
    let hi = std::array::from_fn(|a| {
        (((edges[a][1] + 0.5) / scale[a] - 0.5).floor() as i64).min(shape[a] - 1)
    });
    (0..3).all(|a| lo[a] <= hi[a]).then_some((lo, hi))
}
pub fn segment(p: &Position) -> Option<[Point; 2]> {
    match p {
        Position::Ray(r) | Position::RayAndPicked { ray: r, .. } => {
            let far = r.far.map(f64::from);
            let eye = r.camera.map(f64::from);
            Some([
                std::array::from_fn(|a| eye[a] + f64::from(r.entry_frac) * (far[a] - eye[a])),
                far,
            ])
        }
        Position::ColumnYx { y, x, z_min, z_max } => Some([
            [*z_min as f64, *y as f64, *x as f64],
            [*z_max as f64, *y as f64, *x as f64],
        ]),
        Position::ColumnZx { z, x, y_min, y_max } => Some([
            [*z as f64, *y_min as f64, *x as f64],
            [*z as f64, *y_max as f64, *x as f64],
        ]),
        Position::ColumnZy { z, y, x_min, x_max } => Some([
            [*z as f64, *y as f64, *x_min as f64],
            [*z as f64, *y as f64, *x_max as f64],
        ]),
        _ => None,
    }
}
/// Columns aim at the first/last included centers, but their search range
/// remains half-open. Keep this separate from the range decoder.
pub fn aim_segment(p: &Position) -> Option<[Point; 2]> {
    let mut s = segment(p)?;
    match p {
        Position::ColumnYx { .. } => s[1][0] -= 1.,
        Position::ColumnZx { .. } => s[1][1] -= 1.,
        Position::ColumnZy { .. } => s[1][2] -= 1.,
        _ => {}
    }
    Some(s)
}
fn inside(c: Cell, (lo, hi): Bounds) -> bool {
    (0..3).all(|a| lo[a] <= c[a] && c[a] <= hi[a])
}
fn round(p: Point) -> Cell {
    p.map(|v| (v + 0.5).floor() as i64)
}
/// Slab clipping bounds work before allocating traversal storage.
fn clip_segment([p, q]: [Point; 2], (lo, hi): Bounds) -> Option<[Point; 2]> {
    let d: Point = std::array::from_fn(|a| q[a] - p[a]);
    let (mut enter, mut exit) = (0.0_f64, 1.0_f64);
    for a in 0..3 {
        let (low, high) = (lo[a] as f64 - 0.5, hi[a] as f64 + 0.5);
        if d[a] == 0. {
            if p[a] < low || p[a] > high {
                return None;
            }
        } else {
            let (t, u) = ((low - p[a]) / d[a], (high - p[a]) / d[a]);
            enter = enter.max(t.min(u));
            exit = exit.min(t.max(u));
        }
    }
    if enter > exit {
        return None;
    }
    Some([
        std::array::from_fn(|a| p[a] + enter * d[a]),
        std::array::from_fn(|a| p[a] + exit * d[a]),
    ])
}
/// Sweep the boundary parameters, visiting closed cells at each crossing and
/// inside every intervening interval. A k-way crossing emits all 2^k cells.
/// Unlike repeated t_delta additions, each crossing is computed from the ray.
pub fn traverse([p, q]: [Point; 2]) -> Vec<Cell> {
    let d: Point = std::array::from_fn(|a| q[a] - p[a]);
    let mut times = vec![0., 1.];
    for a in 0..3 {
        if d[a] == 0. {
            continue;
        }
        let first = (p[a].min(q[a]) + 0.5).ceil() as i64;
        let last = (p[a].max(q[a]) + 0.5).floor() as i64;
        for k in first..=last {
            let t = (k as f64 - 0.5 - p[a]) / d[a];
            if (0.0..=1.0).contains(&t) {
                times.push(t);
            }
        }
    }
    times.sort_by(f64::total_cmp);
    times.dedup_by(|a, b| *a == *b);
    let mut seen = HashSet::new();
    let mut out = Vec::new();
    let mut emit = |t: f64| {
        let point: Point = std::array::from_fn(|a| p[a] + t * d[a]);
        let mut choices = [[0; 2]; 3];
        for a in 0..3 {
            let u = point[a] + 0.5;
            // Arithmetic at a crossing may land a few ulps from its plane.
            if (u - u.round()).abs() <= 8.0 * f64::EPSILON * u.abs().max(1.0) {
                choices[a] = [u.round() as i64 - 1, u.round() as i64];
            } else {
                choices[a] = [u.floor() as i64; 2];
            }
        }
        for z in choices[0] {
            for y in choices[1] {
                for x in choices[2] {
                    let c = [z, y, x];
                    if seen.insert(c) {
                        out.push(c);
                    }
                }
            }
        }
    };
    for i in 0..times.len() {
        emit(times[i]);
        if i + 1 < times.len() {
            emit((times[i] + times[i + 1]) * 0.5);
        }
    }
    out
}
pub fn cells(position: &Position, bounds: Bounds) -> Result<Vec<Cell>, String> {
    match position {
        Position::Voxel(p) | Position::Picked(p) => {
            let c = round(p.map(f64::from));
            Ok(if inside(c, bounds) { vec![c] } else { vec![] })
        }
        Position::None => Ok(vec![]),
        Position::Ray(_) | Position::RayAndPicked { .. } => {
            let Some(s) = clip_segment(segment(position).unwrap(), bounds) else {
                return Ok(vec![]);
            };
            let lo = std::array::from_fn(|a| {
                ((s[0][a].min(s[1][a]) - 0.5).ceil() as i64).max(bounds.0[a])
            });
            let hi = std::array::from_fn(|a| {
                ((s[0][a].max(s[1][a]) + 0.5).floor() as i64).min(bounds.1[a]) + 1
            });
            budget((lo, hi))?;
            Ok(traverse(s)
                .into_iter()
                .filter(|&c| inside(c, bounds))
                .collect())
        }
        _ => {
            let [p, q] = segment(position).unwrap();
            let axis = match position {
                Position::ColumnYx { .. } => 0,
                Position::ColumnZx { .. } => 1,
                _ => 2,
            };
            let mut c = round(p);
            let first = (p[axis].ceil() as i64).max(bounds.0[axis]);
            let last = (q[axis].ceil() as i64 - 1).min(bounds.1[axis]);
            if last.saturating_sub(first) as u64 >= MAX_VOXELS && last >= first {
                return Err("Too large".into());
            }
            let mut out = Vec::new();
            for k in first..=last {
                c[axis] = k;
                if inside(c, bounds) {
                    out.push(c);
                }
            }
            Ok(out)
        }
    }
}
pub fn aabb(cells: &[Cell]) -> Option<Bounds> {
    let &first = cells.first()?;
    let (mut lo, mut hi) = (first, first.map(|x| x + 1));
    for c in cells {
        for a in 0..3 {
            lo[a] = lo[a].min(c[a]);
            hi[a] = hi[a].max(c[a] + 1);
        }
    }
    Some((lo, hi))
}
pub fn budget((lo, hi): Bounds) -> Result<(), String> {
    let mut n = 1u64;
    for a in 0..3 {
        n = n
            .checked_mul((hi[a] - lo[a]).max(0) as u64)
            .ok_or("Too large")?;
    }
    if n > MAX_VOXELS {
        Err("Too large".into())
    } else {
        Ok(())
    }
}
pub fn block(cell: Cell, scale: Point, extent: Bounds) -> Bounds {
    (
        std::array::from_fn(|a| ((cell[a] as f64 * scale[a]).ceil() as i64).max(extent.0[a])),
        std::array::from_fn(|a| (((cell[a] + 1) as f64 * scale[a]).ceil() as i64).min(extent.1[a])),
    )
}
pub fn argmax(values: &[f32]) -> Option<usize> {
    let mut best = None;
    for (i, &v) in values.iter().enumerate() {
        if !v.is_nan() && best.is_none_or(|j| v > values[j]) {
            best = Some(i);
        }
    }
    best
}
pub fn distances(cells: &[Cell], spacing: Point) -> Vec<f64> {
    let mut out = vec![0.; cells.len()];
    for i in 1..cells.len() {
        out[i] = out[i - 1]
            + (0..3)
                .map(|a| ((cells[i][a] - cells[i - 1][a]) as f64 * spacing[a]).powi(2))
                .sum::<f64>()
                .sqrt();
    }
    out
}
