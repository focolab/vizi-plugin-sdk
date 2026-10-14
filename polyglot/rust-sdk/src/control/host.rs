//! Typed conveniences over the message-level host client. Calls block the
//! calling worker, never the dispatcher; do not call them in input callbacks.
use super::*;

/// Private, aligned Rust values copied from a host deposit. Copying releases
/// the deposit; dropping a refused reply releases it as well.
#[derive(Clone, Debug, PartialEq)]
pub enum VoxelData {
    U8(Vec<u8>),
    U16(Vec<u16>),
    F32(Vec<f32>),
}
impl VoxelData {
    pub fn into_f32(self) -> Vec<f32> {
        match self {
            Self::U8(v) => v.into_iter().map(f32::from).collect(),
            Self::U16(v) => v.into_iter().map(f32::from).collect(),
            Self::F32(v) => v,
        }
    }
}
#[derive(Clone, Debug)]
pub struct HostVolume {
    pub data: VoxelData,
    pub shape_zyx: [u32; 3],
    pub t: u32,
    pub c: u32,
    pub level: u32,
    pub open_generation: u64,
    /// The filter that produced this reply's voxels. None means the file's own;
    /// never inferred from ViewState or retained from another read.
    pub filter: Option<pb::AppliedFilter>,
}
/// The host's level-0 inclusion calculation for one region at one frame.
/// `volume` is absent for both a hidden region and a present region covering
/// zero dataset voxels; `present` distinguishes those successful outcomes.
#[derive(Clone, Debug, PartialEq)]
pub struct HostRoiInclusion {
    pub region_id: String,
    pub t: i32,
    pub open_generation: u64,
    pub present: bool,
    pub voxel_count: u64,
    pub volume: Option<InclusionVolume>,
}
/// Owned bbox-local U8 membership (0/1), placed at a level-0 dataset origin.
/// Copying disposes of this reply's deposit use; no borrowed bytes remain.
#[derive(Clone, Debug, PartialEq)]
pub struct InclusionVolume {
    pub data: Vec<u8>,
    pub shape_zyx: [u32; 3],
    pub origin_zyx: [u32; 3],
}
fn decode(bytes: Vec<u8>, dtype: i32) -> Result<VoxelData> {
    match pb::DType::try_from(dtype) {
        Ok(pb::DType::DtypeU8) => Ok(VoxelData::U8(bytes)),
        Ok(pb::DType::DtypeU16) if bytes.len().is_multiple_of(2) => Ok(VoxelData::U16(
            bytes
                .as_chunks::<2>()
                .0
                .iter()
                .map(|v| u16::from_le_bytes(*v))
                .collect(),
        )),
        Ok(pb::DType::DtypeF32) if bytes.len().is_multiple_of(4) => Ok(VoxelData::F32(
            bytes
                .as_chunks::<4>()
                .0
                .iter()
                .map(|v| f32::from_le_bytes(*v))
                .collect(),
        )),
        _ => Err(Error::protocol(
            "intensity payload must contain whole u8/u16/f32 values",
        )),
    }
}
impl Context {
    /// Get the current view. `ViewState.c` is deprecated and always zero;
    /// use `visible_channels` for the channels enabled for display.
    pub fn view_state(&self) -> Result<pb::ViewState> {
        self.view_state_timeout(Duration::from_secs(30))
    }
    /// Like `view_state`, with a caller-selected timeout. The deprecated `c`
    /// field is always zero; display channels are in `visible_channels`.
    pub fn view_state_timeout(&self, timeout: Duration) -> Result<pb::ViewState> {
        let reply = self.request_timeout(Plugin::GetViewState(pb::GetViewState {}), timeout)?;
        match &reply.message {
            Host::ViewState(v) => Ok(v.clone()),
            _ => Err(Error::protocol("expected ViewState")),
        }
    }
    /// Read a whole volume, or a half-open ROI in the requested level's grid.
    /// Echoed coordinates and ROI shape are checked before returning values.
    /// The host may serve shared filtered data; use `read_volume_raw` to request
    /// the file's own voxels regardless of the person's sharing setting.
    pub fn read_volume(
        &self,
        t: u32,
        c: u32,
        level: u32,
        roi: Option<([u32; 3], [u32; 3])>,
    ) -> Result<HostVolume> {
        self.read_volume_impl(t, c, level, roi, false)
    }
    /// Like `read_volume`, but explicitly request the file's own voxels.
    /// A raw reply carries no `filter`; `ViewState.filter` still names what the
    /// views show and is independent of this read's provenance.
    pub fn read_volume_raw(
        &self,
        t: u32,
        c: u32,
        level: u32,
        roi: Option<([u32; 3], [u32; 3])>,
    ) -> Result<HostVolume> {
        self.read_volume_impl(t, c, level, roi, true)
    }
    fn read_volume_impl(
        &self,
        t: u32,
        c: u32,
        level: u32,
        roi: Option<([u32; 3], [u32; 3])>,
        raw: bool,
    ) -> Result<HostVolume> {
        if roi.is_some_and(|(lo, hi)| (0..3).any(|a| lo[a] >= hi[a])) {
            return Err(Error::new(
                pb::error::Kind::OutOfBounds,
                "ROI must be nonempty and half-open",
            ));
        }
        let mut reply = self.request(Plugin::ReadDataVolume(pb::ReadDataVolume {
            t,
            c,
            level,
            roi_min_zyx: roi.map_or_else(Vec::new, |(lo, _)| lo.to_vec()),
            roi_max_zyx: roi.map_or_else(Vec::new, |(_, hi)| hi.to_vec()),
            raw,
        }))?;
        let Host::DataVolume(volume) = &reply.message else {
            return Err(Error::protocol("expected DataVolume"));
        };
        let descriptor = volume
            .segment
            .as_ref()
            .ok_or_else(|| Error::protocol("missing volume descriptor"))?;
        let shape_zyx: [u32; 3] = descriptor
            .shape_zyx
            .as_slice()
            .try_into()
            .map_err(|_| Error::protocol("volume shape must have three axes"))?;
        if (volume.t, volume.c, volume.level) != (t, c, level)
            || roi.is_some_and(|(lo, hi)| (0..3).any(|a| shape_zyx[a] != hi[a] - lo[a]))
        {
            return Err(Error::protocol(
                "returned volume coordinates or shape differ from request",
            ));
        }
        let dtype = descriptor.dtype;
        let open_generation = volume.open_generation;
        let filter = volume.filter.clone();
        let bytes = reply.copy_payload()?.bytes;
        Ok(HostVolume {
            data: decode(bytes, dtype)?,
            shape_zyx,
            t,
            c,
            level,
            open_generation,
            filter,
        })
    }
    pub fn get_rois(&self) -> Result<Vec<pb::RoiRecord>> {
        self.get_rois_window(None, None)
    }
    /// Requires data_access. Copy the host's inclusion raster for one region
    /// and frame, checking its echo, shape, membership values and voxel count.
    /// Hidden and present-but-empty regions succeed without a deposit.
    pub fn read_roi_inclusion(&self, region_id: &str, t: i32) -> Result<HostRoiInclusion> {
        self.read_roi_inclusion_clipped(region_id, t, None)
    }
    /// As read_roi_inclusion, optionally clipped to a nonempty, half-open
    /// level-0 ZYX box. The host checks dataset bounds and its voxel budget.
    pub fn read_roi_inclusion_clipped(
        &self,
        region_id: &str,
        t: i32,
        roi: Option<([u32; 3], [u32; 3])>,
    ) -> Result<HostRoiInclusion> {
        if roi.is_some_and(|(lo, hi)| (0..3).any(|a| lo[a] >= hi[a])) {
            return Err(Error::new(
                pb::error::Kind::OutOfBounds,
                "ROI must be nonempty and half-open",
            ));
        }
        let mut reply = self.request(Plugin::ReadRoiInclusion(pb::ReadRoiInclusion {
            region_id: region_id.into(),
            t,
            roi_min_zyx: roi.map_or_else(Vec::new, |(lo, _)| lo.to_vec()),
            roi_max_zyx: roi.map_or_else(Vec::new, |(_, hi)| hi.to_vec()),
        }))?;
        let Host::RoiInclusion(inclusion) = &reply.message else {
            return Err(Error::protocol("expected RoiInclusion"));
        };
        if inclusion.region_id != region_id || inclusion.t != t {
            return Err(Error::protocol(
                "returned ROI inclusion region or frame differs from request",
            ));
        }
        if (!inclusion.present && inclusion.voxel_count != 0)
            || inclusion.segment.is_some() != (inclusion.present && inclusion.voxel_count > 0)
        {
            return Err(Error::protocol(
                "ROI inclusion presence, count and descriptor disagree",
            ));
        }
        let mut result = HostRoiInclusion {
            region_id: inclusion.region_id.clone(),
            t,
            open_generation: inclusion.open_generation,
            present: inclusion.present,
            voxel_count: inclusion.voxel_count,
            volume: None,
        };
        if let Some(descriptor) = &inclusion.segment {
            if descriptor.dtype != pb::DType::DtypeU8 as i32 {
                return Err(Error::protocol("ROI inclusion must be U8 membership"));
            }
            let shape_zyx: [u32; 3] = descriptor
                .shape_zyx
                .as_slice()
                .try_into()
                .map_err(|_| Error::protocol("ROI inclusion shape must have three axes"))?;
            let origin_zyx: [u32; 3] = inclusion
                .origin_zyx
                .as_slice()
                .try_into()
                .map_err(|_| Error::protocol("ROI inclusion origin must have three axes"))?;
            if roi.is_some_and(|(lo, hi)| {
                (0..3).any(|a| {
                    origin_zyx[a] < lo[a]
                        || origin_zyx[a]
                            .checked_add(shape_zyx[a])
                            .is_none_or(|end| end > hi[a])
                })
            }) {
                return Err(Error::protocol(
                    "ROI inclusion lies outside the requested clip box",
                ));
            }
            let data = reply.copy_payload()?.bytes;
            let count = data.iter().try_fold(0_u64, |count, &value| match value {
                0 => Ok(count),
                1 => Ok(count + 1),
                _ => Err(Error::protocol("ROI inclusion membership must be 0 or 1")),
            })?;
            if count != result.voxel_count {
                return Err(Error::protocol(
                    "ROI inclusion voxel count differs from membership",
                ));
            }
            result.volume = Some(InclusionVolume {
                data,
                shape_zyx,
                origin_zyx,
            });
        }
        Ok(result)
    }
    /// Bounds are inclusive; either may be omitted. Without bounds the host
    /// returns the current posed frame. Windowed records retain the original
    /// pose-domain bounds, as do unwindowed records. Compare that pair with
    /// roi.t_start/t_end to detect a partial domain; presence alone is not a
    /// window test. An oversized reply is refused with OUT_OF_BOUNDS, not clipped.
    pub fn get_rois_window(
        &self,
        pose_t_start: Option<i32>,
        pose_t_end: Option<i32>,
    ) -> Result<Vec<pb::RoiRecord>> {
        self.get_rois_selected(&[], pose_t_start, pose_t_end)
    }
    /// Read just these regions, optionally with an inclusive pose window.
    /// Empty IDs means all regions; missing IDs are absent from the reply.
    /// Records preserve revision and content_hash for cache invalidation.
    pub fn get_rois_selected(
        &self,
        region_ids: &[String],
        pose_t_start: Option<i32>,
        pose_t_end: Option<i32>,
    ) -> Result<Vec<pb::RoiRecord>> {
        let reply = self.request(Plugin::GetRois(pb::GetRois {
            pose_t_start,
            pose_t_end,
            region_ids: region_ids.to_vec(),
        }))?;
        match &reply.message {
            Host::Rois(v) => Ok(v.rois.clone()),
            _ => Err(Error::protocol("expected Rois")),
        }
    }
    /// Submit dense pose corrections/extensions. The host validates the whole
    /// batch, ownership, components and adjacency before writing any region.
    pub fn extend_roi_poses(
        &self,
        extensions: Vec<pb::RoiPoseExtension>,
    ) -> Result<pb::RoiPosesExtended> {
        let reply = self.request(Plugin::ExtendRoiPoses(pb::ExtendRoiPoses { extensions }))?;
        match &reply.message {
            Host::RoiPosesExtended(v) => Ok(*v),
            _ => Err(Error::protocol("expected RoiPosesExtended")),
        }
    }
    /// Solicited bookkeeping for one request, not a liveness guarantee.
    /// UNKNOWN does not establish failure; awaiting_user describes a picker.
    pub fn request_status(&self, subject_id: &str, timeout: Duration) -> Result<pb::RequestStatus> {
        let reply = self.request_timeout(
            Plugin::RequestStatusQuery(pb::RequestStatusQuery {
                subject_id: subject_id.into(),
            }),
            timeout,
        )?;
        match &reply.message {
            Host::RequestStatus(v) if v.subject_id == subject_id => Ok(v.clone()),
            _ => Err(Error::protocol(
                "expected RequestStatus for requested subject",
            )),
        }
    }
    /// The caller chooses the picker deadline; a clean cancellation is a
    /// successful result with `declined = true`.
    /// Command handlers may call this and wait, holding the session's request
    /// slot while the person browses. Plugins serving other work should use
    /// `after_reply`, or `start_request` with its waiter on an application worker,
    /// and return promptly from the command.
    pub fn request_asset(
        &self,
        slot: &str,
        force_reprompt: bool,
        timeout: Duration,
    ) -> Result<pb::AssetResult> {
        let reply = self.request_timeout(
            Plugin::RequestAsset(pb::RequestAsset {
                slot: slot.into(),
                force_reprompt,
            }),
            timeout,
        )?;
        match &reply.message {
            Host::AssetResult(v) if v.slot == slot => Ok(v.clone()),
            _ => Err(Error::protocol("expected AssetResult for requested slot")),
        }
    }
    /// Ask for an output path; cancellation is a successful declined result.
    /// No permission is needed and the host creates no file. The caller picks
    /// the timeout. Command handlers may call this and wait, holding the session's
    /// request slot while the person browses. Plugins serving other work should
    /// use `after_reply`, or `start_request` with its waiter on an application
    /// worker, and return promptly from the command.
    pub fn request_save_path(
        &self,
        suggested_name: &str,
        extension: &str,
        title: &str,
        timeout: Duration,
    ) -> Result<pb::SavePathResult> {
        let reply = self.request_timeout(
            Plugin::RequestSavePath(pb::RequestSavePath {
                suggested_name: suggested_name.into(),
                extension: extension.into(),
                title: title.into(),
            }),
            timeout,
        )?;
        match &reply.message {
            Host::SavePathResult(v) if v.declined == v.path.is_empty() => Ok(v.clone()),
            _ => Err(Error::protocol(
                "expected SavePathResult with a path exactly when not declined",
            )),
        }
    }
    /// The host's rendered colormap, exactly 256 RGBA u8 entries. No permission
    /// or open dataset is needed. Unknown names return the host's NOT_FOUND.
    pub fn get_colormap(&self, name: &str) -> Result<pb::ColormapTable> {
        let reply = self.request(Plugin::GetColormap(pb::GetColormap { name: name.into() }))?;
        match &reply.message {
            Host::ColormapTable(v) if v.name == name && v.rgba.len() == 1024 => Ok(v.clone()),
            _ => Err(Error::protocol(
                "expected ColormapTable for requested name with 1024 RGBA bytes",
            )),
        }
    }
    pub fn panel(&self, id: impl Into<String>) -> Panel {
        Panel {
            ctx: self.clone(),
            id: id.into(),
            row_key: String::new(),
        }
    }
}

/// A panel handle; each operation goes through Context's checked patch path.
#[derive(Clone)]
pub struct Panel {
    ctx: Context,
    id: String,
    row_key: String,
}
impl Panel {
    /// Address cells in one list row. The host checks that the row exists and
    /// that each widget belongs to its template. Ordinary panel handles have no row.
    pub fn row(&self, key: impl Into<String>) -> Result<Self> {
        let key = key.into();
        if key.is_empty() || key.chars().count() > 128 {
            return Err(Error::protocol("row key must contain 1 to 128 characters"));
        }
        Ok(Self {
            ctx: self.ctx.clone(),
            id: self.id.clone(),
            row_key: key,
        })
    }
    /// Replace the whole ordered row list. Invalid input is refused before any
    /// push; the host owns row state and resets removed/re-added keys to defaults.
    pub fn set_rows(
        &self,
        widget: &str,
        keys: impl IntoIterator<Item = impl Into<String>>,
    ) -> Result<()> {
        if !self.row_key.is_empty() {
            return Err(Error::protocol("set_rows addresses a list, not a row cell"));
        }
        let max = self
            .ctx
            .core
            .manifest
            .list_max_rows(&self.id, widget)
            .ok_or_else(|| Error::protocol("set_rows requires a declared list"))?;
        let mut seen = std::collections::BTreeSet::new();
        let mut rows = Vec::new();
        for key in keys {
            let key = key.into();
            if rows.len() == max
                || key.is_empty()
                || key.chars().count() > 128
                || !seen.insert(key.clone())
            {
                return Err(Error::protocol(
                    "row keys must be distinct, contain 1 to 128 characters, and fit max_rows",
                ));
            }
            rows.push(key);
        }
        self.patch(
            widget,
            pb::patch_op::Op::SetRows(pb::RowKeys { keys: rows }),
        )
    }
    /// Publish PNG bytes, holding the latest image until the panel's segment installs.
    pub fn push_image(&self, widget: &str, png: &[u8]) -> Result<()> {
        self.ctx
            .push_image_row(&self.id, widget, &self.row_key, png)
    }
    fn patch(&self, widget: &str, op: pb::patch_op::Op) -> Result<()> {
        self.ctx.patch(
            &self.id,
            vec![pb::PatchOp {
                widget_id: widget.into(),
                row_key: self.row_key.clone(),
                op: Some(op),
            }],
        )
    }
    pub fn set_value(&self, widget: &str, value: impl Into<pb::Value>) -> Result<()> {
        self.patch(widget, pb::patch_op::Op::SetValue(value.into()))
    }
    /// The host clamps value to [0, 1], displaying NaN as empty;
    /// indeterminate takes precedence and ignores value.
    /// Progress is noninteractive and never echoed as a command.
    pub fn set_progress(&self, widget: &str, value: f64, indeterminate: bool) -> Result<()> {
        self.patch(
            widget,
            pb::patch_op::Op::SetProgress(pb::Progress {
                value,
                indeterminate,
            }),
        )
    }
    pub fn set_enabled(&self, widget: &str, enabled: bool) -> Result<()> {
        self.patch(widget, pb::patch_op::Op::SetEnabled(enabled))
    }
    pub fn set_visible(&self, widget: &str, visible: bool) -> Result<()> {
        self.patch(widget, pb::patch_op::Op::SetVisible(visible))
    }
    pub fn set_options(
        &self,
        widget: &str,
        options: impl IntoIterator<Item = impl Into<String>>,
    ) -> Result<()> {
        self.patch(
            widget,
            pb::patch_op::Op::SetOptions(pb::Options {
                options: options.into_iter().map(Into::into).collect(),
            }),
        )
    }
}
macro_rules! value_from {
    ($ty:ty, $variant:ident) => {
        impl From<$ty> for pb::Value {
            fn from(v: $ty) -> Self {
                Self {
                    kind: Some(pb::value::Kind::$variant(v)),
                }
            }
        }
    };
}
value_from!(bool, B);
value_from!(i64, I);
value_from!(f64, F);
value_from!(String, S);
impl From<Vec<i64>> for pb::Value {
    fn from(values: Vec<i64>) -> Self {
        Self {
            kind: Some(pb::value::Kind::Ints(pb::IntList { values })),
        }
    }
}
impl From<&str> for pb::Value {
    fn from(v: &str) -> Self {
        v.to_owned().into()
    }
}

impl Panel {
    /// Offer before or after the panel opens. Only the person can start it.
    pub fn offer_follow(&self, label: impl Into<String>) -> Result<()> {
        let label = label.into();
        if label.is_empty() {
            return Err(Error::protocol("use withdraw_follow to remove an offer"));
        }
        self.ctx.push(Plugin::OfferFollow(pb::OfferFollow {
            panel_id: self.id.clone(),
            label,
        }))
    }
    pub fn withdraw_follow(&self) -> Result<()> {
        self.ctx.push(Plugin::OfferFollow(pb::OfferFollow {
            panel_id: self.id.clone(),
            label: String::new(),
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn view_spacing_is_known_axis_by_axis_with_positive_finite_measurements() {
        for (case, provenance, unit, spacing, expected) in [
            (
                "partly recorded",
                1,
                "um",
                [f64::NAN, 0.5, 0.25],
                [None, Some(0.5), Some(0.25)],
            ),
            (
                "all axes",
                1,
                "um",
                [2., 0.5, 0.25],
                [Some(2.), Some(0.5), Some(0.25)],
            ),
            ("display fallback", 3, "", [1.; 3], [None; 3]),
            ("fallback with unit", 3, "um", [1.; 3], [None; 3]),
            ("file without unit", 1, "", [2., 0.5, 0.25], [None; 3]),
            (
                "garbage file pitch",
                1,
                "um",
                [0., -1., f64::INFINITY],
                [None; 3],
            ),
        ] {
            let view = pb::ViewState {
                spacing_zyx: spacing.to_vec(),
                spacing_unit: unit.into(),
                spacing_provenance: provenance,
                ..Default::default()
            };
            let measured = view.measured_spacing_zyx();
            let known = view.axis_known_zyx();
            assert_eq!(measured, expected, "{case}");
            assert_eq!(known, expected.map(|axis| axis.is_some()), "{case}");
            for axis in 0..3 {
                assert_eq!(
                    measured[axis].is_none(),
                    !known[axis],
                    "{case}, axis {axis}"
                );
                if let Some(pitch) = measured[axis] {
                    assert!(pitch > 0. && pitch.is_finite(), "{case}, axis {axis}");
                }
            }
            assert_eq!(
                view.spacing_known(),
                known.into_iter().any(|axis| axis),
                "{case}"
            );
            let all_axes = match expected {
                [Some(z), Some(y), Some(x)] => Some([z, y, x]),
                _ => None,
            };
            assert_eq!(view.physical_spacing(), all_axes, "{case}");
        }
    }

    #[test]
    fn polled_and_epoch_spacing_share_calibration_rules() {
        for provenance in [0, 1, 2, 3, 99] {
            for unit in ["", "um"] {
                let geometry = pb::Geometry {
                    spacing_z: 3.,
                    spacing_y: 2.,
                    spacing_x: 1.,
                    spacing_unit: unit.into(),
                    spacing_provenance: provenance,
                    ..Default::default()
                };
                let mut view = pb::ViewState {
                    spacing_zyx: vec![3., 2., 1.],
                    spacing_unit: unit.into(),
                    spacing_provenance: provenance,
                    ..Default::default()
                };
                let expected =
                    (matches!(provenance, 1 | 2) && !unit.is_empty()).then_some([3., 2., 1.]);
                assert_eq!(geometry.physical_spacing(), expected);
                assert_eq!(view.physical_spacing(), expected);
                assert_eq!(view.axis_known_zyx(), [expected.is_some(); 3]);
                assert_eq!(view.spacing_known(), expected.is_some());
                for invalid in [vec![], vec![1., 1.], vec![1.; 4]] {
                    view.spacing_zyx = invalid;
                    assert_eq!(view.physical_spacing(), None);
                    assert_eq!(view.measured_spacing_zyx(), [None; 3]);
                    assert_eq!(view.axis_known_zyx(), [false; 3]);
                    assert!(!view.spacing_known());
                }
            }
        }
        let mut view = pb::ViewState {
            spacing_zyx: vec![f64::NAN, 0.3208, 0.3208],
            spacing_unit: "um".into(),
            spacing_provenance: 1,
            ..Default::default()
        };
        assert_eq!(
            view.measured_spacing_zyx(),
            [None, Some(0.3208), Some(0.3208)]
        );
        assert_eq!(view.axis_known_zyx(), [false, true, true]);
        assert!(view.spacing_known());
        assert_eq!(view.physical_spacing(), None);
        // Units and measured provenance remain necessary, even for finite axes.
        view.spacing_unit.clear();
        assert_eq!(view.measured_spacing_zyx(), [None; 3]);
        assert!(!view.spacing_known());
        view.spacing_unit = "um".into();
        view.spacing_provenance = 3;
        assert_eq!(view.measured_spacing_zyx(), [None; 3]);
        view.spacing_provenance = 1;
        view.spacing_zyx = vec![f64::INFINITY, 2., f64::NEG_INFINITY];
        assert_eq!(view.measured_spacing_zyx(), [None, Some(2.), None]);
        assert_eq!(view.axis_known_zyx(), [false, true, false]);
        assert_eq!(view.physical_spacing(), None);
        view.spacing_zyx = vec![f64::NAN; 3];
        assert_eq!(view.measured_spacing_zyx(), [None; 3]);
        assert!(!view.spacing_known());
    }
    #[test]
    fn typed_values_preserve_endianness_nonfinite_values_and_reject_partial_values() {
        assert_eq!(
            decode(vec![0, 1, 255, 255], pb::DType::DtypeU16 as i32).unwrap(),
            VoxelData::U16(vec![256, 65535])
        );
        let bytes: Vec<_> = [f32::NEG_INFINITY, 2., f32::INFINITY, f32::NAN]
            .into_iter()
            .flat_map(f32::to_le_bytes)
            .collect();
        let values = decode(bytes, pb::DType::DtypeF32 as i32)
            .unwrap()
            .into_f32();
        assert_eq!(&values[..3], &[f32::NEG_INFINITY, 2., f32::INFINITY]);
        assert!(values[3].is_nan());
        assert!(decode(vec![1], pb::DType::DtypeU16 as i32).is_err());
        assert!(decode(vec![0; 8], pb::DType::DtypeF64 as i32).is_err());
    }
}
