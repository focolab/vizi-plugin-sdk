use super::*;
#[derive(Clone, Debug)]
pub struct PaneState {
    pub pane: pb::PaneEpoch,
    pub dataset: pb::DatasetState,
    pub time: Option<Arc<pb::TimeMapping>>,
}
impl PartialEq for PaneState {
    fn eq(&self, other: &Self) -> bool {
        self.pane == other.pane
            && self.dataset == other.dataset
            && match (&self.time, &other.time) {
                (Some(a), Some(b)) => time_mapping_eq(a, b),
                (None, None) => true,
                _ => false,
            }
    }
}
// Non-finite mappings are legal to receive. Compare their wire values so an
// unchanged NaN can be re-acknowledged instead of becoming a content mismatch.
fn time_mapping_eq(a: &pb::TimeMapping, b: &pb::TimeMapping) -> bool {
    a.gen == b.gen
        && a.revision == b.revision
        && a.t.len() == b.t.len()
        && a.t
            .iter()
            .zip(&b.t)
            .all(|(a, b)| a.to_bits() == b.to_bits())
}
impl PaneState {
    /// Absence stays absent; display-fallback spacing is not a measurement.
    pub fn physical_spacing(&self) -> Option<[f64; 3]> {
        self.dataset.geometry.as_ref()?.physical_spacing()
    }
    pub fn time_at(&self, index: u32) -> Option<f64> {
        self.time.as_ref()?.t.get(index as usize).copied()
    }
}
/// An input retains the arrangement under which it arrived. A terminal with
/// unusable context still completes its interaction; coordinates are not guessed.
#[derive(Clone, Debug)]
pub struct Input {
    pub event: crate::InputEvent,
    pub implicitly_closed: Option<u32>,
    pub state: Option<Arc<PaneState>>,
    pub usable_context: bool,
    pub binding: Option<pb::ToolBinding>,
}
impl Input {
    /// Resolve a FIRE action using this input's captured binding. Unknown
    /// indices and primary gestures have no action ID and remain deliverable.
    pub fn action(&self) -> Option<&str> {
        if self.event.phase != crate::Phase::Fire || self.event.action == 0 {
            return None;
        }
        self.binding
            .as_ref()?
            .actions
            .iter()
            .find(|a| a.action == u32::from(self.event.action))
            .map(|a| a.action_id.as_str())
    }
}
#[derive(Default)]
pub(super) struct Arrangements {
    pub panes: BTreeMap<u32, Arc<PaneState>>,
    tools: BTreeMap<u32, pb::ToolBinding>,
    // Every handle a statement has named, recorded before any refusal: a
    // refused statement was still received, so the next is not the first.
    named_tools: std::collections::BTreeSet<u32>,
    times: BTreeMap<u64, Arc<pb::TimeMapping>>,
}
impl Core {
    pub(super) fn time_mapping(&self, t: pb::TimeMapping) -> Result<()> {
        if t.gen == 0 || t.revision == 0 {
            self.memory.report(4, "zero time mapping identity", vec![]);
            return Ok(());
        }
        if t.t.iter().any(|v| !v.is_finite()) || t.t.windows(2).any(|a| a[0] > a[1]) {
            eprintln!("vizi SDK: installing non-finite or non-ascending time mapping");
        }
        let mut a = self.arrangements.lock().unwrap();
        if let Some(old) = a.times.get(&t.gen) {
            if t.revision < old.revision {
                self.memory
                    .report(20, "time mapping revision regressed", vec![]);
                return Ok(());
            }
            if t.revision == old.revision && !time_mapping_eq(old, &t) {
                self.memory
                    .report(19, "time mapping content mismatch", vec![]);
                return Ok(());
            }
        } else if t.revision != 1 {
            self.memory
                .report(26, "first time mapping revision is not 1", vec![]);
        }
        let total: usize = a
            .times
            .iter()
            .filter(|(gen, _)| **gen != t.gen)
            .map(|(_, v)| v.t.len())
            .sum();
        if total + t.t.len() > 8_388_608 || !a.times.contains_key(&t.gen) && a.times.len() >= 65536
        {
            eprintln!("vizi SDK: retained time mapping budget exceeded");
            return Ok(());
        }
        let ack = pb::TimeMappingAck {
            gen: t.gen,
            revision: t.revision,
        };
        a.times.insert(t.gen, Arc::new(t));
        drop(a);
        self.push(Plugin::TimeMappingAck(ack))
    }
    pub(super) fn tool_binding(&self, b: pb::ToolBinding) -> Result<Option<pb::ToolBinding>> {
        if b.tool == 0 || b.revision == 0 {
            self.memory.report(4, "zero tool binding identity", vec![]);
            return Ok(None);
        }
        let first = {
            let mut a = self.arrangements.lock().unwrap();
            let first = !a.named_tools.contains(&b.tool);
            if first && a.named_tools.len() < 65536 {
                a.named_tools.insert(b.tool);
            }
            first
        };
        // A mode this build does not know is a later minor's, not a host bug:
        // the binding is refused alone, neither activated nor acknowledged
        // (ToolBindingAck carries no reason), and the reason is logged here.
        if !b.retired && pb::ToolMode::try_from(b.mode).is_err() {
            eprintln!(
                "vizi SDK: ToolBinding tool {} revision {} refused, not activated or acknowledged: \
                 mode {} is not a ToolMode this build knows",
                b.tool, b.revision, b.mode
            );
            return Ok(None);
        }
        if !self.manifest.contribution_ids("tools").contains(&b.tool_id) {
            eprintln!(
                "vizi SDK: tool binding names undeclared tool {:?}",
                b.tool_id
            );
        }
        let mut a = self.arrangements.lock().unwrap();
        if let Some(old) = a.tools.get(&b.tool) {
            if b.revision < old.revision {
                self.memory.report(20, "tool revision regressed", vec![]);
                return Ok(None);
            }
            if b.revision == old.revision && old != &b {
                self.memory.report(19, "tool content mismatch", vec![]);
                return Ok(None);
            }
            if old.retired && !b.retired {
                eprintln!("vizi SDK: retired tool cannot be reactivated");
                return Ok(None);
            }
        } else if first && b.revision != 1 {
            self.memory
                .report(26, "first tool revision is not 1", vec![]);
        }
        if !a.tools.contains_key(&b.tool) && a.tools.len() >= 65536 {
            eprintln!("vizi SDK: tool identity budget exceeded");
            return Ok(None);
        }
        let changed = match a.tools.get(&b.tool) {
            Some(old) => old.retired != b.retired,
            None => !b.retired,
        };
        let applied = if b.retired {
            self.bell.tool_retired(b.tool)
        } else {
            self.bell.tool_activated(b.tool)
        };
        if let Err(e) = applied {
            eprintln!("vizi SDK: tool lifecycle rejected: {e:?}");
            return Ok(None);
        }
        let ack = pb::ToolBindingAck {
            tool: b.tool,
            revision: b.revision,
        };
        a.tools.insert(b.tool, b.clone());
        drop(a);
        self.push(Plugin::ToolBindingAck(ack))?;
        Ok(changed.then_some(b))
    }
    pub(super) fn epoch(&self, change: pb::EpochChange) -> Result<()> {
        let datasets: BTreeMap<_, _> = change.datasets.iter().map(|d| (d.gen, d)).collect();
        if datasets.len() != change.datasets.len() {
            eprintln!("vizi SDK: duplicate dataset in epoch; keeping the last entry");
        }
        let mut seen = std::collections::BTreeSet::new();
        let mut a = self.arrangements.lock().unwrap();
        let mut accepted = Vec::new();
        for pane in change.panes {
            if pane.view_id > 255 {
                eprintln!("vizi SDK: installing pane above doorbell view_id range");
            }
            if pane.epoch == 0 || pane.gen == 0 {
                self.memory.report(4, "zero pane identity", vec![]);
                continue;
            }
            if !seen.insert(pane.view_id) {
                eprintln!(
                    "vizi SDK: duplicate pane {} in epoch; keeping the first entry",
                    pane.view_id
                );
                continue;
            }
            let Some(dataset) = datasets.get(&pane.gen) else {
                self.memory.report(3, "epoch missing dataset", vec![]);
                continue;
            };
            // §8.10 does not make geometry presence or its gen metadata an
            // acknowledgement gate. Preserve the whole supplied snapshot;
            // input contextualization separately handles unavailable geometry.
            match &dataset.geometry {
                None => eprintln!("vizi SDK: epoch for pane {} has no geometry", pane.view_id),
                Some(g) if g.gen != pane.gen => eprintln!(
                    "vizi SDK: geometry generation {} differs from dataset {}",
                    g.gen, pane.gen
                ),
                _ => {}
            }
            let time = if dataset.time_mapping_revision == 0 {
                None
            } else {
                match a
                    .times
                    .get(&pane.gen)
                    .filter(|t| t.revision == dataset.time_mapping_revision)
                {
                    Some(t) => Some(t.clone()),
                    None => {
                        self.memory.report(
                            3,
                            "epoch references unacknowledged time mapping",
                            vec![],
                        );
                        continue;
                    }
                }
            };
            let snapshot = Arc::new(PaneState {
                pane: pane.clone(),
                dataset: (**dataset).clone(),
                time,
            });
            if let Some(old) = a.panes.get(&pane.view_id) {
                if pane.epoch < old.pane.epoch {
                    self.memory.report(20, "pane epoch regressed", vec![]);
                    continue;
                }
                if pane.epoch == old.pane.epoch && **old != *snapshot {
                    self.memory
                        .report(19, "pane arrangement content mismatch", vec![]);
                    continue;
                }
            }
            if !a.panes.contains_key(&pane.view_id) && a.panes.len() >= 65536 {
                eprintln!("vizi SDK: pane identity budget exceeded");
                continue;
            }
            if let Ok(view) = u8::try_from(pane.view_id) {
                if let Err(e) = self.bell.epoch_acknowledged(view, pane.epoch) {
                    eprintln!("vizi SDK: epoch lifecycle rejected: {e:?}");
                    continue;
                }
            }
            a.panes.insert(pane.view_id, snapshot);
            accepted.push(pane);
        }
        drop(a);
        if !accepted.is_empty() {
            self.push(Plugin::EpochAck(pb::EpochAck { panes: accepted }))?;
        }
        Ok(())
    }
    pub(super) fn input(&self, event: crate::InputEvent, implicitly_closed: Option<u32>) -> Input {
        let a = self.arrangements.lock().unwrap();
        let state = a
            .panes
            .get(&u32::from(event.view_id))
            .cloned()
            .filter(|s| s.pane.gen as u32 == event.gen && s.pane.epoch == event.epoch);
        let reason = match state.as_ref() {
            None => Some("pane generation/epoch has no acknowledged context"),
            Some(s) => {
                if s.dataset
                    .geometry
                    .as_ref()
                    .is_none_or(|g| (event.level as usize) >= g.levels.len())
                {
                    Some("level does not name an arranged geometry level")
                } else if s
                    .time
                    .as_ref()
                    .is_some_and(|t| (event.t as usize) >= t.t.len())
                {
                    Some("t is outside the arranged time table")
                } else if !position_in_context(&event, s) {
                    Some("voxel coordinate is outside the arranged crop/extent at its level")
                } else {
                    None
                }
            }
        };
        if let Some(reason) = reason {
            self.memory
                .report(pb::ViolationSite::UnusableContext as i32, reason, vec![]);
        }
        Input {
            binding: a.tools.get(&event.tool).cloned(),
            event,
            implicitly_closed,
            state,
            usable_context: reason.is_none(),
        }
    }
}
/// Map a level-0 edge to the target sampling grid, preserving its grid phase.
pub fn edge_at_level(edge: f64, scale: f64) -> Option<f64> {
    if !edge.is_finite() || !scale.is_finite() || scale <= 0.0 {
        None
    } else {
        Some((edge + 0.5) / scale - 0.5)
    }
}
fn position_in_context(event: &crate::InputEvent, state: &PaneState) -> bool {
    use crate::Position;
    if event.miss || matches!(event.position, Position::None | Position::Ray(_)) {
        return true;
    }
    let Some(g) = &state.dataset.geometry else {
        return false;
    };
    let (Some(crop), Some(extent), Some(scale)) =
        (&g.crop, &g.extent, g.levels.get(event.level as usize))
    else {
        return false;
    };
    let bounds = [
        (
            crop.z0.max(extent.z0 as f64 - 0.5),
            crop.z1.min(extent.z_n as f64 - 0.5),
            scale.z,
        ),
        (
            crop.y0.max(extent.y0 as f64 - 0.5),
            crop.y1.min(extent.y_n as f64 - 0.5),
            scale.y,
        ),
        (
            crop.x0.max(extent.x0 as f64 - 0.5),
            crop.x1.min(extent.x_n as f64 - 0.5),
            scale.x,
        ),
    ];
    let inside = |point: [f32; 3]| {
        bounds.iter().zip(point).all(|(&(lo, hi, scale), v)| {
            let (Some(lo), Some(hi)) = (edge_at_level(lo, scale), edge_at_level(hi, scale)) else {
                return false;
            };
            // Allow f32 transport roundoff at an edge, not a voxel of clamping.
            let tolerance = 8.0 * f32::EPSILON as f64 * (1.0 + lo.abs().max(hi.abs()));
            lo <= hi && (v as f64) >= lo - tolerance && (v as f64) <= hi + tolerance
        })
    };
    match &event.position {
        Position::None => true,
        Position::Voxel(p) | Position::Picked(p) => inside(*p),
        // E §2 reconciles named points only. Column endpoints are half-open
        // ranges, not voxel centers (the exclusive end may lie past the crop).
        Position::ColumnYx { .. } | Position::ColumnZx { .. } | Position::ColumnZy { .. } => true,
        Position::Ray(_) => true,
        Position::RayAndPicked { picked, .. } => inside(*picked),
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    #[test]
    fn retention_budgets_refuse_locally_and_allow_existing_identity_updates() {
        let manifest = crate::manifest::Manifest::parse(
            r#"{"manifest_version":1,"id":"test.budgets","name":"Budgets","version":"1.0.0","engines":{"vizi":"1.0"},"entry":{"command":["test"]},"contributions":{"tools":[{"id":"tool"}]}}"#,
        ).unwrap();
        let (fd, _peer) = crate::socket_pair().unwrap();
        let (bell, listener) = Doorbell::new(fd, Default::default()).unwrap();
        let runtime = Runtime::new(
            manifest,
            Arc::new(crate::plugin::Registry::new()),
            bell,
            listener,
        )
        .unwrap();
        let ctx = runtime.context();
        ctx.core.bell.handshake_completed(1, 0).unwrap();
        *ctx.core.peer.lock().unwrap() = Some((1, 0));

        // Seed retained state directly so the test reaches local budgets
        // without sending tens of thousands of arrangements through gRPC.
        {
            let mut a = ctx.core.arrangements.lock().unwrap();
            for gen in 1..=8 {
                a.times.insert(
                    gen,
                    Arc::new(pb::TimeMapping {
                        gen,
                        revision: 1,
                        t: vec![0.; 1_048_576],
                    }),
                );
            }
        }
        ctx.core
            .time_mapping(pb::TimeMapping {
                gen: 9,
                revision: 1,
                t: vec![0.],
            })
            .unwrap();
        assert!(!ctx.core.arrangements.lock().unwrap().times.contains_key(&9));
        assert!(ctx.core.queues.lock().unwrap().reliable.is_empty());
        {
            let mut a = ctx.core.arrangements.lock().unwrap();
            a.times.clear();
            for gen in 1..=65536 {
                a.times.insert(
                    gen,
                    Arc::new(pb::TimeMapping {
                        gen,
                        revision: 1,
                        t: vec![],
                    }),
                );
            }
            for tool in 1..=65536 {
                a.tools.insert(
                    tool,
                    pb::ToolBinding {
                        tool,
                        tool_id: "tool".into(),
                        revision: 1,
                        ..Default::default()
                    },
                );
            }
        }
        ctx.core
            .time_mapping(pb::TimeMapping {
                gen: 65537,
                revision: 1,
                t: vec![],
            })
            .unwrap();
        ctx.core
            .tool_binding(pb::ToolBinding {
                tool: 65537,
                tool_id: "tool".into(),
                revision: 1,
                ..Default::default()
            })
            .unwrap();
        assert!(ctx.core.queues.lock().unwrap().reliable.is_empty());
        assert!(ctx.memory().drain_events().unwrap().is_empty());
        assert!(!ctx
            .core
            .arrangements
            .lock()
            .unwrap()
            .tools
            .contains_key(&65537));
        ctx.core
            .time_mapping(pb::TimeMapping {
                gen: 1,
                revision: 2,
                t: vec![1.],
            })
            .unwrap();
        ctx.core
            .tool_binding(pb::ToolBinding {
                tool: 1,
                tool_id: "tool".into(),
                revision: 2,
                ..Default::default()
            })
            .unwrap();
        let a = ctx.core.arrangements.lock().unwrap();
        assert_eq!(a.times[&1].revision, 2);
        assert_eq!(a.tools[&1].revision, 2);
        drop(a);
        assert_eq!(ctx.core.queues.lock().unwrap().reliable.len(), 2);
        runtime.stop();
    }
}

fn measured_spacing(provenance: i32, unit: &str, spacing: [f64; 3]) -> Option<[f64; 3]> {
    (matches!(provenance, 1 | 2) && !unit.is_empty()).then_some(spacing)
}
impl pb::Geometry {
    /// Z/Y/X voxel pitch, only when file or override calibration is known.
    pub fn physical_spacing(&self) -> Option<[f64; 3]> {
        measured_spacing(
            self.spacing_provenance,
            &self.spacing_unit,
            [self.spacing_z, self.spacing_y, self.spacing_x],
        )
    }
}
impl pb::ViewState {
    /// Host-wide polled calibration. Tools should use their pane epoch instead.
    /// Answers for **all three axes**: Some only when every pitch is known,
    /// positive and finite. Otherwise None. For a partly recorded file, check
    /// each axis with `axis_known_zyx` before using `measured_spacing_zyx`.
    pub fn physical_spacing(&self) -> Option<[f64; 3]> {
        let [z, y, x] = self.measured_spacing_zyx();
        Some([z?, y?, x?])
    }
    /// Per-axis Z/Y/X measurements in `spacing_unit`. Check `axis_known_zyx()[i]`
    /// for every axis you use before using its pitch: one known axis says nothing
    /// about the rest. The value is None exactly where that axis is not known,
    /// and Some with a positive, finite pitch elsewhere. Leave unknown axes in
    /// voxels; display fallback is never a measurement.
    pub fn measured_spacing_zyx(&self) -> [Option<f64>; 3] {
        if !matches!(self.spacing_provenance, 1 | 2) || self.spacing_unit.is_empty() {
            return [None; 3];
        }
        let Ok(spacing) = <[f64; 3]>::try_from(self.spacing_zyx.as_slice()) else {
            return [None; 3];
        };
        spacing.map(|axis| (axis > 0.0 && axis.is_finite()).then_some(axis))
    }
    /// Whether each Z/Y/X axis is a positive, finite file/override measurement
    /// with a unit. Check every axis you use: one known axis says nothing about
    /// the rest. `measured_spacing_zyx()[i]` is None exactly when this axis is
    /// false, and Some with a positive, finite pitch otherwise.
    pub fn axis_known_zyx(&self) -> [bool; 3] {
        self.measured_spacing_zyx().map(|axis| axis.is_some())
    }
    /// Answers whether **at least one axis** is measured. A true result does not license
    /// using every axis: check `axis_known_zyx()[i]` for each axis you use.
    /// Use `physical_spacing` when you need all three axes to be known.
    pub fn any_axis_known(&self) -> bool {
        self.axis_known_zyx().into_iter().any(|known| known)
    }
}
