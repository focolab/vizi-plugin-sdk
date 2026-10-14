//! Capability/meaning join, region validation, and copy-based shared memory API.
//!
//! All returned bytes are private copies. Operations serialize locally, and
//! retirement therefore completes after any copy/claim already in progress.
mod mapping;
use crate::{proto as pb, MemoryGrant, PaneEpoch, RecordBytes};
use mapping::Mapping;
use pb::plugin_msg::Msg;
use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Failure {
    pub class: i32,
    pub detail: String,
}
pub type Result<T> = std::result::Result<T, Failure>;
impl std::fmt::Display for Failure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.detail)
    }
}
impl std::error::Error for Failure {}
impl Failure {
    pub fn malformed(s: impl Into<String>) -> Self {
        Self {
            class: 3,
            detail: s.into(),
        }
    }
    pub fn too_large(s: impl Into<String>) -> Self {
        Self {
            class: 2,
            detail: s.into(),
        }
    }
    pub fn retryable(s: impl Into<String>) -> Self {
        Self {
            class: 1,
            detail: s.into(),
        }
    }
    fn unsupported(s: impl Into<String>) -> Self {
        Self {
            class: 4,
            detail: s.into(),
        }
    }
    fn os(e: std::io::Error) -> Self {
        #[cfg(unix)]
        let retry = matches!(
            e.raw_os_error(),
            Some(libc::ENOMEM | libc::EMFILE | libc::ENFILE)
        );
        #[cfg(windows)]
        let retry = matches!(e.raw_os_error(), Some(8 | 14 | 1450 | 1455 | 4));
        Self {
            class: if retry { 1 } else { 5 },
            detail: e.to_string(),
        }
    }
}
#[derive(Clone, Debug)]
pub struct Limits {
    pub max_mapped_bytes: u64,
    pub max_segment_ids: usize,
}
impl Default for Limits {
    fn default() -> Self {
        Self {
            max_mapped_bytes: 4 * 1024 * 1024 * 1024,
            max_segment_ids: 65536,
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Payload {
    pub bytes: Vec<u8>,
    pub pane: Option<PaneEpoch>,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Loan {
    pub segment_id: u64,
    pub region: u32,
    pub offset: u64,
    pub len: u64,
}
#[derive(Clone, Copy, Default)]
struct LoanUses {
    declared: u32,
    named: u32,
    sent: u32,
    stranded: bool,
}
impl LoanUses {
    fn holds_claim(self) -> bool {
        self.named == 0
            || self.sent
                < if self.stranded {
                    self.named
                } else {
                    self.declared
                }
    }
}

#[derive(Default)]
struct Runtime {
    quarantined: bool,
    staged_busy: bool,
    staged_iterations: BTreeMap<(u32, u32), u32>,
    next_revision: u64,
    last_seen: u64,
    cursor: u64,
    // Reservation -> (declared uses, admitted messages, handed to transport).
    // A reservation pins retirement while unnamed or while admitted messages
    // remain unsent. Promised uses alone must not pin a stranded reservation.
    loans: BTreeMap<(u64, u64), LoanUses>,
}
#[derive(Default)]
struct Entry {
    record: Option<RecordBytes>,
    grant: Option<MemoryGrant>,
    install: Option<pb::InstallSegment>,
    mapping: Option<Mapping>,
    ack: Option<pb::GrantAck>,
    retiring: bool,
    in_flight: usize,
    released: bool,
    // Exact reservations still out at unmap; each crossing may settle once.
    crossed_loans: BTreeSet<(u32, u64, u64)>,
    release_requested: bool,
    regions: Vec<Runtime>,
}
struct State {
    entries: BTreeMap<u64, Entry>,
    events: VecDeque<Msg>,
    overflow: bool,
    retirement_drops: u64,
    crossed_releases: u64,
    closed: bool,
    shortfalls: BTreeMap<(u64, u32), pb::SegmentShortfall>,
    violations: BTreeMap<i32, pb::ContractViolation>,
    reported_sites: BTreeSet<i32>,
    last_report: Instant,
    limits: Limits,
    subjects: Option<(BTreeSet<String>, BTreeSet<String>)>,
    // Received descriptor uses are accounted until the final disposition.
    deposits: BTreeMap<(u64, u64, u64), (u32, u32)>,
}
#[derive(Clone)]
pub struct Memory {
    inner: Arc<Mutex<State>>,
}
impl Default for Memory {
    fn default() -> Self {
        Self::new(Limits::default(), None)
    }
}
impl Memory {
    pub fn new(limits: Limits, manifest: Option<&crate::manifest::Manifest>) -> Self {
        Self {
            inner: Arc::new(Mutex::new(State {
                entries: BTreeMap::new(),
                events: VecDeque::new(),
                overflow: false,
                retirement_drops: 0,
                crossed_releases: 0,
                closed: false,
                shortfalls: BTreeMap::new(),
                violations: BTreeMap::new(),
                reported_sites: BTreeSet::new(),
                last_report: Instant::now() - Duration::from_secs(1),
                limits,
                subjects: manifest
                    .map(|m| (m.contribution_ids("overlays"), m.contribution_ids("panels"))),
                deposits: BTreeMap::new(),
            })),
        }
    }
    pub fn install(&self, install: pb::InstallSegment) -> Result<()> {
        let mut s = self.inner.lock().unwrap();
        let id = install.segment_id;
        s.admit(id)?;
        if s.entries[&id].released {
            s.released(id);
            return Ok(());
        }
        if let Some(old) = &s.entries[&id].install {
            if old != &install {
                s.conflict(id);
                return Ok(());
            }
            s.replay(id);
            return Ok(());
        }
        let check = validate_install(&install, s.subjects.as_ref());
        s.entries.get_mut(&id).unwrap().install = Some(install);
        if s.entries[&id].ack.is_some() {
            s.replay(id);
            return Ok(());
        }
        if let Err(e) = check {
            s.fail(id, e);
            return Ok(());
        }
        s.finish(id);
        Ok(())
    }
    #[cfg(unix)]
    pub fn grant(
        &self,
        grant: MemoryGrant,
        record: RecordBytes,
        fd: std::os::fd::OwnedFd,
    ) -> Result<()> {
        self.grant_with(grant, record, |g| Mapping::open(g, fd))
    }
    #[cfg(windows)]
    pub fn grant_named(&self, grant: MemoryGrant, record: RecordBytes, name: &str) -> Result<()> {
        self.grant_with(grant, record, |g| Mapping::open(g, name))
    }
    fn grant_with(
        &self,
        grant: MemoryGrant,
        record: RecordBytes,
        open: impl FnOnce(&MemoryGrant) -> Result<Mapping>,
    ) -> Result<()> {
        let mut s = self.inner.lock().unwrap();
        let id = grant.segment_id;
        s.admit(id)?;
        if s.entries[&id].released {
            drop(open);
            s.released(id);
            return Ok(());
        }
        if let Some(old) = s.entries[&id].record {
            drop(open);
            if old != record {
                s.conflict(id);
            } else {
                s.replay(id);
            }
            return Ok(());
        }
        let entry = s.entries.get_mut(&id).unwrap();
        entry.record = Some(record);
        entry.grant = Some(grant.clone());
        if entry.ack.is_some() {
            drop(open);
            s.replay(id);
            return Ok(());
        }
        let mapped: u64 = s
            .entries
            .values()
            .filter(|e| e.mapping.is_some())
            .map(|e| e.grant.as_ref().unwrap().size)
            .sum();
        if grant.size < 32 {
            drop(open);
            s.fail(id, Failure::malformed("grant shorter than preamble"));
            return Ok(());
        }
        if grant.size > isize::MAX as u64
            || mapped
                .checked_add(grant.size)
                .is_none_or(|n| n > s.limits.max_mapped_bytes)
        {
            drop(open);
            s.fail(id, Failure::too_large("SDK mapping budget exceeded"));
            return Ok(());
        }
        let result = open(&grant).and_then(|mapping| {
            let p = mapping.read(0, 32)?;
            if u64::from_le_bytes(p[..8].try_into().unwrap()) != id
                || p[8..12] != [0; 4]
                || crate::crc32(&p[..28]) != u32::from_le_bytes(p[28..32].try_into().unwrap())
            {
                return Err(Failure::malformed(
                    "invalid segment preamble identity, padding or CRC",
                ));
            }
            Ok(mapping)
        });
        match result {
            Ok(m) => {
                s.entries.get_mut(&id).unwrap().mapping = Some(m);
                s.finish(id);
            }
            Err(e) => s.fail(id, e),
        }
        Ok(())
    }
    /// Record-local M failures are sticky even if InstallSegment has not arrived.
    pub fn reject_grant(&self, id: u64, detail: impl Into<String>) -> Result<()> {
        self.reject_grant_record(id, detail.into(), None)
    }
    pub(crate) fn reject_grant_record(
        &self,
        id: u64,
        detail: String,
        record: Option<RecordBytes>,
    ) -> Result<()> {
        let mut s = self.inner.lock().unwrap();
        s.admit(id)?;
        if s.entries[&id].released {
            s.released(id);
        } else if record
            .zip(s.entries[&id].record)
            .is_some_and(|(new, old)| new != old)
            || matches!(
                s.entries[&id].ack.as_ref().and_then(|a| a.outcome.as_ref()),
                Some(pb::grant_ack::Outcome::Installed(_))
            )
        {
            s.conflict(id);
        } else if s.entries[&id].ack.is_some() {
            s.replay(id);
        } else {
            s.entries.get_mut(&id).unwrap().record = record;
            s.fail(id, Failure::malformed(detail));
        }
        Ok(())
    }
    pub fn query(&self, id: u64) -> pb::GrantState {
        let s = self.inner.lock().unwrap();
        let mut out = pb::GrantState {
            segment_id: id,
            state: 1,
            ..Default::default()
        };
        if let Some(e) = s.entries.get(&id) {
            if e.released {
                out.state = 5;
            } else if let Some(ack) = &e.ack {
                match ack.outcome.as_ref().unwrap() {
                    pb::grant_ack::Outcome::Installed(v) => {
                        out.state = 3;
                        out.identity_proof = Some(v.identity_proof.clone());
                    }
                    pb::grant_ack::Outcome::Failed(v) => {
                        out.state = 4;
                        out.failure_class = Some(v.failure_class);
                        out.detail = Some(v.detail.clone());
                    }
                }
            } else {
                out.state = 2;
                out.have_capability = Some(e.record.is_some());
                out.have_install = Some(e.install.is_some());
            }
        }
        out
    }
    pub fn retire(&self, id: u64) -> Result<()> {
        let mut s = self.inner.lock().unwrap();
        s.admit(id)?;
        s.retire(id);
        Ok(())
    }
    pub fn request_release(&self, id: u64, reason: impl Into<String>) -> Result<()> {
        let mut s = self.inner.lock().unwrap();
        let e = s.live(id)?;
        if !e.release_requested {
            e.release_requested = true;
            s.emit(Msg::ReleaseRequest(pb::ReleaseRequest {
                segment_id: id,
                reason: reason.into(),
            }));
        }
        Ok(())
    }
    pub fn close(&self) {
        let mut s = self.inner.lock().unwrap();
        s.closed = true;
        s.entries.clear();
        s.deposits.clear();
        s.events.clear();
        s.shortfalls.clear();
    }
    pub fn installed(&self) -> Vec<pb::InstallSegment> {
        self.inner
            .lock()
            .unwrap()
            .entries
            .values()
            .filter(|e| {
                !e.retiring
                    && e.mapping.is_some()
                    && matches!(
                        e.ack.as_ref().and_then(|a| a.outcome.as_ref()),
                        Some(pb::grant_ack::Outcome::Installed(_))
                    )
            })
            .filter_map(|e| e.install.clone())
            .collect()
    }
    /// Constant reads have no handoff. Returned bytes are independent of retirement.
    pub fn read_constant(&self, id: u64, region: u32) -> Result<Payload> {
        let mut s = self.inner.lock().unwrap();
        let e = s.readable(id)?;
        let r = region_at(e, region, 1, None)?.clone();
        let bytes = e.mapping.as_ref().unwrap().read(r.offset, r.extent)?;
        Ok(payload(bytes, r.epoch_offset))
    }
    /// The embedding application calls this only when its OWNED exchange has
    /// transferred this range to the plugin. This SDK does not infer that transfer.
    pub fn authorize_owned(
        &self,
        id: u64,
        region: u32,
        offset: u64,
        len: u64,
    ) -> Result<OwnedAccess> {
        let mut s = self.inner.lock().unwrap();
        s.begin_use(id)?;
        let check = (|| {
            let e = s.readable(id)?;
            let r = region_at(e, region, 2, None)?;
            within(r, offset, len)
        })();
        if let Err(error) = check {
            s.end_use(id);
            return Err(error);
        }
        Ok(OwnedAccess {
            memory: self.clone(),
            id,
            region,
            offset,
            len,
        })
    }
    /// Copy a host-produced deposit and account one use. The release is queued
    /// even when shape validation fails; no borrowed data survives the return.
    pub fn copy_deposit(&self, d: &pb::SegmentRef) -> Result<Payload> {
        self.copy_deposit_inner(d, false, false)
    }
    pub(crate) fn copy_deposit_bytes(&self, d: &pb::SegmentRef) -> Result<Payload> {
        self.copy_deposit_inner(d, true, false)
    }
    pub(crate) fn pin_deposit(&self, d: &pb::SegmentRef) -> Result<DepositLease> {
        let mut s = self.inner.lock().unwrap();
        s.begin_use(d.segment_id)?;
        if let Err(error) = find_deposit(s.readable(d.segment_id)?, d) {
            s.end_use(d.segment_id);
            return Err(error);
        }
        Ok(DepositLease {
            memory: self.clone(),
            id: d.segment_id,
        })
    }
    fn copy_deposit_inner(&self, d: &pb::SegmentRef, raw: bool, pinned: bool) -> Result<Payload> {
        let mut s = self.inner.lock().unwrap();
        if !pinned {
            s.begin_use(d.segment_id)?;
        }
        let result = (|| {
            let e = s.readable(d.segment_id)?;
            let r = find_deposit(e, d)?.clone();
            let result = (if raw { Ok(()) } else { validate_descriptor(d) })
                .and_then(|_| e.mapping.as_ref().unwrap().read(d.offset, d.len))
                .map(|v| payload(v, r.epoch_offset));
            s.dispose(d, &r);
            result
        })();
        if !pinned {
            s.end_use(d.segment_id);
        }
        result
    }

    /// Abandoned replies must return their reservation without reading the bytes.
    pub fn discard_deposit(&self, d: &pb::SegmentRef) -> Result<()> {
        let mut s = self.inner.lock().unwrap();
        if s.entries.get(&d.segment_id).is_some_and(|e| e.released) {
            return Ok(());
        }
        let e = s.readable(d.segment_id)?;
        let r = find_deposit(e, d)?.clone();
        s.dispose(d, &r);
        Ok(())
    }
    /// Low-level reservation. Pair every success with publication or cancel_unsent;
    /// an unpublished reservation pins retirement. Use try_reserve_push for
    /// best-effort pushes that should quietly drop when no successor is available.
    pub fn reserve(&self, id: u64, bytes: &[u8]) -> Result<Loan> {
        self.reserve_inner(id, bytes, true)?
            .ok_or_else(|| Failure::too_large("loan arena full; push refused"))
    }
    /// Follow supersession atomically. None means normal retirement without a
    /// successor or arena exhaustion (the latter records SegmentShortfall).
    pub fn try_reserve_push(&self, id: u64, bytes: &[u8]) -> Result<Option<Loan>> {
        let mut s = self.inner.lock().unwrap();
        let id = s.successor(id);
        if s.entries.get(&id).is_some_and(|e| e.retiring) {
            s.retirement_drops = s.retirement_drops.saturating_add(1);
            return Ok(None);
        }
        Self::reserve_locked(&mut s, id, bytes, true)
    }
    /// Local diagnostic counter; normal retirement is not an error log.
    pub fn retirement_drops(&self) -> u64 {
        self.inner.lock().unwrap().retirement_drops
    }
    /// Local debug counter for ReleaseLoan messages ignored after SegmentReleased.
    /// These normal retirement crossings do not emit contract violations.
    pub fn crossed_releases(&self) -> u64 {
        self.inner.lock().unwrap().crossed_releases
    }
    /// Reserve a solicited chunk without counting a shortfall. None means the
    /// arena is full or retiring without a successor: retain the answer and retry
    /// after ReleaseLoan or a replacement grant.
    pub fn try_reserve_chunk(&self, id: u64, bytes: &[u8]) -> Result<Option<Loan>> {
        let mut s = self.inner.lock().unwrap();
        let id = s.successor(id);
        if s.entries.get(&id).is_some_and(|e| e.retiring) {
            s.retirement_drops = s.retirement_drops.saturating_add(1);
            return Ok(None);
        }
        Self::reserve_locked(&mut s, id, bytes, false)
    }
    fn reserve_inner(&self, id: u64, bytes: &[u8], count_shortfall: bool) -> Result<Option<Loan>> {
        if bytes.is_empty() {
            return Err(Failure::malformed("empty loan"));
        }
        let mut s = self.inner.lock().unwrap();
        let id = s.successor(id);
        Self::reserve_locked(&mut s, id, bytes, count_shortfall)
    }
    /// Atomically choose a live arena and reserve; there is no selection/retire gap.
    pub(crate) fn reserve_for_subject(
        &self,
        purpose: &str,
        subject: &str,
        bytes: &[u8],
        count_shortfall: bool,
    ) -> Result<Option<Loan>> {
        let mut s = self.inner.lock().unwrap();
        let id = s.subject_id(purpose, subject);
        match id {
            Some(id) => {
                let loan = Self::reserve_locked(&mut s, id, bytes, count_shortfall)?;
                if count_shortfall && loan.is_none() {
                    return Err(Failure::too_large("loan arena full; push refused"));
                }
                Ok(loan)
            }
            None => {
                s.count_retired_subject(purpose, subject);
                Ok(None)
            }
        }
    }
    fn reserve_locked(
        s: &mut State,
        id: u64,
        bytes: &[u8],
        count_shortfall: bool,
    ) -> Result<Option<Loan>> {
        if bytes.is_empty() {
            return Err(Failure::malformed("empty loan"));
        }
        s.begin_use(id)?;
        let result = Self::reserve_claimed(s, id, bytes, count_shortfall);
        if !matches!(&result, Ok(Some(_))) {
            s.end_use(id);
        }
        result
    }
    fn reserve_claimed(
        s: &mut State,
        id: u64,
        bytes: &[u8],
        count_shortfall: bool,
    ) -> Result<Option<Loan>> {
        let e = s.readable(id)?;
        let r = e
            .install
            .as_ref()
            .unwrap()
            .regions
            .iter()
            .find(|r| r.discipline == 4 && r.producer == 2)
            .cloned()
            .ok_or_else(|| Failure::malformed("segment has no plugin loan arena"))?;
        if e.regions[r.index as usize].quarantined {
            return Err(Failure::malformed("region quarantined"));
        }
        let rt = &mut e.regions[r.index as usize];
        let len = bytes.len() as u64;
        let padded = len
            .checked_add(63)
            .map(|n| n & !63)
            .ok_or_else(|| Failure::too_large("loan overflow"))?;
        if rt.loans.is_empty() {
            rt.cursor = 0;
        }
        let free = r.extent - rt.cursor;
        if padded > free {
            if !count_shortfall {
                return Ok(None);
            }
            let sf = s
                .shortfalls
                .entry((id, r.index))
                .or_insert(pb::SegmentShortfall {
                    segment_id: id,
                    region: r.index,
                    ..Default::default()
                });
            if len > sf.wanted_bytes {
                sf.wanted_bytes = len;
                sf.largest_free_bytes = free;
            }
            sf.dropped = sf.dropped.saturating_add(1);
            return Ok(None);
        }
        let offset = r.offset + rt.cursor;
        e.mapping.as_mut().unwrap().write(offset, bytes)?;
        rt.cursor += padded;
        rt.loans.insert((offset, len), LoanUses::default());
        Ok(Some(Loan {
            segment_id: id,
            region: r.index,
            offset,
            len,
        }))
    }
    /// Reclaim a reservation only when no admitted message has named it.
    /// Once admitted, only ReleaseLoan or retirement may free the bytes.
    pub fn cancel_unsent(&self, loan: Loan) -> Result<()> {
        let mut s = self.inner.lock().unwrap();
        let e = s.readable(loan.segment_id)?;
        let r = region_at(e, loan.region, 4, Some(2))?;
        within(r, loan.offset, loan.len)?;
        let loans = &mut e.regions[loan.region as usize].loans;
        match loans.get(&(loan.offset, loan.len)) {
            None => return Err(Failure::malformed("cancel names no outstanding loan")),
            Some(uses) if uses.named > 0 => {
                return Err(Failure::malformed(
                    "admitted loan is the receiver's to release",
                ));
            }
            _ => {}
        }
        loans.remove(&(loan.offset, loan.len));
        s.end_use(loan.segment_id);
        Ok(())
    }
    /// Validate the whole message before committing any use. The caller holds
    /// the outbound queue lock and has already checked all admission limits.
    pub(crate) fn admit_loans(&self, descriptors: &[pb::SegmentRef]) -> Result<()> {
        let mut s = self.inner.lock().unwrap();
        let mut admitted = BTreeMap::new();
        for d in descriptors {
            let e = s.readable(d.segment_id)?;
            let r = e.install.as_ref().unwrap().regions.iter().find(|r| {
                r.discipline == 4 && r.producer == 2 && within(r, d.offset, d.len).is_ok()
            });
            let Some(r) = r else { continue };
            let key = (d.segment_id, r.index, d.offset, d.len);
            if admitted.contains_key(&key) {
                return Err(Failure::malformed("message names the same loan twice"));
            }
            let &uses_so_far = e.regions[r.index as usize]
                .loans
                .get(&(d.offset, d.len))
                .ok_or_else(|| Failure::malformed("descriptor names no outstanding loan"))?;
            let uses = d.uses.max(1);
            if uses_so_far.named > 0 && uses_so_far.declared != uses {
                return Err(Failure::malformed("loan uses disagreement"));
            }
            if uses_so_far.named >= uses {
                return Err(Failure::malformed("loan uses exceeded"));
            }
            admitted.insert(
                key,
                LoanUses {
                    declared: uses,
                    named: uses_so_far.named + 1,
                    sent: uses_so_far.sent,
                    stranded: false,
                },
            );
        }
        for &(id, region, offset, len) in admitted.keys() {
            // A retry may resume a previously stranded reservation. The
            // mapping lock keeps reacquisition atomic with retirement.
            let old = s.entries[&id].regions[region as usize].loans[&(offset, len)];
            if !old.holds_claim() && s.readable(id)?.retiring {
                return Err(Failure::malformed("segment retiring"));
            }
        }
        for ((id, region, offset, len), counts) in admitted {
            let old = s.entries[&id].regions[region as usize].loans[&(offset, len)];
            if !old.holds_claim() {
                s.begin_use(id)?;
            }
            s.entries.get_mut(&id).unwrap().regions[region as usize]
                .loans
                .insert((offset, len), counts);
        }
        Ok(())
    }
    /// Called only after the descriptor-bearing message entered the ordered
    /// transport. A release emitted here cannot overtake its publication.
    pub(crate) fn loans_sent(&self, descriptors: &[pb::SegmentRef]) {
        let mut s = self.inner.lock().unwrap();
        for d in descriptors {
            let mut drained = false;
            if let Some(e) = s.entries.get_mut(&d.segment_id) {
                for r in &mut e.regions {
                    if let Some(uses) = r.loans.get_mut(&(d.offset, d.len)) {
                        let held = uses.holds_claim();
                        uses.sent += 1;
                        drained = held && !uses.holds_claim();
                        break;
                    }
                }
            }
            if drained {
                s.end_use(d.segment_id);
            }
        }
    }

    /// An earlier message named these bytes, but a later message failed
    /// admission. Keep the bytes lent, and hold only already-admitted work.
    pub(crate) fn strand_loans(&self, descriptors: &[pb::SegmentRef]) {
        let mut s = self.inner.lock().unwrap();
        for d in descriptors {
            let mut drained = false;
            if let Some(e) = s.entries.get_mut(&d.segment_id) {
                for region in &mut e.regions {
                    if let Some(uses) = region.loans.get_mut(&(d.offset, d.len)) {
                        if uses.named != 0 {
                            let held = uses.holds_claim();
                            uses.stranded = true;
                            drained = held && !uses.holds_claim();
                        }
                        break;
                    }
                }
            }
            if drained {
                s.end_use(d.segment_id);
            }
        }
    }

    pub(crate) fn cancel_unsent_descriptor(&self, d: &pb::SegmentRef) -> Result<()> {
        let region = {
            let mut s = self.inner.lock().unwrap();
            let e = s.readable(d.segment_id)?;
            e.install
                .as_ref()
                .unwrap()
                .regions
                .iter()
                .find(|r| {
                    r.discipline == 4 && r.producer == 2 && within(r, d.offset, d.len).is_ok()
                })
                .map(|r| r.index)
                .ok_or_else(|| Failure::malformed("unsent descriptor names no arena"))?
        };
        self.cancel_unsent(Loan {
            segment_id: d.segment_id,
            region,
            offset: d.offset,
            len: d.len,
        })
    }
    pub fn release_loan(&self, release: pb::ReleaseLoan) -> Result<()> {
        let mut s = self.inner.lock().unwrap();
        // A crossing is valid only for a loan still out when we unmapped.
        if s.entries.get_mut(&release.segment_id).is_some_and(|e| {
            e.released
                && e.crossed_loans
                    .remove(&(release.region, release.offset, release.len))
        }) {
            s.crossed_releases = s.crossed_releases.saturating_add(1);
            return Ok(());
        }
        let result = (|| {
            let e = s.readable(release.segment_id).map_err(|e| (12, e))?;
            let r = e
                .install
                .as_ref()
                .unwrap()
                .regions
                .iter()
                .find(|r| r.index == release.region)
                .ok_or((18, Failure::malformed("release region missing")))?;
            if r.discipline != 4 || r.producer != 2 {
                return Err((
                    11,
                    Failure::malformed("release names wrong producer or discipline"),
                ));
            }
            within(r, release.offset, release.len).map_err(|e| (18, e))?;
            let Some(uses) = e.regions[release.region as usize]
                .loans
                .remove(&(release.offset, release.len))
            else {
                return Err((12, Failure::malformed("release names no outstanding loan")));
            };
            if uses.holds_claim() {
                s.end_use(release.segment_id);
            }
            Ok(())
        })();
        if let Err((site, e)) = result {
            s.quarantine(release.segment_id, release.region);
            s.violation(site, e.detail.clone(), Vec::new());
            return Err(e);
        }
        Ok(())
    }
    pub fn drain_events(&self) -> Result<Vec<Msg>> {
        let mut s = self.inner.lock().unwrap();
        if s.overflow {
            return Err(Failure::malformed("memory one-shot queue overflow"));
        }
        let mut out: Vec<_> = s.events.drain(..).collect();
        if s.last_report.elapsed() >= Duration::from_secs(1) {
            s.last_report = Instant::now();
            out.extend(
                std::mem::take(&mut s.violations)
                    .into_values()
                    .map(Msg::ContractViolation),
            );
            out.extend(
                std::mem::take(&mut s.shortfalls)
                    .into_values()
                    .map(Msg::SegmentShortfall),
            );
        }
        Ok(out)
    }
    /// Repeats remain pending until the next rate window. Shutdown may wait
    /// for that window, bounded by the host's deadline.
    pub(crate) fn has_pending_reports(&self) -> bool {
        let s = self.inner.lock().unwrap();
        !s.violations.is_empty() || !s.shortfalls.is_empty()
    }
    pub fn report(&self, site: i32, detail: impl Into<String>, record: Vec<u8>) {
        self.inner
            .lock()
            .unwrap()
            .violation(site, detail.into(), record);
    }
    /// Hints schedule no ownership transfer. Polling is owned by the runtime.
    pub fn notification(&self, n: &crate::Notification) {
        self.notification_record(n, Vec::new());
    }
    pub(crate) fn notification_record(&self, n: &crate::Notification, record: Vec<u8>) {
        let mut s = self.inner.lock().unwrap();
        let mut owned = Vec::new();
        for i in n.mask.indices() {
            let discipline = s
                .entries
                .get(&n.segment_id)
                .filter(|e| !e.released && !e.retiring)
                .and_then(|e| e.install.as_ref())
                .and_then(|t| t.regions.iter().find(|r| r.index as usize == i))
                .map(|r| r.discipline);
            if discipline == Some(2) {
                owned.push(i);
            }
        }
        if !owned.is_empty() {
            s.violation(
                7,
                format!("notification names OWNED regions {owned:?}"),
                record,
            );
        }
    }
}

/// Pins an already-delivered descriptor while application scheduling is pending.
/// Copying consumes the lease; abandoning HostReply disposes before dropping it.
pub(crate) struct DepositLease {
    memory: Memory,
    id: u64,
}
impl DepositLease {
    pub(crate) fn copy(self, descriptor: &pb::SegmentRef, raw: bool) -> Result<Payload> {
        self.memory.copy_deposit_inner(descriptor, raw, true)
    }
}
impl Drop for DepositLease {
    fn drop(&mut self) {
        let mut s = self.memory.inner.lock().unwrap();
        s.end_use(self.id);
    }
}

/// One explicit application authorization, consumed by one read or write.
pub struct OwnedAccess {
    memory: Memory,
    id: u64,
    region: u32,
    offset: u64,
    len: u64,
}
impl OwnedAccess {
    pub fn read(self) -> Result<Vec<u8>> {
        let mut s = self.memory.inner.lock().unwrap();
        let e = s.readable(self.id)?;
        region_at(e, self.region, 2, None)?;
        e.mapping.as_ref().unwrap().read(self.offset, self.len)
    }
    pub fn write(self, bytes: &[u8]) -> Result<()> {
        if bytes.len() as u64 != self.len {
            return Err(Failure::malformed("OWNED write length differs"));
        }
        let mut s = self.memory.inner.lock().unwrap();
        let e = s.readable(self.id)?;
        region_at(e, self.region, 2, None)?;
        e.mapping.as_mut().unwrap().write(self.offset, bytes)
    }
}
impl Drop for OwnedAccess {
    fn drop(&mut self) {
        let mut s = self.memory.inner.lock().unwrap();
        s.end_use(self.id);
    }
}
fn payload(bytes: Vec<u8>, epoch_offset: Option<u64>) -> Payload {
    let pane = epoch_offset.and_then(|o| {
        let end = o.checked_add(8)?;
        if end > bytes.len() as u64 {
            return None;
        }
        let b = &bytes[o as usize..end as usize];
        let view = u32::from_le_bytes(b[..4].try_into().unwrap());
        let epoch = u32::from_le_bytes(b[4..].try_into().unwrap());
        if view > 255 || epoch == 0 {
            None
        } else {
            Some(PaneEpoch {
                view_id: view as u8,
                epoch,
            })
        }
    });
    Payload { bytes, pane }
}
fn within(r: &pb::Region, offset: u64, len: u64) -> Result<()> {
    if len == 0
        || offset < r.offset
        || offset
            .checked_add(len)
            .is_none_or(|n| n > r.offset + r.extent)
    {
        return Err(Failure::malformed("range outside region"));
    }
    Ok(())
}
fn region_at(e: &Entry, index: u32, discipline: i32, producer: Option<i32>) -> Result<&pb::Region> {
    let r = e
        .install
        .as_ref()
        .unwrap()
        .regions
        .iter()
        .find(|r| r.index == index)
        .ok_or_else(|| Failure::malformed("unknown region"))?;
    if e.regions[index as usize].quarantined {
        return Err(Failure::malformed("region quarantined"));
    }
    if r.discipline != discipline || producer.is_some_and(|p| p != r.producer) {
        return Err(Failure::malformed("wrong discipline or producer"));
    }
    Ok(r)
}
fn find_deposit<'a>(e: &'a Entry, d: &pb::SegmentRef) -> Result<&'a pb::Region> {
    let r = e
        .install
        .as_ref()
        .unwrap()
        .regions
        .iter()
        .find(|r| within(r, d.offset, d.len).is_ok())
        .ok_or_else(|| Failure::malformed("descriptor names no region"))?;
    region_at(e, r.index, 4, Some(1))
}
pub fn dtype_bytes(dtype: i32) -> Result<u64> {
    match dtype {
        1 => Ok(1),
        2 => Ok(2),
        3 => Ok(4),
        4 => Ok(8),
        _ => Err(Failure::unsupported("unknown dtype")),
    }
}
pub fn validate_descriptor(d: &pb::SegmentRef) -> Result<()> {
    if d.shape_zyx.len() != 3 {
        return Err(Failure::malformed("shape must have three ZYX dimensions"));
    }
    let size = d.shape_zyx.iter().try_fold(dtype_bytes(d.dtype)?, |a, &n| {
        if n == 0 {
            None
        } else {
            a.checked_mul(n as u64)
        }
    });
    if size != Some(d.len) {
        return Err(Failure::malformed(
            "descriptor length does not equal shape × dtype",
        ));
    }
    Ok(())
}

impl State {
    fn admit(&mut self, id: u64) -> Result<()> {
        if self.closed {
            return Err(Failure::malformed("memory session closed"));
        }
        if id == 0 {
            self.violation(4, "zero segment identity".into(), Vec::new());
            return Err(Failure::malformed("zero segment identity"));
        }
        if !self.entries.contains_key(&id) && self.entries.len() >= self.limits.max_segment_ids {
            return Err(Failure::too_large("session segment identity limit reached"));
        }
        self.entries.entry(id).or_default();
        Ok(())
    }
    fn emit(&mut self, msg: Msg) {
        if self.events.len() == 256 {
            self.overflow = true;
        } else {
            self.events.push_back(msg);
        }
    }
    fn violation(&mut self, site: i32, detail: String, record: Vec<u8>) {
        // Finite site vocabulary and one report per site per second: switching
        // sites cannot reset this per-session bound.
        if site == 0 || pb::ViolationSite::try_from(site).is_err() {
            return;
        }
        if self.reported_sites.insert(site) {
            self.emit(Msg::ContractViolation(pb::ContractViolation {
                r#where: site,
                detail,
                record,
                count: 1,
            }));
            return;
        }
        let v = self
            .violations
            .entry(site)
            .or_insert(pb::ContractViolation {
                r#where: site,
                detail,
                record,
                count: 0,
            });
        v.count = v.count.saturating_add(1);
    }
    fn quarantine(&mut self, id: u64, region: u32) {
        if let Some(r) = self
            .entries
            .get_mut(&id)
            .and_then(|e| e.regions.get_mut(region as usize))
        {
            r.quarantined = true;
        }
    }
    fn released(&mut self, id: u64) {
        self.emit(Msg::SegmentReleased(pb::SegmentReleased { segment_id: id }));
    }
    fn replay(&mut self, id: u64) {
        if let Some(ack) = self.entries[&id].ack.clone() {
            self.emit(Msg::GrantAck(ack));
        }
    }
    fn fail(&mut self, id: u64, e: Failure) {
        let entry = self.entries.get_mut(&id).unwrap();
        entry.mapping = None;
        entry.regions.clear();
        entry.ack = Some(pb::GrantAck {
            segment_id: id,
            outcome: Some(pb::grant_ack::Outcome::Failed(pb::GrantFailed {
                failure_class: e.class,
                detail: e.detail,
            })),
        });
        self.replay(id);
    }
    fn conflict(&mut self, id: u64) {
        self.emit(Msg::GrantAck(pb::GrantAck {
            segment_id: id,
            outcome: Some(pb::grant_ack::Outcome::Failed(pb::GrantFailed {
                failure_class: 3,
                detail: "conflicting grant; original state retained".into(),
            })),
        }));
    }
    fn finish(&mut self, id: u64) {
        let e = self.entries.get_mut(&id).unwrap();
        if e.ack.is_some() || e.mapping.is_none() || e.install.is_none() {
            return;
        }
        let install = e.install.as_ref().unwrap();
        let grant = e.grant.as_ref().unwrap();
        for r in &install.regions {
            if r.offset + r.extent > grant.size
                || ((r.producer == 2 || r.discipline == 3 || r.discipline == 5)
                    && !grant.permissions.plugin_writable())
            {
                self.fail(
                    id,
                    Failure::malformed("region exceeds mapping or requires write permission"),
                );
                return;
            }
        }
        let proof = match e.mapping.as_ref().unwrap().read(12, 16) {
            Ok(p) => p,
            Err(err) => {
                self.fail(id, err);
                return;
            }
        };
        e.regions = (0..install.regions.len())
            .map(|_| Runtime {
                next_revision: 1,
                ..Default::default()
            })
            .collect();
        let supersedes = install.supersedes;
        e.ack = Some(pb::GrantAck {
            segment_id: id,
            outcome: Some(pb::grant_ack::Outcome::Installed(pb::GrantInstalled {
                identity_proof: proof,
            })),
        });
        self.replay(id);
        if supersedes != 0 {
            if self.admit(supersedes).is_ok() {
                self.retire(supersedes);
            } else {
                self.overflow = true;
            }
        }
    }
    fn subject_id(&self, purpose: &str, subject: &str) -> Option<u64> {
        self.entries
            .iter()
            .rev()
            .find(|(_, e)| {
                !e.retiring
                    && !e.released
                    && e.mapping.is_some()
                    && matches!(
                        e.ack.as_ref().and_then(|a| a.outcome.as_ref()),
                        Some(pb::grant_ack::Outcome::Installed(_))
                    )
                    && e.install
                        .as_ref()
                        .is_some_and(|i| i.purpose == purpose && i.subject == subject)
            })
            .map(|(&id, _)| id)
    }
    fn count_retired_subject(&mut self, purpose: &str, subject: &str) {
        if self.entries.values().any(|e| {
            e.retiring
                && e.install
                    .as_ref()
                    .is_some_and(|i| i.purpose == purpose && i.subject == subject)
        }) {
            self.retirement_drops = self.retirement_drops.saturating_add(1);
        }
    }
    // The check and claim acquisition share the unmap mutex. Take the count
    // before the authoritative retiring check; roll it back on refusal.
    fn begin_use(&mut self, id: u64) -> Result<()> {
        let e = self.readable(id)?;
        e.in_flight += 1;
        if e.retiring {
            self.end_use(id);
            return Err(Failure::malformed("segment retiring"));
        }
        Ok(())
    }
    fn end_use(&mut self, id: u64) {
        if let Some(e) = self.entries.get_mut(&id) {
            debug_assert!(e.in_flight > 0);
            e.in_flight -= 1;
            self.settle_retirement(id);
        }
    }
    fn retire(&mut self, id: u64) {
        let e = self.entries.get_mut(&id).unwrap();
        if e.released {
            self.released(id); // Explicit repeated retirement replays its receipt.
            return;
        }
        e.retiring = true;
        self.settle_retirement(id);
    }
    fn settle_retirement(&mut self, id: u64) {
        let e = self.entries.get_mut(&id).unwrap();
        if !e.retiring || e.released || e.in_flight != 0 {
            return;
        }
        e.mapping = None;
        e.crossed_loans = e
            .regions
            .iter()
            .enumerate()
            .flat_map(|(region, runtime)| {
                runtime
                    .loans
                    .keys()
                    .map(move |&(offset, len)| (region as u32, offset, len))
            })
            .collect();
        e.regions.clear();
        e.released = true;
        self.deposits.retain(|&(segment, _, _), _| segment != id);
        self.shortfalls.retain(|&(segment, _), _| segment != id);
        self.released(id);
    }
    fn successor(&self, mut id: u64) -> u64 {
        // InstallSegment forbids self-supersession; bound traversal defensively
        // against malformed cycles without letting a stale identity go elsewhere.
        for _ in 0..self.entries.len() {
            if !self.entries.get(&id).is_some_and(|e| e.retiring) {
                break;
            }
            let Some((&next, _)) = self.entries.iter().find(|(_, e)| {
                e.install.as_ref().is_some_and(|i| i.supersedes == id)
                    && matches!(
                        e.ack.as_ref().and_then(|a| a.outcome.as_ref()),
                        Some(pb::grant_ack::Outcome::Installed(_))
                    )
            }) else {
                break;
            };
            id = next;
        }
        id
    }
    fn live(&mut self, id: u64) -> Result<&mut Entry> {
        let e = self.readable(id)?;
        if e.retiring {
            return Err(Failure::malformed("segment retiring"));
        }
        Ok(e)
    }
    fn readable(&mut self, id: u64) -> Result<&mut Entry> {
        if self.closed {
            return Err(Failure::malformed("memory session closed"));
        }
        let e = self
            .entries
            .get_mut(&id)
            .ok_or_else(|| Failure::malformed("segment not present"))?;
        if e.released {
            return Err(Failure::malformed("segment released"));
        }
        match e.ack.as_ref().and_then(|a| a.outcome.as_ref()) {
            Some(pb::grant_ack::Outcome::Installed(_)) => Ok(e),
            Some(pb::grant_ack::Outcome::Failed(f)) => Err(Failure {
                class: f.failure_class,
                detail: f.detail.clone(),
            }),
            None => Err(Failure::malformed("segment still pending")),
        }
    }
    fn dispose(&mut self, d: &pb::SegmentRef, r: &pb::Region) {
        let key = (d.segment_id, d.offset, d.len);
        let count = d.uses.max(1);
        let (declared, remaining) = self.deposits.entry(key).or_insert((count, count));
        let disagreement = *declared != count;
        *remaining -= 1;
        let done = *remaining == 0;
        if disagreement {
            self.violation(
                24,
                "descriptor uses disagree; first count retained".into(),
                Vec::new(),
            );
        }
        if done {
            self.deposits.remove(&key);
            self.emit(Msg::ReleaseDeposit(pb::ReleaseDeposit {
                segment_id: d.segment_id,
                region: r.index,
                offset: d.offset,
                len: d.len,
            }));
        }
    }
}
fn validate_install(
    i: &pb::InstallSegment,
    subjects: Option<&(BTreeSet<String>, BTreeSet<String>)>,
) -> Result<()> {
    if i.purpose.is_empty() {
        return Err(Failure::malformed("purpose missing"));
    }
    if !matches!(i.purpose.as_str(), "G" | "V" | "O" | "P" | "X" | "S") {
        return Err(Failure::unsupported("unknown purpose"));
    }
    if i.supersedes == i.segment_id {
        return Err(Failure::malformed("segment cannot supersede itself"));
    }
    if matches!(i.purpose.as_str(), "O" | "P") {
        if i.subject.is_empty() {
            return Err(Failure::malformed("contribution subject required"));
        }
        if let Some((overlays, panels)) = subjects {
            if !if i.purpose == "O" { overlays } else { panels }.contains(&i.subject) {
                return Err(Failure::malformed("subject not declared in manifest"));
            }
        }
    } else if !i.subject.is_empty() {
        return Err(Failure::malformed("exchange subject must be empty"));
    }
    if i.regions.len() > 512 {
        return Err(Failure::malformed("more than 512 regions"));
    }
    if i.purpose == "S" && i.regions.is_empty() {
        return Err(Failure::malformed("stage ring must have at least one slot"));
    }
    let mut ranges = Vec::new();
    let mut arenas = 0;
    let mut lanes = 0;
    let mut indices = BTreeSet::new();
    for r in &i.regions {
        if r.index as usize >= i.regions.len()
            || !indices.insert(r.index)
            || r.offset < 32
            || r.extent == 0
            || r.offset.checked_add(r.extent).is_none()
        {
            return Err(Failure::malformed("invalid region index or extent"));
        }
        if r.discipline == 0 || r.producer == 0 {
            return Err(Failure::malformed("discipline and producer required"));
        }
        if !(1..=5).contains(&r.discipline) || !(1..=2).contains(&r.producer) {
            return Err(Failure::unsupported(
                "unknown region discipline or producer",
            ));
        }
        if (i.purpose == "S") != (r.discipline == 5)
            || r.discipline == 5
                && (r.producer != 1
                    || r.offset % 64 != 0
                    || r.extent < 256
                    || r.slot_bytes != 0
                    || r.epoch_offset.is_some())
        {
            return Err(Failure::malformed("invalid STAGED slot layout"));
        }
        // §8's receiver matrix checks the producer enum and write access.
        // CONSTANT/OWNED's initial HOST producer is a sender rule, not an
        // additional reason for a receiver to refuse the grant.
        if r.discipline == 3 {
            if r.buffers < 3
                || r.offset % 8 != 0
                || r.slot_bytes == 0
                || r.slot_bytes
                    .checked_add(16)
                    .and_then(|n| n.checked_mul(r.buffers as u64))
                    != Some(r.extent)
            {
                return Err(Failure::malformed("invalid Pulsed layout"));
            }
            if let Some(o) = r.epoch_offset {
                if r.slot_bytes <= 8 || o != r.slot_bytes - 8 {
                    return Err(Failure::malformed(
                        "Pulsed epoch header must be at slot end",
                    ));
                }
            }
        } else {
            if r.buffers != 0 {
                return Err(Failure::malformed("buffers must be zero outside Pulsed"));
            }
            if r.epoch_offset
                .is_some_and(|o| o.checked_add(8).is_none_or(|n| n > r.extent))
            {
                return Err(Failure::malformed("epoch header outside region"));
            }
        }
        if r.producer == 2 {
            if r.discipline == 4 {
                arenas += 1;
            }
            if r.discipline == 3 {
                lanes += 1;
            }
        }
        ranges.push((r.offset, r.offset + r.extent));
    }
    if arenas > 1 || lanes > 1 {
        return Err(Failure::malformed(
            "multiple plugin arenas or lanes in one segment",
        ));
    }
    ranges.sort_unstable();
    if ranges.windows(2).any(|r| r[0].1 > r[1].0) {
        return Err(Failure::malformed("regions overlap"));
    }
    Ok(())
}

mod pulsed;

mod staged;
pub(crate) use staged::Turn;

#[cfg(all(test, target_os = "linux"))]
mod retirement_tests;
