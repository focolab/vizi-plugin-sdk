use super::*;
use std::sync::atomic::Ordering::{AcqRel, Acquire, Release};
const WRITE: u64 = 1 << 62;
const READ: u64 = 1 << 63;
const REV: u64 = WRITE - 1;
type Operation<T> = std::result::Result<T, (i32, Failure)>;
fn states(mapping: &Mapping, r: &pb::Region) -> Operation<Vec<u64>> {
    let words: Vec<_> = (0..r.buffers)
        .map(|i| {
            mapping
                .atomic(r.offset + i as u64 * 16)
                .load(Acquire)
                .to_le()
        })
        .collect();
    if words
        .iter()
        .any(|&w| w != 0 && (w & REV == 0 || w & (WRITE | READ) == WRITE | READ))
    {
        return Err((8, Failure::malformed("impossible Pulsed state word")));
    }
    Ok(words)
}
fn cas(mapping: &Mapping, offset: u64, old: u64, new: u64) -> bool {
    mapping
        .atomic(offset)
        .compare_exchange(old.to_le(), new.to_le(), AcqRel, Acquire)
        .is_ok()
}
impl Memory {
    /// Publish one complete value. False means contention coalesced the update
    /// or this exact segment retired (counted by retirement_drops).
    /// The newest READY value is never stolen, even under sustained publishing.
    pub fn publish(
        &self,
        id: u64,
        region: u32,
        bytes: &[u8],
        pane: Option<PaneEpoch>,
    ) -> Result<bool> {
        let mut s = self.inner.lock().unwrap();
        Self::publish_locked(&mut s, id, region, bytes, pane)
    }
    fn publish_locked(
        s: &mut State,
        id: u64,
        region: u32,
        bytes: &[u8],
        pane: Option<PaneEpoch>,
    ) -> Result<bool> {
        if s.entries.get(&id).is_some_and(|e| e.retiring) {
            s.retirement_drops = s.retirement_drops.saturating_add(1);
            return Ok(false);
        }
        s.begin_use(id)?;
        let result = Self::publish_claimed(s, id, region, bytes, pane);
        s.end_use(id);
        result
    }
    pub(crate) fn publish_overlay(
        &self,
        subject: &str,
        bytes: &[u8],
        pane: Option<PaneEpoch>,
    ) -> Result<Option<(u64, u32)>> {
        let mut s = self.inner.lock().unwrap();
        let Some(id) = s.subject_id("O", subject) else {
            s.count_retired_subject("O", subject);
            return Ok(None);
        };
        let region = s.entries[&id]
            .install
            .as_ref()
            .unwrap()
            .regions
            .iter()
            .find(|r| r.discipline == 3 && r.producer == 2)
            .ok_or_else(|| Failure::malformed("overlay has no live lane"))?
            .index;
        Ok(Self::publish_locked(&mut s, id, region, bytes, pane)?.then_some((id, region)))
    }
    fn publish_claimed(
        s: &mut State,
        id: u64,
        region: u32,
        bytes: &[u8],
        pane: Option<PaneEpoch>,
    ) -> Result<bool> {
        let e = s.readable(id)?;
        let r = region_at(e, region, 3, Some(2))?.clone();
        let capacity = r.slot_bytes - if r.epoch_offset.is_some() { 8 } else { 0 };
        if bytes.is_empty() || bytes.len() as u64 > capacity {
            return Err(Failure::too_large(format!(
                "Pulsed payload {} bytes must be nonempty and fit the {capacity}-byte payload capacity of a {}-byte slot",
                bytes.len(), r.slot_bytes
            )));
        }
        if r.epoch_offset.is_some() != pane.is_some() || pane.is_some_and(|p| p.epoch == 0) {
            return Err(Failure::malformed(
                "pane header presence differs from region",
            ));
        }
        let rt = &mut e.regions[region as usize];
        let revision = rt.next_revision;
        if revision > REV {
            return Err(Failure::too_large("Pulsed revision space exhausted"));
        }
        rt.next_revision += 1;
        let result = (|| -> Operation<bool> {
            let mapping = e.mapping.as_mut().unwrap();
            for _ in 0..8 {
                let words = states(mapping, &r)?;
                let newest = words
                    .iter()
                    .copied()
                    .filter(|w| *w != 0 && *w & (WRITE | READ) == 0)
                    .max();
                let n = r.buffers as usize;
                let mut candidates: Vec<_> = (0..n)
                    .map(|i| (i + revision as usize % n) % n)
                    .filter(|&i| words[i] == 0)
                    .collect();
                let mut ready: Vec<_> = (0..n)
                    .filter(|&i| {
                        words[i] != 0 && words[i] & (WRITE | READ) == 0 && Some(words[i]) != newest
                    })
                    .collect();
                ready.sort_unstable_by_key(|&i| words[i]);
                candidates.extend(ready);
                for i in candidates {
                    let state = r.offset + i as u64 * 16;
                    if !cas(mapping, state, words[i], revision | WRITE) {
                        continue;
                    }
                    let offset = r.offset + r.buffers as u64 * 16 + i as u64 * r.slot_bytes;
                    let write = (|| {
                        mapping.write(offset, bytes)?;
                        if let (Some(o), Some(p)) = (r.epoch_offset, pane) {
                            let mut header = [0; 8];
                            header[..4].copy_from_slice(&(p.view_id as u32).to_le_bytes());
                            header[4..].copy_from_slice(&p.epoch.to_le_bytes());
                            mapping.write(offset + o, &header)?;
                        }
                        mapping.write(state + 8, &(bytes.len() as u64).to_le_bytes())
                    })();
                    if let Err(err) = write {
                        if !cas(mapping, state, revision | WRITE, 0) {
                            return Err((
                                10,
                                Failure::malformed("Pulsed write release CAS failed"),
                            ));
                        }
                        return Err((9, err));
                    }
                    mapping.atomic(state).store(revision.to_le(), Release);
                    return Ok(true);
                }
            }
            Ok(false)
        })();
        match result {
            Ok(v) => Ok(v),
            Err((site, error)) => {
                s.quarantine(id, region);
                s.violation(site, error.detail.clone(), Vec::new());
                Err(error)
            }
        }
    }
    /// Consume the newest value once. Independently call this on a finite
    /// schedule; a D notification is never a prerequisite.
    pub fn read_pulsed(&self, id: u64, region: u32) -> Result<Option<Payload>> {
        let mut s = self.inner.lock().unwrap();
        if s.entries.get(&id).is_some_and(|e| e.retiring) {
            s.retirement_drops = s.retirement_drops.saturating_add(1);
            return Ok(None);
        }
        s.begin_use(id)?;
        let result = Self::read_pulsed_claimed(&mut s, id, region);
        s.end_use(id);
        result
    }
    fn read_pulsed_claimed(s: &mut State, id: u64, region: u32) -> Result<Option<Payload>> {
        let e = s.readable(id)?;
        let r = region_at(e, region, 3, Some(1))?.clone();
        let last = e.regions[region as usize].last_seen;
        let result = (|| -> Operation<Option<(u64, Payload)>> {
            let mapping = e.mapping.as_ref().unwrap();
            for _ in 0..8 {
                let words = states(mapping, &r)?;
                let Some((i, revision)) = words
                    .into_iter()
                    .enumerate()
                    .filter(|(_, w)| *w > last && *w & (WRITE | READ) == 0)
                    .max_by_key(|(_, w)| *w)
                else {
                    return Ok(None);
                };
                let state = r.offset + i as u64 * 16;
                if !cas(mapping, state, revision, revision | READ) {
                    continue;
                }
                let offset = r.offset + r.buffers as u64 * 16 + i as u64 * r.slot_bytes;
                let read = (|| -> Result<Payload> {
                    let length =
                        u64::from_le_bytes(mapping.read(state + 8, 8)?.try_into().unwrap());
                    let capacity = r.slot_bytes - if r.epoch_offset.is_some() { 8 } else { 0 };
                    if length == 0 || length > capacity {
                        return Err(Failure::malformed("invalid Pulsed published length"));
                    }
                    let bytes = mapping.read(offset, length)?;
                    let pane = match r.epoch_offset {
                        Some(o) => payload(mapping.read(offset + o, 8)?, Some(0)).pane,
                        None => None,
                    };
                    Ok(Payload { bytes, pane })
                })();
                // Exact release even on allocation/validation failure, and no
                // clearing a word somebody else changed under our claim.
                if !cas(mapping, state, revision | READ, 0) {
                    return Err((10, Failure::malformed("Pulsed read release CAS failed")));
                }
                return read.map(|p| Some((revision, p))).map_err(|e| (9, e));
            }
            Ok(None)
        })();
        match result {
            Ok(Some((revision, p))) => {
                e.regions[region as usize].last_seen = revision;
                Ok(Some(p))
            }
            Ok(None) => Ok(None),
            Err((site, error)) => {
                s.quarantine(id, region);
                s.violation(site, error.detail.clone(), Vec::new());
                Err(error)
            }
        }
    }
}
