use crate::wire::*;
use std::collections::BTreeMap;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Interaction {
    pub interaction: u32,
    pub tool: u32,
    pub gen: u32,
    pub view_id: u8,
    pub epoch: u32,
}
#[derive(Clone, Debug, PartialEq, Eq)]
#[non_exhaustive]
pub enum CancellationReason {
    Rejected(Violation),
    ToolRetired,
    EpochChanged,
    Supervisor,
    SessionEnded,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Cancellation {
    pub interaction: Interaction,
    pub reason: CancellationReason,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LifecycleError {
    Closed,
    InvalidHandle,
    OlderEpoch,
    ZeroEpoch,
    Capacity,
}
impl std::fmt::Display for LifecycleError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{self:?}")
    }
}
impl std::error::Error for LifecycleError {}

struct Open {
    context: Interaction,
    last_seq: u32,
    opening: RecordBytes,
}
pub(crate) struct InputState {
    tools: BTreeMap<u32, Option<u32>>,
    high_water: u32,
    watermark: u32,
    epochs: [u32; 256],
    open: BTreeMap<u32, Open>,
    max_tools: usize,
}
pub(crate) enum InputDisposition {
    Accepted {
        event: InputEvent,
        implicitly_closed: Option<u32>,
    },
    Ignored(DiagnosticKind),
    Rejected(Violation),
}
impl InputState {
    pub fn new(max_tools: usize) -> Self {
        Self {
            tools: BTreeMap::new(),
            high_water: 0,
            watermark: 0,
            epochs: [0; 256],
            open: BTreeMap::new(),
            max_tools,
        }
    }
    pub fn activate(&mut self, handle: u32) -> Result<(), LifecycleError> {
        if self.tools.contains_key(&handle) {
            return Ok(());
        }
        if handle == 0 || handle <= self.high_water {
            return Err(LifecycleError::InvalidHandle);
        }
        if self.tools.len() >= self.max_tools {
            return Err(LifecycleError::Capacity);
        }
        self.tools.insert(handle, None);
        self.high_water = handle;
        Ok(())
    }
    fn remove(&mut self, n: u32) -> Option<Interaction> {
        let entry = self.open.remove(&n)?;
        if self.tools.get(&entry.context.tool) == Some(&Some(n)) {
            self.tools.insert(entry.context.tool, None);
        }
        Some(entry.context)
    }
    pub fn cancel(&mut self, n: u32, reason: CancellationReason) -> Vec<Cancellation> {
        self.remove(n)
            .map(|interaction| Cancellation {
                interaction,
                reason,
            })
            .into_iter()
            .collect()
    }
    pub fn cancel_view(
        &mut self,
        view: Option<u8>,
        reason: CancellationReason,
    ) -> Vec<Cancellation> {
        let ids: Vec<_> = self
            .open
            .iter()
            .filter(|(_, v)| view.is_none_or(|v_id| v.context.view_id == v_id))
            .map(|(k, _)| *k)
            .collect();
        ids.into_iter()
            .flat_map(|id| self.cancel(id, reason.clone()))
            .collect()
    }
    pub fn retire(&mut self, h: u32) -> Result<Vec<Cancellation>, LifecycleError> {
        if h == 0 || h > self.high_water {
            return Err(LifecycleError::InvalidHandle);
        }
        let out = self
            .tools
            .get(&h)
            .copied()
            .flatten()
            .map_or_else(Vec::new, |n| {
                self.cancel(n, CancellationReason::ToolRetired)
            });
        self.tools.remove(&h);
        Ok(out)
    }
    pub fn epoch(&mut self, view: u8, epoch: u32) -> Result<Vec<Cancellation>, LifecycleError> {
        if epoch == 0 {
            return Err(LifecycleError::ZeroEpoch);
        }
        let old = self.epochs[view as usize];
        if epoch < old {
            return Err(LifecycleError::OlderEpoch);
        }
        if epoch == old {
            return Ok(Vec::new());
        }
        let out = self.cancel_view(Some(view), CancellationReason::EpochChanged);
        self.epochs[view as usize] = epoch;
        Ok(out)
    }
    fn reject(
        &mut self,
        r: &RawInput,
        phase: Phase,
        v: Violation,
        cancellations: &mut Vec<Cancellation>,
    ) -> InputDisposition {
        // E §2: a rejected PRESS closes the named active tool's predecessor.
        // Other rejected records prefer their interaction, then the named tool.
        let n = if phase == Phase::Fire {
            None
        } else if phase != Phase::Press && self.open.contains_key(&r.interaction) {
            Some(r.interaction)
        } else {
            self.tools.get(&r.tool).copied().flatten()
        };
        if let Some(n) = n {
            cancellations.extend(self.cancel(n, CancellationReason::Rejected(v)));
        }
        InputDisposition::Rejected(v)
    }
    pub fn process(
        &mut self,
        bytes: &RecordBytes,
        diagnostics: &mut Vec<Diagnostic>,
        cancellations: &mut Vec<Cancellation>,
    ) -> InputDisposition {
        use DiagnosticKind::*;
        let r = RawInput::new(&bytes[8..108]);
        let ignore = |kind, value, diags: &mut Vec<Diagnostic>| {
            diags.push(Diagnostic::notice(kind, value));
            InputDisposition::Ignored(kind)
        };
        let Some(phase) = Phase::from_byte(r.phase) else {
            return ignore(UnknownPhase, r.phase as u64, diagnostics);
        };
        // Exact replay comparison is required only while that interaction is live.
        if phase.is_opening() && r.interaction > 0 && r.interaction <= self.watermark {
            if let Some(open) = self.open.get(&r.interaction) {
                if open.opening != *bytes {
                    return self.reject(&r, phase, Violation::InteractionMismatch, cancellations);
                }
            }
            return ignore(DuplicateOpening, r.interaction as u64, diagnostics);
        }
        if r.zero_id() {
            return self.reject(&r, phase, Violation::ZeroId, cancellations);
        }
        if phase.is_opening() && r.seq != 0 {
            return self.reject(&r, phase, Violation::SeqOnOpen, cancellations);
        }
        if let Some(v) = r.local_violation() {
            return self.reject(&r, phase, v, cancellations);
        }
        let epoch = self.epochs[r.view_id as usize];
        if r.epoch < epoch {
            return ignore(StaleEpoch, r.epoch as u64, diagnostics);
        }
        if r.epoch > epoch {
            return self.reject(&r, phase, Violation::EpochMismatch, cancellations);
        }
        if r.tool > self.high_water {
            return self.reject(&r, phase, Violation::UnknownTool, cancellations);
        }
        if !self.tools.contains_key(&r.tool) {
            return ignore(RetiredTool, r.tool as u64, diagnostics);
        }
        if !phase.is_opening() {
            let Some(open) = self.open.get(&r.interaction) else {
                return ignore(UnknownInteraction, r.interaction as u64, diagnostics);
            };
            if open.context.gen != r.gen
                || open.context.view_id != r.view_id
                || open.context.tool != r.tool
            {
                return self.reject(&r, phase, Violation::InteractionMismatch, cancellations);
            }
            if r.seq <= open.last_seq {
                return ignore(DuplicateSequence, r.seq as u64, diagnostics);
            }
            // This cannot overflow: seq is greater than last_seq.
            if r.seq - open.last_seq > 1 {
                diagnostics.push(Diagnostic::sequence(
                    SequenceGap,
                    open.last_seq + 1,
                    r.seq,
                    r.seq - open.last_seq - 1,
                ));
            }
        }
        let mut implicitly_closed = None;
        if phase.is_opening() {
            self.watermark = r.interaction;
        }
        if phase == Phase::Press {
            if let Some(n) = self.tools.get(&r.tool).copied().flatten() {
                self.remove(n);
                implicitly_closed = Some(n);
            }
            self.open.insert(
                r.interaction,
                Open {
                    context: Interaction {
                        interaction: r.interaction,
                        tool: r.tool,
                        gen: r.gen,
                        view_id: r.view_id,
                        epoch: r.epoch,
                    },
                    last_seq: 0,
                    opening: *bytes,
                },
            );
            self.tools.insert(r.tool, Some(r.interaction));
        } else if phase.is_terminal() {
            self.remove(r.interaction);
        } else if phase == Phase::Drag {
            self.open.get_mut(&r.interaction).unwrap().last_seq = r.seq;
        }
        InputDisposition::Accepted {
            event: r.decode(phase, diagnostics),
            implicitly_closed,
        }
    }
}
