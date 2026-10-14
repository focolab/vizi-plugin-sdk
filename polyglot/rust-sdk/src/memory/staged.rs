use super::*;
use std::sync::atomic::Ordering::{Acquire, Release};

// No reference into shared memory escapes a memory operation. Retirement
// waits for this lease; session close invalidates subsequent operations.
pub(crate) struct Turn {
    memory: Memory,
    pub id: u64,
    pub region: pb::Region,
    pub word: u64,
    returned: bool,
}
impl Memory {
    pub(crate) fn staged_turn(
        &self,
        id: u64,
        region: u32,
        pipeline: u32,
        position: u32,
    ) -> Result<Option<Turn>> {
        let mut s = self.inner.lock().unwrap();
        if s.entries.get(&id).is_some_and(|e| e.retiring) {
            s.retirement_drops = s.retirement_drops.saturating_add(1);
            return Ok(None);
        }
        s.begin_use(id)?;
        let result = (|| {
            let e = s.readable(id)?;
            let r = region_at(e, region, 5, Some(1))?.clone();
            let rt = &mut e.regions[region as usize];
            if rt.staged_busy {
                return Ok(None);
            }
            // This is the single acquire-load that establishes the whole identity.
            let word = u64::from_le(e.mapping.as_ref().unwrap().atomic(r.offset).load(Acquire));
            let iteration = word as u32;
            if (word >> 48) != position as u64
                || ((word >> 32) & 65535) != pipeline as u64
                || iteration == 0
                || rt.staged_iterations.get(&(pipeline, position)) == Some(&iteration)
            {
                return Ok(None);
            }
            rt.staged_busy = true;
            rt.staged_iterations.insert((pipeline, position), iteration);
            Ok(Some(Turn {
                memory: self.clone(),
                id,
                region: r,
                word,
                returned: false,
            }))
        })();
        if !matches!(&result, Ok(Some(_))) {
            s.end_use(id);
        }
        result
    }
}
impl Turn {
    fn range(&self, offset: u64, len: u64) -> Result<u64> {
        if offset < 8
            || offset
                .checked_add(len)
                .is_none_or(|end| end > self.region.extent)
        {
            return Err(Failure::malformed(
                "stage access outside slot or into turn word",
            ));
        }
        Ok(self.region.offset + offset)
    }
    pub fn read(&self, offset: u64, len: u64) -> Result<Vec<u8>> {
        let offset = self.range(offset, len)?;
        let mut s = self.memory.inner.lock().unwrap();
        let e = s.readable(self.id)?;
        e.mapping.as_ref().unwrap().read(offset, len)
    }
    pub fn write(&self, offset: u64, bytes: &[u8]) -> Result<()> {
        let offset = self.range(offset, bytes.len() as u64)?;
        let mut s = self.memory.inner.lock().unwrap();
        let e = s.readable(self.id)?;
        e.mapping.as_mut().unwrap().write(offset, bytes)
    }
    pub fn finish(&mut self, outcome: u32, reason: u32) -> Result<()> {
        let mut s = self.memory.inner.lock().unwrap();
        let e = s.readable(self.id)?;
        let m = e.mapping.as_mut().unwrap();
        m.write(self.region.offset + 8, &outcome.to_le_bytes())?;
        m.write(self.region.offset + 24, &reason.to_le_bytes())?;
        if outcome == 1 {
            m.write(
                self.region.offset + 20,
                &((self.word >> 48) as u32).to_le_bytes(),
            )?;
        }
        // Successful work passes directly to the next position; a negative
        // position ends the chain at the host. Preserve pipeline/iteration.
        let position = (self.word >> 48) as i16;
        let next = if outcome == 1 {
            position + 1
        } else {
            -position
        };
        let word = (self.word & 0x0000_ffff_ffff_ffff) | (u64::from(next as u16) << 48);
        m.atomic(self.region.offset).store(word.to_le(), Release);
        self.returned = true;
        Ok(())
    }
}
impl Drop for Turn {
    fn drop(&mut self) {
        // Normal worker paths explicitly finish (and hint) even after panic.
        // An unexpected unwind must still relinquish ownership if possible.
        if !self.returned {
            let _ = self.finish(3, 2);
        }
        let mut s = self.memory.inner.lock().unwrap();
        if let Some(e) = s.entries.get_mut(&self.id) {
            if let Some(rt) = e.regions.get_mut(self.region.index as usize) {
                rt.staged_busy = false;
            }
            s.end_use(self.id);
        }
    }
}
