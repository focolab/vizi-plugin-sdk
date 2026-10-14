use std::sync::atomic::{AtomicBool, Ordering};

pub const MAX_COMPONENT: usize = 8_000_000;
pub type Result<T> = std::result::Result<T, String>;

pub fn inside(path: &[[f64; 3]], py: f64, px: f64) -> bool {
    let mut result = false;
    for (a, b) in path
        .iter()
        .zip(path.iter().cycle().skip(1))
        .take(path.len())
    {
        if (a[1] > py) != (b[1] > py) {
            let cross = a[2] + (py - a[1]) * (b[2] - a[2]) / (b[1] - a[1]);
            if px < cross {
                result = !result;
            }
        }
    }
    result
}

pub fn preview_indices(n: usize) -> Vec<usize> {
    let k = n.min(512);
    if k <= 1 {
        return (0..k).collect();
    }
    let mut indices: Vec<_> = (0..k)
        .map(|i| (i as f64 * (n - 1) as f64 / (k - 1) as f64).round_ties_even() as usize)
        .collect();
    indices.dedup();
    indices
}

pub struct Selection {
    pub origin: [usize; 3],
    pub shape: [usize; 3],
    pub selected: Vec<bool>,
    pub volume_shape: [usize; 3],
}

pub fn select(path: &[[f64; 3]], volume_shape: [usize; 3]) -> Result<Selection> {
    let mut lo = [usize::MAX; 2];
    let mut hi = [0; 2];
    let mut selected = Vec::new();
    let bounds = |axis: usize| {
        let min = path
            .iter()
            .map(|p| p[axis])
            .fold(f64::INFINITY, f64::min)
            .floor()
            .max(0.0) as usize;
        let max = path
            .iter()
            .map(|p| p[axis])
            .fold(f64::NEG_INFINITY, f64::max)
            .ceil()
            .max(0.0) as usize;
        min.min(volume_shape[axis])..max.min(volume_shape[axis])
    };
    for y in bounds(1) {
        for x in bounds(2) {
            if inside(path, y as f64 + 0.5, x as f64 + 0.5) {
                selected.push([y, x]);
                lo[0] = lo[0].min(y);
                lo[1] = lo[1].min(x);
                hi[0] = hi[0].max(y + 1);
                hi[1] = hi[1].max(x + 1);
            }
        }
    }
    if selected.is_empty() {
        return Err("Drag a closed loop around a region.".into());
    }
    for axis in 0..2 {
        let margin = (0.25 * (hi[axis] - lo[axis]) as f64)
            .round_ties_even()
            .max(8.0) as usize;
        lo[axis] = lo[axis].saturating_sub(margin);
        hi[axis] = hi[axis].saturating_add(margin).min(volume_shape[axis + 1]);
    }
    let shape = [volume_shape[0], hi[0] - lo[0], hi[1] - lo[1]];
    let mut mask = vec![false; shape[1] * shape[2]];
    for [y, x] in selected {
        mask[(y - lo[0]) * shape[2] + x - lo[1]] = true;
    }
    Ok(Selection {
        origin: [0, lo[0], lo[1]],
        shape,
        selected: mask,
        volume_shape,
    })
}

pub struct Component {
    pub voxels: Vec<[usize; 3]>,
    pub threshold: f64,
    pub clipped: bool,
}

pub fn grow(
    w: &[f32],
    s: &Selection,
    z_g: usize,
    threshold: f64,
    grow_3d: bool,
    min_size: usize,
    stop: &AtomicBool,
) -> Result<Component> {
    let [z, y, x] = s.shape;
    if w.len() != z * y * x || z_g >= z {
        return Err("Volume or drawn plane is out of bounds".into());
    }
    let plane = y * x;
    let mut visited = vec![false; w.len()];
    let mut best = Vec::new();
    let range = if grow_3d {
        0..w.len()
    } else {
        z_g * plane..(z_g + 1) * plane
    };
    for start in range {
        if start % 4096 == 0 && stop.load(Ordering::Relaxed) {
            return Err("Cancelled".into());
        }
        let foreground = f64::from(w[start]) >= threshold;
        if visited[start] || !foreground {
            continue;
        }
        let mut component = vec![start];
        visited[start] = true;
        let mut cursor = 0;
        let mut anchored = false;
        while cursor < component.len() {
            if cursor % 4096 == 0 && stop.load(Ordering::Relaxed) {
                return Err("Cancelled".into());
            }
            let i = component[cursor];
            cursor += 1;
            let zz = i / plane;
            let yy = i % plane / x;
            let xx = i % x;
            anchored |= zz == z_g && s.selected[i % plane];
            let candidates = [
                (xx > 0, i.wrapping_sub(1)),
                (xx + 1 < x, i + 1),
                (yy > 0, i.wrapping_sub(x)),
                (yy + 1 < y, i + x),
                (grow_3d && zz > 0, i.wrapping_sub(plane)),
                (grow_3d && zz + 1 < z, i + plane),
            ];
            for (valid, next) in candidates {
                if valid && !visited[next] && f64::from(w[next]) >= threshold {
                    visited[next] = true;
                    component.push(next);
                }
            }
        }
        // Raster scan + strict greater-than preserves the first component on ties.
        if anchored && component.len() > best.len() {
            best = component;
        }
    }
    if best.is_empty() {
        let mut peak = f64::NEG_INFINITY;
        let mut peak_z = None;
        for (i, &v) in w.iter().enumerate() {
            if s.selected[i % plane] && !v.is_nan() && (peak_z.is_none() || f64::from(v) > peak) {
                peak = f64::from(v);
                peak_z = Some(i / plane);
            }
        }
        return Err(format!("Nothing above the threshold on plane {z_g}. The brightest voxel you enclosed is at z={} — scroll there, or lower Threshold ×.", peak_z.unwrap_or(z_g)));
    }
    if best.len() < min_size {
        return Err(format!("No component of at least {min_size} voxels."));
    }
    if best.len() > MAX_COMPONENT {
        return Err(
            "Selection too permissive — try a tighter loop or a higher Threshold ×.".into(),
        );
    }
    let voxels: Vec<_> = best
        .into_iter()
        .map(|i| [i / plane, i % plane / x + s.origin[1], i % x + s.origin[2]])
        .collect();
    let clipped = voxels.iter().any(|p| {
        (0..3).any(|a| {
            (p[a] == s.origin[a] && s.origin[a] > 0)
                || (p[a] + 1 == s.origin[a] + s.shape[a] && p[a] + 1 < s.volume_shape[a])
        })
    });
    Ok(Component {
        voxels,
        threshold,
        clipped,
    })
}

#[derive(Clone, Default)]
pub struct Labels {
    pub origin: [usize; 3],
    pub shape: [usize; 3],
    pub bytes: Vec<u8>,
    pub next: u16,
}

impl Labels {
    pub fn add(&self, voxels: &[[usize; 3]], replace: bool) -> Result<(Self, u8)> {
        let previous = if replace {
            Self::default()
        } else {
            self.clone()
        };
        let id = previous.next.max(1);
        if id > 255 {
            return Err("At most 255 labels; clear or use Replace.".into());
        }
        let mut lo = [usize::MAX; 3];
        let mut hi = [0; 3];
        for p in voxels {
            for a in 0..3 {
                lo[a] = lo[a].min(p[a]);
                hi[a] = hi[a].max(p[a] + 1);
            }
        }
        if !previous.bytes.is_empty() {
            for a in 0..3 {
                lo[a] = lo[a].min(previous.origin[a]);
                hi[a] = hi[a].max(previous.origin[a] + previous.shape[a]);
            }
        }
        let shape = std::array::from_fn(|a| hi[a] - lo[a]);
        let count = shape
            .iter()
            .try_fold(1usize, |n, &v| n.checked_mul(v))
            .ok_or("Label box is too large")?;
        let mut result = Self {
            origin: lo,
            shape,
            bytes: vec![0; count],
            next: id + 1,
        };
        for (i, &value) in previous.bytes.iter().enumerate() {
            if value != 0 {
                let p = [
                    i / (previous.shape[1] * previous.shape[2]) + previous.origin[0],
                    i / previous.shape[2] % previous.shape[1] + previous.origin[1],
                    i % previous.shape[2] + previous.origin[2],
                ];
                let j = result.index(p);
                result.bytes[j] = value;
            }
        }
        for &p in voxels {
            let j = result.index(p);
            result.bytes[j] = id as u8;
        }
        Ok((result, id as u8))
    }
    fn index(&self, p: [usize; 3]) -> usize {
        ((p[0] - self.origin[0]) * self.shape[1] + p[1] - self.origin[1]) * self.shape[2] + p[2]
            - self.origin[2]
    }
}

pub fn lut() -> Vec<u8> {
    let colors = [
        [230, 25, 75],
        [60, 180, 75],
        [67, 99, 216],
        [245, 130, 49],
        [145, 30, 180],
        [66, 212, 244],
        [240, 50, 230],
        [191, 239, 69],
    ];
    let mut result = vec![0; 1024];
    for k in 1..256 {
        result[k * 4..k * 4 + 3].copy_from_slice(&colors[(k - 1) % 8]);
        result[k * 4 + 3] = 180;
    }
    result
}
