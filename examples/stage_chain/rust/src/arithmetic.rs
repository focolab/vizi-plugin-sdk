//! Pure voxel arithmetic. These buffers contain plane data, never wire headers.
use std::collections::BTreeMap;
use vizi::{
    proto::DType,
    stage::{Pose, Voxel},
};

pub struct Plane {
    pub shape: [u32; 3],
    pub dtype: DType,
    pub bytes: Vec<u8>,
}

fn indices(extent: [[u32; 2]; 3]) -> impl Iterator<Item = [u32; 3]> {
    (extent[0][0]..extent[0][1]).flat_map(move |z| {
        (extent[1][0]..extent[1][1])
            .flat_map(move |y| (extent[2][0]..extent[2][1]).map(move |x| [z, y, x]))
    })
}

fn as_f64(v: Voxel) -> f64 {
    match v {
        Voxel::U8(v) => v.into(),
        Voxel::U16(v) => v.into(),
        Voxel::F32(v) => v.into(),
        Voxel::F64(v) => v,
    }
}

impl Plane {
    pub fn width(&self) -> usize {
        match self.dtype {
            DType::DtypeU8 => 1,
            DType::DtypeU16 => 2,
            DType::DtypeF32 => 4,
            DType::DtypeF64 => 8,
            _ => unreachable!("SDK checked dtype"),
        }
    }
    pub fn offset(&self, [z, y, x]: [u32; 3]) -> usize {
        ((z as usize * self.shape[1] as usize + y as usize) * self.shape[2] as usize + x as usize)
            * self.width()
    }
    pub fn voxel(&self, at: [u32; 3]) -> Voxel {
        let offset = self.offset(at);
        let bytes = &self.bytes[offset..offset + self.width()];
        match self.dtype {
            DType::DtypeU8 => Voxel::U8(bytes[0]),
            DType::DtypeU16 => Voxel::U16(u16::from_le_bytes(bytes.try_into().unwrap())),
            DType::DtypeF32 => Voxel::F32(f32::from_le_bytes(bytes.try_into().unwrap())),
            DType::DtypeF64 => Voxel::F64(f64::from_le_bytes(bytes.try_into().unwrap())),
            _ => unreachable!("SDK checked dtype"),
        }
    }
    pub fn set(&mut self, at: [u32; 3], v: Voxel) {
        let offset = self.offset(at);
        let width = self.width();
        let out = &mut self.bytes[offset..offset + width];
        match v {
            Voxel::U8(v) => out.copy_from_slice(&[v]),
            Voxel::U16(v) => out.copy_from_slice(&v.to_le_bytes()),
            Voxel::F32(v) => out.copy_from_slice(&v.to_le_bytes()),
            Voxel::F64(v) => out.copy_from_slice(&v.to_le_bytes()),
        }
    }
    pub fn gain(&mut self, extent: [[u32; 2]; 3], factor: f64) {
        for at in indices(extent) {
            let p = as_f64(self.voxel(at)) * factor;
            let value = match self.dtype {
                DType::DtypeU8 => Voxel::U8(p.round_ties_even().clamp(0.0, 255.0) as u8),
                DType::DtypeU16 => Voxel::U16(p.round_ties_even().clamp(0.0, 65535.0) as u16),
                DType::DtypeF32 => Voxel::F32(p as f32),
                DType::DtypeF64 => Voxel::F64(p),
                _ => unreachable!("SDK checked dtype"),
            };
            self.set(at, value);
        }
    }
    pub fn diff(&self, b: &Self, extent: [[u32; 2]; 3]) -> Self {
        let mut result = Self {
            shape: self.shape,
            dtype: self.dtype,
            bytes: vec![0; self.bytes.len()],
        };
        for at in indices(extent) {
            let value = match (self.voxel(at), b.voxel(at)) {
                (Voxel::U8(a), Voxel::U8(b)) => Voxel::U8(a.abs_diff(b)),
                (Voxel::U16(a), Voxel::U16(b)) => Voxel::U16(a.abs_diff(b)),
                (Voxel::F32(a), Voxel::F32(b)) => Voxel::F32((a - b).abs()),
                (Voxel::F64(a), Voxel::F64(b)) => Voxel::F64((a - b).abs()),
                _ => unreachable!("planes have the same dtype"),
            };
            result.set(at, value);
        }
        result
    }
    pub fn widen(&self, crop: u32) -> Self {
        let shape = [
            self.shape[0],
            self.shape[1] - 2 * crop,
            self.shape[2] - 2 * crop,
        ];
        let mut result = Self {
            shape,
            dtype: DType::DtypeF32,
            bytes: vec![0; shape.iter().map(|&n| n as usize).product::<usize>() * 4],
        };
        for [z, y, x] in indices(shape.map(|n| [0, n])) {
            result.set(
                [z, y, x],
                Voxel::F32(as_f64(self.voxel([z, y + crop, x + crop])) as f32),
            );
        }
        result
    }
    pub fn centroid(&self, extent: [[u32; 2]; 3], origin: [f64; 3]) -> Option<[f64; 3]> {
        let mut sum = 0u64;
        let mut weighted = [0u64; 3];
        for at in indices(extent) {
            let v = match self.voxel(at) {
                Voxel::U8(v) => u64::from(v),
                Voxel::U16(v) => u64::from(v),
                _ => unreachable!("drift plans only u8/u16"),
            };
            sum += v;
            for axis in 0..3 {
                weighted[axis] += v * u64::from(at[axis]);
            }
        }
        (sum != 0).then(|| {
            std::array::from_fn(|axis| {
                origin[axis] + (weighted[axis] as f64 / sum as f64 - f64::from(extent[axis][0]))
            })
        })
    }
}

pub fn placement(
    extent: [[u32; 2]; 3],
    origin: [f64; 3],
    shape: [u32; 3],
    crop: u32,
) -> ([[u32; 2]; 3], [f64; 3]) {
    let offset = [0, crop, crop];
    let next = std::array::from_fn(|axis| {
        extent[axis].map(|v| v.saturating_sub(offset[axis]).min(shape[axis]))
    });
    let origin = std::array::from_fn(|axis| {
        origin[axis]
            + (f64::from(next[axis][0]) + f64::from(offset[axis]) - f64::from(extent[axis][0]))
    });
    (next, origin)
}

struct History {
    t: u32,
    reference: Option<[f64; 3]>,
}
#[derive(Default)]
pub struct Drift(BTreeMap<(u32, u32), History>);
impl Drift {
    /// Check before reading or writing the slot; a gap forgets this arm only.
    pub fn accept_time(&mut self, key: (u32, u32), t: u32) -> bool {
        if self
            .0
            .get(&key)
            .is_some_and(|old| old.t.checked_add(1) != Some(t))
        {
            self.0.remove(&key);
            return false;
        }
        true
    }
    pub fn apply(&mut self, key: (u32, u32), t: u32, centroid: Option<[f64; 3]>, pose: &mut Pose) {
        let old = self.0.entry(key).or_insert(History {
            t,
            reference: centroid,
        });
        if let (Some(reference), Some(current)) = (old.reference, centroid) {
            pose.compose(&Pose {
                rotation: [1., 0., 0., 0., 1., 0., 0., 0., 1.],
                translation: std::array::from_fn(|axis| reference[axis] - current[axis]),
            });
        }
        old.t = t;
    }
}
