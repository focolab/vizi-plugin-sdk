use std::fmt;

pub const RECORD_LEN: usize = 112;
pub const PROTOCOL_VERSION: (u32, u32) = (1, 0);
pub type RecordBytes = [u8; RECORD_LEN];

/// CRC-32/ISO-HDLC, including initial and final XORs (the zlib convention).
pub fn crc32(bytes: &[u8]) -> u32 {
    crc32fast::hash(bytes)
}

pub(crate) fn u32_at(b: &[u8], i: usize) -> u32 {
    u32::from_le_bytes(b[i..i + 4].try_into().unwrap())
}
pub(crate) fn u64_at(b: &[u8], i: usize) -> u64 {
    u64::from_le_bytes(b[i..i + 8].try_into().unwrap())
}

fn frame(op: u8, id: u32, payload: [u8; 100]) -> RecordBytes {
    let mut b = [0; RECORD_LEN];
    b[0] = op;
    b[1] = 2;
    b[4..8].copy_from_slice(&id.to_le_bytes());
    b[8..108].copy_from_slice(&payload);
    let crc = crc32(&b[..108]);
    b[108..].copy_from_slice(&crc.to_le_bytes());
    b
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct EncodeError(pub &'static str);
impl fmt::Display for EncodeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.0)
    }
}
impl std::error::Error for EncodeError {}

/// A 512-bit, LSB-first lower-bound hint. Clear bits never mean unchanged.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RegionMask([u8; 64]);
impl Default for RegionMask {
    fn default() -> Self {
        Self([0; 64])
    }
}
impl RegionMask {
    pub fn from_bytes(bytes: [u8; 64]) -> Self {
        Self(bytes)
    }
    pub fn as_bytes(&self) -> &[u8; 64] {
        &self.0
    }
    pub fn set(&mut self, region: usize) -> Result<(), EncodeError> {
        if region >= 512 {
            return Err(EncodeError("region index exceeds the 512-bit mask"));
        }
        self.0[region / 8] |= 1 << (region % 8);
        Ok(())
    }
    pub fn indices(&self) -> impl Iterator<Item = usize> + '_ {
        (0..512).filter(|r| self.0[r / 8] & (1 << (r % 8)) != 0)
    }
    pub fn is_empty(&self) -> bool {
        self.0.iter().all(|b| *b == 0)
    }
    pub fn union(&mut self, other: &Self) {
        for (a, b) in self.0.iter_mut().zip(other.0) {
            *a |= b;
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PaneEpoch {
    pub view_id: u8,
    pub epoch: u32,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Notification {
    pub segment_id: u64,
    pub mask: RegionMask,
    /// Pane zero is valid; absence is represented by None, never view_id == 0.
    pub pane: Option<PaneEpoch>,
}
impl Notification {
    pub fn encode(&self, id: u32) -> Result<RecordBytes, EncodeError> {
        if self.segment_id == 0 {
            return Err(EncodeError("segment_id must be nonzero"));
        }
        let mut p = [0; 100];
        p[..8].copy_from_slice(&self.segment_id.to_le_bytes());
        p[8..72].copy_from_slice(self.mask.as_bytes());
        if let Some(pane) = self.pane {
            if pane.epoch == 0 {
                return Err(EncodeError("a present pane epoch must be nonzero"));
            }
            p[72..76].copy_from_slice(&pane.epoch.to_le_bytes());
            p[76] = pane.view_id;
        }
        Ok(frame(b'D', id, p))
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Permissions(u8);
impl Permissions {
    pub const PLUGIN_READ: u8 = 1;
    pub const PLUGIN_WRITE: u8 = 2;
    pub const HOST_READ: u8 = 4;
    pub const HOST_WRITE: u8 = 8;
    pub fn new(bits: u8) -> Result<Self, EncodeError> {
        if bits & 1 == 0 || bits & 0xf0 != 0 {
            Err(EncodeError(
                "grant permissions require PLUGIN_READ and no reserved bits",
            ))
        } else {
            Ok(Self(bits))
        }
    }
    pub fn bits(self) -> u8 {
        self.0
    }
    pub fn plugin_writable(self) -> bool {
        self.0 & Self::PLUGIN_WRITE != 0
    }
}

/// A capability declaration, not a mapped or installed segment.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MemoryGrant {
    pub segment_id: u64,
    pub size: u64,
    pub permissions: Permissions,
}
impl MemoryGrant {
    /// Encode a Windows grant. The name's namespace/spelling is the host's
    /// policy; only its bounded UTF-8/NUL representation is checked here.
    pub fn encode_named(&self, id: u32, name: &str) -> Result<RecordBytes, EncodeError> {
        if name.is_empty() || name.len() > 80 || name.as_bytes().contains(&0) {
            return Err(EncodeError(
                "a section name must contain 1..=80 UTF-8 bytes and no NUL",
            ));
        }
        let bytes = self.encode(id)?;
        let mut payload: [u8; 100] = bytes[8..108].try_into().unwrap();
        payload[18] = name.len() as u8;
        payload[19..19 + name.len()].copy_from_slice(name.as_bytes());
        Ok(frame(b'M', id, payload))
    }
    /// Encode a Unix grant for fixture generators/host test peers. The plugin
    /// transport intentionally provides no M send operation.
    pub fn encode(&self, id: u32) -> Result<RecordBytes, EncodeError> {
        if self.segment_id == 0 || self.size < 32 {
            return Err(EncodeError(
                "a grant requires a segment id and at least its 32-byte preamble",
            ));
        }
        let mut p = [0; 100];
        p[..8].copy_from_slice(&self.segment_id.to_le_bytes());
        p[8..16].copy_from_slice(&self.size.to_le_bytes());
        p[16] = self.permissions.bits();
        Ok(frame(b'M', id, p))
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum Phase {
    Press = 0,
    Drag = 1,
    Release = 2,
    Cancel = 3,
    Fire = 4,
}
impl Phase {
    pub fn is_opening(self) -> bool {
        matches!(self, Self::Press | Self::Fire)
    }
    pub fn is_terminal(self) -> bool {
        matches!(self, Self::Release | Self::Cancel)
    }
    pub(crate) fn from_byte(b: u8) -> Option<Self> {
        Some(match b {
            0 => Self::Press,
            1 => Self::Drag,
            2 => Self::Release,
            3 => Self::Cancel,
            4 => Self::Fire,
            _ => return None,
        })
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum TargetKind {
    Annotation = 1,
    Roi = 2,
    Handle = 3,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Target {
    pub kind: TargetKind,
    pub id: u32,
    pub part: u32,
}

/// Coordinates are z,y,x in the event's level, not normalized to level zero.
#[derive(Clone, Debug, PartialEq)]
pub struct Ray {
    pub camera: [f32; 3],
    pub far: [f32; 3],
    pub entry_frac: f32,
    /// Physical-space radians about far-camera; reference axis is in the event.
    pub roll: f32,
}
#[derive(Clone, Debug, PartialEq)]
pub enum Position {
    None,
    Voxel([f32; 3]),
    Picked([f32; 3]),
    ColumnYx {
        y: f32,
        x: f32,
        z_min: f32,
        z_max: f32,
    },
    ColumnZx {
        z: f32,
        x: f32,
        y_min: f32,
        y_max: f32,
    },
    ColumnZy {
        z: f32,
        y: f32,
        x_min: f32,
        x_max: f32,
    },
    Ray(Ray),
    RayAndPicked {
        ray: Ray,
        picked: [f32; 3],
        /// How far the picked structure runs behind its front, as a fraction
        /// of the ray left there, in [0, 1]: the back is
        /// `front + back_frac * (far - front)`. 0.0 is a pick of no depth,
        /// and what a sender that predates the field leaves. NaN says
        /// nothing stood out: a point pick, which has no back point.
        back_frac: f32,
    },
}

impl Position {
    /// Whether this is a `RayAndPicked` point pick (NaN `back_frac`): nothing
    /// stood out along the ray, and `picked` is a likely voxel with no
    /// structure around it. A zero-depth pick (0.0) is not a point.
    /// Returns false for every other position kind.
    pub fn is_point(&self) -> bool {
        matches!(self, Self::RayAndPicked { back_frac, .. } if back_frac.is_nan())
    }

    /// Where a `RayAndPicked`'s picked voxel centre projects onto
    /// camera -> far, as a fraction of it clamped to [0, 1]; 0 when the
    /// camera and far coincide. Returns None for every other position kind.
    pub fn front_frac(&self) -> Option<f64> {
        match self {
            Self::RayAndPicked { ray, picked, .. } => {
                // Widen before subtracting to avoid f32 overflow.
                let d: [f64; 3] =
                    std::array::from_fn(|i| f64::from(ray.far[i]) - f64::from(ray.camera[i]));
                let dd: f64 = d.iter().map(|x| x * x).sum();
                if dd <= 0.0 {
                    return Some(0.0);
                }
                let t = (0..3)
                    .map(|i| (f64::from(picked[i]) - f64::from(ray.camera[i])) * d[i])
                    .sum::<f64>()
                    / dd;
                Some(t.clamp(0.0, 1.0))
            }
            _ => None,
        }
    }

    /// The picked front on the ray: `picked`'s centre projected onto it, in
    /// the event's level and ZYX order. Returns None for every other kind.
    pub fn front(&self) -> Option<[f32; 3]> {
        self.front_frac().and_then(|t| self.on_ray(t))
    }

    /// Back of a `RayAndPicked` structure, in the event's level and ZYX order:
    /// `front + back_frac * (far - front)`. The front itself for a structure
    /// of no depth. Returns None for a point pick or any other position kind.
    pub fn back(&self) -> Option<[f32; 3]> {
        match self {
            Self::RayAndPicked { back_frac, .. } if !back_frac.is_nan() => {
                let t = self.front_frac()?;
                self.on_ray(t + f64::from(*back_frac) * (1.0 - t))
            }
            _ => None,
        }
    }

    fn on_ray(&self, t: f64) -> Option<[f32; 3]> {
        let (Self::Ray(ray) | Self::RayAndPicked { ray, .. }) = self else {
            return None;
        };
        Some(std::array::from_fn(|i| {
            // Widen the intermediate difference to avoid f32 overflow.
            let camera = f64::from(ray.camera[i]);
            (camera + t * (f64::from(ray.far[i]) - camera)) as f32
        }))
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct InputEvent {
    pub gen: u32,
    pub t: u32,
    pub interaction: u32,
    pub seq: u32,
    pub epoch: u32,
    pub target: Option<Target>,
    pub tool: u32,
    pub channels: u16,
    pub phase: Phase,
    pub view_id: u8,
    pub level: u8,
    /// SHIFT=1, CTRL=2, ALT=4, META=8. Reserved bits are removed on decode.
    pub mods: u8,
    pub click_count: u8,
    pub miss: bool,
    pub roll_ref_y: bool,
    /// One-based tool action on FIRE; zero names the primary gesture.
    pub action: u16,
    pub position: Position,
}
impl InputEvent {
    /// The 3D ray this record carries, whether or not a pick came with it
    /// (`RAY` or `RAY_AND_PICKED`); None for every other kind. A tool reads
    /// the ray here and does not branch on which of the two arrived.
    pub fn ray(&self) -> Option<&Ray> {
        match &self.position {
            Position::Ray(ray) | Position::RayAndPicked { ray, .. } => Some(ray),
            _ => None,
        }
    }

    /// The host's pick along the ray, when one came: the `RayAndPicked`
    /// position. None on a bare `RAY` (the tool declared `wants_pick` false,
    /// or the host found nothing to send) and on every non-ray kind.
    pub fn pick(&self) -> Option<&Position> {
        matches!(self.position, Position::RayAndPicked { .. }).then_some(&self.position)
    }

    /// Codec for independent fixtures. No plugin-side E send operation exists.
    pub fn encode(&self, id: u32) -> Result<RecordBytes, EncodeError> {
        if self.mods & 0xf0 != 0 {
            return Err(EncodeError("reserved modifier bits"));
        }
        let mut p = [0; 100];
        for (i, n) in [
            self.gen,
            self.t,
            self.interaction,
            self.seq,
            self.epoch,
            self.target.map_or(0, |t| t.id),
            self.target.map_or(0, |t| t.part),
            self.tool,
        ]
        .into_iter()
        .enumerate()
        {
            p[i * 4..i * 4 + 4].copy_from_slice(&n.to_le_bytes());
        }
        p[32..34].copy_from_slice(&self.channels.to_le_bytes());
        p[34] = self.phase as u8;
        p[35] = self.view_id;
        p[36] = self.level;
        p[37] = self.mods;
        p[38] = self.click_count;
        p[39] = self.target.map_or(0, |t| t.kind as u8);
        p[41] = u8::from(self.miss) | (u8::from(self.roll_ref_y) << 1);
        p[42..44].copy_from_slice(&self.action.to_le_bytes());
        let mut values = Vec::new();
        let ray_values = |r: &Ray| {
            [
                r.camera[0],
                r.camera[1],
                r.camera[2],
                r.far[0],
                r.far[1],
                r.far[2],
                r.entry_frac,
                r.roll,
            ]
        };
        p[40] = match &self.position {
            Position::None => 0,
            Position::Voxel(v) => {
                values.extend(v);
                1
            }
            Position::Picked(v) => {
                values.extend(v);
                2
            }
            Position::ColumnYx { y, x, z_min, z_max } => {
                values.extend([y, x, z_min, z_max]);
                3
            }
            Position::ColumnZx { z, x, y_min, y_max } => {
                values.extend([z, x, y_min, y_max]);
                4
            }
            Position::ColumnZy { z, y, x_min, x_max } => {
                values.extend([z, y, x_min, x_max]);
                5
            }
            Position::Ray(r) => {
                values.extend(ray_values(r));
                6
            }
            Position::RayAndPicked {
                ray,
                picked,
                back_frac,
            } => {
                values.extend(ray_values(ray));
                values.extend(picked);
                values.push(*back_frac);
                7
            }
        };
        for (i, v) in values.iter().enumerate() {
            p[44 + i * 4..48 + i * 4].copy_from_slice(&v.to_le_bytes());
        }
        let raw = RawInput::new(&p);
        if raw.zero_id()
            || (self.phase.is_opening() && self.seq != 0)
            || raw.local_violation().is_some()
        {
            return Err(EncodeError("input violates record-local rules"));
        }
        Ok(frame(b'E', id, p))
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Violation {
    ZeroId,
    SeqOnOpen,
    PositionTail,
    NonfinitePosition,
    MissWithPicked,
    InvalidTarget,
    InteractionMismatch,
    UnknownTool,
    EpochMismatch,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum GrantViolation {
    SizeBelowPreamble,
    MissingPluginRead,
    ReservedPermissions,
    EmptyName,
    NameTooLong,
    NameTerminator,
    EmbeddedNul,
    InvalidUtf8,
    NamePadding,
}

#[cfg(any(windows, test))]
pub(crate) fn grant_name(payload: &[u8]) -> Result<String, GrantViolation> {
    let len = payload[18] as usize;
    // Validate the length before computing any slice controlled by it.
    if len > 80 {
        return Err(GrantViolation::NameTooLong);
    }
    if len == 0 {
        return Err(GrantViolation::EmptyName);
    }
    if payload[19 + len] != 0 {
        return Err(GrantViolation::NameTerminator);
    }
    let bytes = &payload[19..19 + len];
    if bytes.contains(&0) {
        return Err(GrantViolation::EmbeddedNul);
    }
    let name = std::str::from_utf8(bytes).map_err(|_| GrantViolation::InvalidUtf8)?;
    if payload[20 + len..].iter().any(|b| *b != 0) {
        return Err(GrantViolation::NamePadding);
    }
    Ok(name.to_owned())
}

#[cfg(test)]
mod name_tests {
    use super::*;
    #[test]
    fn named_grants_validate_representation_not_a_naming_pattern() {
        let grant = MemoryGrant {
            segment_id: 1,
            size: 32,
            permissions: Permissions::new(1).unwrap(),
        };
        for name in [
            "Local\\vizi-example",
            "another-spelling",
            "é",
            &"x".repeat(80),
        ] {
            let b = grant.encode_named(1, name).unwrap();
            assert_eq!(b[26] as usize, name.len());
            assert_eq!(grant_name(&b[8..108]).unwrap(), name);
        }
        for name in ["", "a\0b", &"x".repeat(81), &"é".repeat(41)] {
            assert!(grant.encode_named(1, name).is_err());
        }
    }
    #[test]
    fn every_invalid_name_disposition_is_record_local() {
        let grant = MemoryGrant {
            segment_id: 1,
            size: 32,
            permissions: Permissions::new(1).unwrap(),
        };
        let b = grant.encode_named(1, "abc").unwrap();
        for (offset, value, expected) in [
            (18, 0, GrantViolation::EmptyName),
            (18, 81, GrantViolation::NameTooLong),
            (18, 255, GrantViolation::NameTooLong),
            (22, 1, GrantViolation::NameTerminator),
            (20, 0, GrantViolation::EmbeddedNul),
            (19, 255, GrantViolation::InvalidUtf8),
            (99, 1, GrantViolation::NamePadding),
        ] {
            let mut p = b[8..108].to_vec();
            p[offset] = value;
            assert_eq!(grant_name(&p), Err(expected));
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Rejection {
    Contract(Violation),
    /// Supervisor reports grant acknowledgement failure class `malformed`.
    Grant {
        segment_id: u64,
        violations: Vec<GrantViolation>,
    },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum DiagnosticKind {
    IdGap,
    IdRegression,
    UnknownOpcode,
    UnixNameIgnored,
    EmptyMask,
    ViewWithoutEpoch,
    UnknownPhase,
    DuplicateOpening,
    UnknownPosition,
    UnknownTarget,
    ReservedMods,
    ReservedPositionFlags,
    MissWithoutPosition,
    StaleEpoch,
    RetiredTool,
    UnknownInteraction,
    DuplicateSequence,
    SequenceGap,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Diagnostic {
    pub kind: DiagnosticKind,
    /// Count of missing IDs/samples for gaps; raw ignored value otherwise.
    pub value: u64,
    pub expected: Option<u32>,
    pub observed: Option<u32>,
}
impl Diagnostic {
    pub(crate) fn notice(kind: DiagnosticKind, value: impl Into<u64>) -> Self {
        Self {
            kind,
            value: value.into(),
            expected: None,
            observed: None,
        }
    }
    pub(crate) fn sequence(kind: DiagnosticKind, expected: u32, observed: u32, value: u32) -> Self {
        Self {
            kind,
            value: value as u64,
            expected: Some(expected),
            observed: Some(observed),
        }
    }
}

pub(crate) struct RawInput {
    pub gen: u32,
    pub t: u32,
    pub interaction: u32,
    pub seq: u32,
    pub epoch: u32,
    pub target_id: u32,
    pub target_part: u32,
    pub tool: u32,
    pub channels: u16,
    pub phase: u8,
    pub view_id: u8,
    pub level: u8,
    pub mods: u8,
    pub click_count: u8,
    pub target_kind: u8,
    pub kind: u8,
    pub flags: u8,
    pub action: u16,
    position: [u8; 48],
}
impl RawInput {
    pub fn new(p: &[u8]) -> Self {
        Self {
            gen: u32_at(p, 0),
            t: u32_at(p, 4),
            interaction: u32_at(p, 8),
            seq: u32_at(p, 12),
            epoch: u32_at(p, 16),
            target_id: u32_at(p, 20),
            target_part: u32_at(p, 24),
            tool: u32_at(p, 28),
            channels: u16::from_le_bytes(p[32..34].try_into().unwrap()),
            phase: p[34],
            view_id: p[35],
            level: p[36],
            mods: p[37],
            click_count: p[38],
            target_kind: p[39],
            kind: p[40],
            flags: p[41],
            action: u16::from_le_bytes(p[42..44].try_into().unwrap()),
            position: p[44..92].try_into().unwrap(),
        }
    }
    pub fn zero_id(&self) -> bool {
        self.gen == 0 || self.interaction == 0 || self.tool == 0 || self.epoch == 0
    }
    fn values(&self) -> [f32; 12] {
        std::array::from_fn(|i| f32::from_bits(u32_at(&self.position, i * 4)))
    }
    pub fn local_violation(&self) -> Option<Violation> {
        let len = match self.kind {
            0 => Some(0),
            1 | 2 => Some(12),
            3..=5 => Some(16),
            6 => Some(32),
            7 => Some(48),
            _ => None,
        };
        if let Some(len) = len {
            if self.position[len..].iter().any(|b| *b != 0) {
                return Some(Violation::PositionTail);
            }
            let v = self.values();
            // Only back_frac (the twelfth float) admits NaN: nothing stood out.
            if v[..(len / 4).min(11)].iter().any(|v| !v.is_finite())
                || (matches!(self.kind, 6 | 7)
                    && (!(0.0..=1.0).contains(&v[6]) || v[..3] == v[3..6]))
                || (self.kind == 7 && !v[11].is_nan() && !(0.0..=1.0).contains(&v[11]))
            {
                return Some(Violation::NonfinitePosition);
            }
            if matches!(self.kind, 2 | 7) && self.flags & 1 != 0 {
                return Some(Violation::MissWithPicked);
            }
        }
        if self.target_kind <= 3
            && ((self.target_kind == 0) != (self.target_id == 0)
                || (self.target_kind == 0 && self.target_part != 0))
        {
            return Some(Violation::InvalidTarget);
        }
        None
    }
    pub fn decode(&self, phase: Phase, diagnostics: &mut Vec<Diagnostic>) -> InputEvent {
        use DiagnosticKind::*;
        for (condition, kind, value) in [
            (self.kind > 7, UnknownPosition, self.kind),
            (self.target_kind > 3, UnknownTarget, self.target_kind),
            (self.mods & 0xf0 != 0, ReservedMods, self.mods & 0xf0),
            (
                self.flags & 0xfc != 0,
                ReservedPositionFlags,
                self.flags & 0xfc,
            ),
            (
                self.kind == 0 && self.flags & 1 != 0,
                MissWithoutPosition,
                1,
            ),
        ] {
            if condition {
                diagnostics.push(Diagnostic::notice(kind, value));
            }
        }
        let v = self.values();
        let ray = || Ray {
            camera: v[..3].try_into().unwrap(),
            far: v[3..6].try_into().unwrap(),
            entry_frac: v[6],
            roll: v[7],
        };
        let position = match self.kind {
            1 => Position::Voxel(v[..3].try_into().unwrap()),
            2 => Position::Picked(v[..3].try_into().unwrap()),
            3 => Position::ColumnYx {
                y: v[0],
                x: v[1],
                z_min: v[2],
                z_max: v[3],
            },
            4 => Position::ColumnZx {
                z: v[0],
                x: v[1],
                y_min: v[2],
                y_max: v[3],
            },
            5 => Position::ColumnZy {
                z: v[0],
                y: v[1],
                x_min: v[2],
                x_max: v[3],
            },
            6 => Position::Ray(ray()),
            7 => Position::RayAndPicked {
                ray: ray(),
                picked: v[8..11].try_into().unwrap(),
                back_frac: v[11],
            },
            _ => Position::None,
        };
        let target = match self.target_kind {
            1 => Some(TargetKind::Annotation),
            2 => Some(TargetKind::Roi),
            3 => Some(TargetKind::Handle),
            _ => None,
        }
        .map(|kind| Target {
            kind,
            id: self.target_id,
            part: self.target_part,
        });
        InputEvent {
            gen: self.gen,
            t: self.t,
            interaction: self.interaction,
            seq: self.seq,
            epoch: self.epoch,
            target,
            tool: self.tool,
            channels: self.channels,
            phase,
            view_id: self.view_id,
            level: self.level,
            mods: self.mods & 15,
            click_count: self.click_count,
            miss: matches!(self.kind, 1..=7) && self.flags & 1 != 0,
            roll_ref_y: self.flags & 2 != 0,
            action: self.action,
            position,
        }
    }
}
