//! In-place processing stages (processing_stages.md). Layout changes and
//! repacking are the author's responsibility; the SDK owns header/turn writes.
use crate::{
    control::{Context, Error, Result},
    memory::{dtype_bytes, Turn},
    proto as pb,
};
use std::collections::BTreeSet;
pub(crate) mod params;
pub use params::{ParamSpec, ParamType, ParamValue, Params, STRING_PARAM_MAX_BYTES};

pub trait Stage: Send + Sync + 'static {
    /// Plan the largest output for this input and these checked position settings.
    /// During processing, the accepted settings are available through Slot::params.
    fn plan(
        &self,
        ctx: &Context,
        input: &pb::StageShape,
        params: &Params,
    ) -> Result<pb::StagePlanned>;
    /// Runs on this stage's worker, independently of the control dispatcher.
    /// Per processing_stages.md §6.3, refuse BEFORE writing any data. If parameters
    /// no longer fit arm.output, return Refused(Other) until the host re-arms.
    /// Errors/panics cannot undo writes; the host discards failed volumes.
    fn process(&self, ctx: &Context, arm: &pb::StageArm, slot: &mut Slot) -> Result<Outcome>;
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Refusal {
    NeedsEarlierState = 1,
    MissingInput = 2,
    Other = 3,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Failure {
    Internal = 2,
    Other = 3,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Outcome {
    Processed,
    Refused(Refusal),
    Failed(Failure),
}
#[derive(Clone, Debug, PartialEq)]
pub struct Layout {
    /// Channel identity in plane order, not the plane number.
    pub channels: Vec<u16>,
    pub allocation: [u32; 3],
    pub dtype: pb::DType,
}
impl Layout {
    pub fn plane_bytes(&self) -> Result<u64> {
        self.allocation
            .iter()
            .try_fold(dtype_bytes(self.dtype as i32)?, |n, &v| {
                n.checked_mul(v as u64)
                    .ok_or_else(|| Error::protocol("stage layout byte count overflow"))
            })
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct Pose {
    /// Row-major rotation acting on column (z, y, x).
    pub rotation: [f64; 9],
    pub translation: [f64; 3],
}
impl Pose {
    /// The header pose is absolute. Compose relative output→input motion using
    /// the OLD rotation for both products (processing_stages.md §5.5).
    pub fn compose(&mut self, motion: &Pose) {
        let old = self.rotation;
        for i in 0..3 {
            self.translation[i] += (0..3)
                .map(|j| old[3 * i + j] * motion.translation[j])
                .sum::<f64>();
            for j in 0..3 {
                self.rotation[3 * i + j] = (0..3)
                    .map(|k| old[3 * i + k] * motion.rotation[3 * k + j])
                    .sum();
            }
        }
    }
}
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Voxel {
    U8(u8),
    U16(u16),
    F32(f32),
    F64(f64),
}

/// A checked view of the CURRENT packed layout. Shared bytes are accessed
/// through bounded operations, never a Rust reference escaping the mapping.
/// All offsets passed to read/write_bytes are relative to data, not the header.
/// Scratch buffers and overlap-safe repacking are explicitly author-owned.
pub struct Slot {
    pub layout: Layout,
    pub extent: [[u32; 2]; 3],
    pub origin: [f64; 3],
    pub pose: Pose,
    turn: Turn,
    params: Params,
    plane_cap: u32,
    capacity: u64,
    header_bytes: u64,
    t: u32,
    open_generation: u64,
}
fn u32_at(b: &[u8], i: usize) -> u32 {
    u32::from_le_bytes(b[i..i + 4].try_into().unwrap())
}
fn u64_at(b: &[u8], i: usize) -> u64 {
    u64::from_le_bytes(b[i..i + 8].try_into().unwrap())
}
fn f64_at(b: &[u8], i: usize) -> f64 {
    f64::from_le_bytes(b[i..i + 8].try_into().unwrap())
}
impl Slot {
    pub(crate) fn open(
        turn: Turn,
        params: Params,
    ) -> std::result::Result<Self, Box<(Turn, Error)>> {
        let decode = (|| -> Result<_> {
            let mut fixed = vec![0; 8]; // Never read the atomic word non-atomically.
            fixed.extend(turn.read(8, 248)?);
            let plane_cap = u32_at(&fixed, 36);
            let planes = u32_at(&fixed, 32);
            if plane_cap > 65536 || planes > plane_cap {
                return Err(Error::protocol("invalid stage plane count"));
            }
            let header_bytes = (256 + 2 * u64::from(plane_cap)).div_ceil(64) * 64;
            let capacity = u64_at(&fixed, 56);
            if header_bytes
                .checked_add(capacity)
                .is_none_or(|n| n > turn.region.extent)
            {
                return Err(Error::protocol("stage header and capacity exceed region"));
            }
            if fixed[12..16]
                .iter()
                .chain(&fixed[52..56])
                .chain(&fixed[216..256])
                .any(|&b| b != 0)
            {
                return Err(Error::protocol("nonzero stage header reserved bytes"));
            }
            let channels = turn
                .read(256, 2 * u64::from(planes))?
                .as_chunks::<2>()
                .0
                .iter()
                .map(|b| u16::from_le_bytes(*b))
                .collect();
            let layout = Layout {
                channels,
                allocation: std::array::from_fn(|i| u32_at(&fixed, 40 + 4 * i)),
                dtype: pb::DType::try_from(u32_at(&fixed, 28) as i32)
                    .map_err(|_| Error::protocol("unknown stage dtype"))?,
            };
            let extent =
                std::array::from_fn(|i| [u32_at(&fixed, 64 + 8 * i), u32_at(&fixed, 68 + 8 * i)]);
            let origin = std::array::from_fn(|i| f64_at(&fixed, 88 + 8 * i));
            let pose = Pose {
                rotation: std::array::from_fn(|i| f64_at(&fixed, 112 + 8 * i)),
                translation: std::array::from_fn(|i| f64_at(&fixed, 184 + 8 * i)),
            };
            validate_header(&layout, &extent, &origin, &pose, plane_cap, capacity)?;
            Ok((
                layout,
                extent,
                origin,
                pose,
                plane_cap,
                capacity,
                header_bytes,
                u32_at(&fixed, 16),
                u64_at(&fixed, 208),
            ))
        })();
        match decode {
            Ok((
                layout,
                extent,
                origin,
                pose,
                plane_cap,
                capacity,
                header_bytes,
                t,
                open_generation,
            )) => Ok(Self {
                layout,
                extent,
                origin,
                pose,
                plane_cap,
                capacity,
                header_bytes,
                t,
                open_generation,
                turn,
                params,
            }),
            Err(e) => Err(Box::new((turn, e))),
        }
    }
    /// This position's checked settings, captured from its accepted arm.
    pub fn params(&self) -> &Params {
        &self.params
    }
    pub fn t(&self) -> u32 {
        self.t
    }
    pub fn open_generation(&self) -> u64 {
        self.open_generation
    }
    pub fn iteration(&self) -> u32 {
        self.turn.word as u32
    }
    pub fn capacity(&self) -> u64 {
        self.capacity
    }
    pub fn plane_cap(&self) -> u32 {
        self.plane_cap
    }
    pub fn read_bytes(&self, offset: u64, len: u64) -> Result<Vec<u8>> {
        self.check_range(offset, len)?;
        Ok(self.turn.read(self.header_bytes + offset, len)?)
    }
    pub fn write_bytes(&mut self, offset: u64, bytes: &[u8]) -> Result<()> {
        self.check_range(offset, bytes.len() as u64)?;
        Ok(self.turn.write(self.header_bytes + offset, bytes)?)
    }
    fn check_range(&self, offset: u64, len: u64) -> Result<()> {
        if offset.checked_add(len).is_none_or(|n| n > self.capacity) {
            return Err(Error::protocol("stage data access exceeds capacity"));
        }
        Ok(())
    }
    fn voxel_offset(&self, channel: u16, index: [u32; 3]) -> Result<u64> {
        let plane = self
            .layout
            .channels
            .iter()
            .position(|&c| c == channel)
            .ok_or_else(|| Error::protocol("stage channel absent"))?;
        let mut n = plane as u64;
        for (i, size) in index.into_iter().zip(self.layout.allocation) {
            if i >= size {
                return Err(Error::protocol("stage voxel outside allocation"));
            }
            n = n
                .checked_mul(size as u64)
                .and_then(|n| n.checked_add(i as u64))
                .ok_or_else(|| Error::protocol("stage offset overflow"))?;
        }
        n.checked_mul(dtype_bytes(self.layout.dtype as i32)?)
            .ok_or_else(|| Error::protocol("stage offset overflow"))
    }
    pub fn read_voxel(&self, channel: u16, index: [u32; 3]) -> Result<Voxel> {
        let b = self.read_bytes(
            self.voxel_offset(channel, index)?,
            dtype_bytes(self.layout.dtype as i32)?,
        )?;
        Ok(match self.layout.dtype {
            pb::DType::DtypeU8 => Voxel::U8(b[0]),
            pb::DType::DtypeU16 => Voxel::U16(u16::from_le_bytes(b.try_into().unwrap())),
            pb::DType::DtypeF32 => Voxel::F32(f32::from_le_bytes(b.try_into().unwrap())),
            pb::DType::DtypeF64 => Voxel::F64(f64::from_le_bytes(b.try_into().unwrap())),
            _ => return Err(Error::protocol("unknown stage dtype")),
        })
    }
    pub fn write_voxel(&mut self, channel: u16, index: [u32; 3], value: Voxel) -> Result<()> {
        let bytes = match (self.layout.dtype, value) {
            (pb::DType::DtypeU8, Voxel::U8(v)) => vec![v],
            (pb::DType::DtypeU16, Voxel::U16(v)) => v.to_le_bytes().to_vec(),
            (pb::DType::DtypeF32, Voxel::F32(v)) => v.to_le_bytes().to_vec(),
            (pb::DType::DtypeF64, Voxel::F64(v)) => v.to_le_bytes().to_vec(),
            _ => {
                return Err(Error::protocol(
                    "stage voxel dtype differs from current layout",
                ))
            }
        };
        self.write_bytes(self.voxel_offset(channel, index)?, &bytes)
    }
    pub(crate) fn publish(&self) -> Result<()> {
        validate_header(
            &self.layout,
            &self.extent,
            &self.origin,
            &self.pose,
            self.plane_cap,
            self.capacity,
        )?;
        self.turn
            .write(28, &(self.layout.dtype as u32).to_le_bytes())?;
        self.turn
            .write(32, &(self.layout.channels.len() as u32).to_le_bytes())?;
        for (i, n) in self.layout.allocation.iter().enumerate() {
            self.turn.write(40 + 4 * i as u64, &n.to_le_bytes())?;
        }
        for (i, pair) in self.extent.iter().enumerate() {
            for (j, n) in pair.iter().enumerate() {
                self.turn
                    .write(64 + 8 * i as u64 + 4 * j as u64, &n.to_le_bytes())?;
            }
        }
        for (base, values) in [
            (88, self.origin.as_slice()),
            (112, self.pose.rotation.as_slice()),
            (184, self.pose.translation.as_slice()),
        ] {
            for (i, v) in values.iter().enumerate() {
                self.turn.write(base + 8 * i as u64, &v.to_le_bytes())?;
            }
        }
        let channels: Vec<_> = self
            .layout
            .channels
            .iter()
            .flat_map(|v| v.to_le_bytes())
            .collect();
        self.turn.write(256, &channels)?;
        Ok(())
    }
    pub(crate) fn finish(&mut self, outcome: u32, reason: u32) -> Result<()> {
        Ok(self.turn.finish(outcome, reason)?)
    }
}
fn validate_header(
    layout: &Layout,
    extent: &[[u32; 2]; 3],
    origin: &[f64; 3],
    pose: &Pose,
    plane_cap: u32,
    capacity: u64,
) -> Result<()> {
    if layout.channels.len() > plane_cap as usize
        || layout.channels.iter().collect::<BTreeSet<_>>().len() != layout.channels.len()
        || layout
            .plane_bytes()?
            .checked_mul(layout.channels.len() as u64)
            .is_none_or(|n| n > capacity)
        || extent
            .iter()
            .zip(layout.allocation)
            .any(|(v, n)| v[0] > v[1] || v[1] > n)
        || origin
            .iter()
            .chain(&pose.rotation)
            .chain(&pose.translation)
            .any(|v| !v.is_finite())
    {
        return Err(Error::protocol(
            "invalid stage header layout, extent or pose",
        ));
    }
    Ok(())
}

pub(crate) fn validate_shape(shape: &pb::StageShape) -> Result<()> {
    dtype_bytes(shape.dtype).map_err(|e| Error::protocol(e.detail))?;
    if shape.channels.len() > 65536
        || shape.channels.iter().any(|&c| c > 65535)
        || shape.channels.iter().collect::<BTreeSet<_>>().len() != shape.channels.len()
    {
        return Err(Error::protocol("invalid stage channel indices"));
    }
    Ok(())
}
pub(crate) fn validate_plan(input: &pb::StageShape, plan: &pb::StagePlanned) -> Result<()> {
    validate_shape(input)?;
    let output = plan
        .output
        .as_ref()
        .ok_or_else(|| Error::internal("stage output missing"))?;
    dtype_bytes(output.dtype).map_err(|e| Error::internal(e.detail))?;
    // §4 explicitly makes any out-of-range StagePlanned channel a protocol
    // error. Other invalid author plans are internal errors, not host faults.
    if output
        .channels
        .iter()
        .chain(
            plan.modifies
                .iter()
                .flat_map(|m| std::iter::once(&m.channel).chain(&m.needs)),
        )
        .chain(plan.adds.iter().flat_map(|a| &a.needs))
        .chain(&plan.reads)
        .any(|&c| c > 65535)
    {
        return Err(Error::protocol("stage plan names out-of-range channel"));
    }
    if output.z == 0
        || output.y == 0
        || output.x == 0
        || output.z > input.z
        || output.y > input.y
        || output.x > input.x
    {
        return Err(Error::internal(
            "stage output must have positive dimensions no larger than input",
        ));
    }
    let input_channels: BTreeSet<_> = input.channels.iter().copied().collect();
    if plan.reads.iter().any(|c| !input_channels.contains(c)) {
        return Err(Error::internal("stage reads a channel absent from input"));
    }
    let mut modified = BTreeSet::new();
    let mut keys = BTreeSet::new();
    for m in &plan.modifies {
        if !input_channels.contains(&m.channel)
            || !modified.insert(m.channel)
            || m.needs.iter().any(|c| !input_channels.contains(c))
        {
            return Err(Error::internal("invalid stage modifies or needs"));
        }
    }
    for a in &plan.adds {
        if a.key.trim().is_empty()
            || !keys.insert(&a.key)
            || a.needs.iter().any(|c| !input_channels.contains(c))
        {
            return Err(Error::internal("invalid stage added channel key or needs"));
        }
    }
    if input
        .channels
        .len()
        .checked_add(plan.adds.len())
        .is_none_or(|n| n > 65536)
    {
        return Err(Error::internal("stage exceeds 65536 channels"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn pose_composition_uses_old_rotation_for_translation() {
        let mut pose = Pose {
            rotation: [0., -1., 0., 1., 0., 0., 0., 0., 1.],
            translation: [10., 20., 30.],
        };
        let motion = Pose {
            rotation: [1., 0., 0., 0., 0., -1., 0., 1., 0.],
            translation: [1., 2., 3.],
        };
        pose.compose(&motion);
        assert_eq!(pose.rotation, [0., 0., 1., 1., 0., 0., 0., 1., 0.]);
        assert_eq!(pose.translation, [8., 21., 33.]);
    }
    #[test]
    fn plans_cannot_grow_or_name_invalid_dependencies_and_keys() {
        let input = pb::StageShape {
            channels: vec![7],
            z: 8,
            y: 9,
            x: 10,
            dtype: 1,
        };
        let mut p = pb::StagePlanned {
            output: Some(input.clone()),
            ..Default::default()
        };
        assert!(validate_plan(&input, &p).is_ok()); // Pose-only stages are legal.
        p.output.as_mut().unwrap().x = 11;
        assert!(validate_plan(&input, &p).is_err());
        p.output.as_mut().unwrap().x = 10;
        p.output.as_mut().unwrap().channels = vec![1, 1]; // Ignored on output.
        assert!(validate_plan(&input, &p).is_ok());
        p.modifies.push(pb::StageChannelNeed {
            channel: 7,
            needs: vec![65536],
        });
        assert!(validate_plan(&input, &p).is_err());
        p.modifies.clear();
        p.adds = vec![
            pb::StageNewChannel {
                key: "same".into(),
                name: String::new(),
                needs: vec![7]
            };
            2
        ];
        assert!(validate_plan(&input, &p).is_err());
    }
}
