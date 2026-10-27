use super::*;
use std::{
    fs::File,
    os::{fd::FromRawFd, unix::fs::FileExt},
    sync::{mpsc, Barrier},
    thread,
};

fn arena(purpose: &str, discipline: i32, producer: i32) -> (Memory, File) {
    let fd = unsafe {
        libc::memfd_create(
            c"retirement".as_ptr(),
            libc::MFD_CLOEXEC | libc::MFD_ALLOW_SEALING,
        )
    };
    assert!(fd >= 0);
    let file = unsafe { File::from_raw_fd(fd) };
    file.set_len(512).unwrap();
    let mut preamble = [0; 32];
    preamble[..8].copy_from_slice(&1_u64.to_le_bytes());
    let crc = crate::crc32(&preamble[..28]);
    preamble[28..].copy_from_slice(&crc.to_le_bytes());
    file.write_all_at(&preamble, 0).unwrap();
    assert_eq!(
        unsafe {
            libc::fcntl(
                fd,
                libc::F_ADD_SEALS,
                libc::F_SEAL_GROW | libc::F_SEAL_SHRINK,
            )
        },
        0
    );
    let memory = Memory::default();
    let grant = MemoryGrant {
        segment_id: 1,
        size: 512,
        permissions: crate::Permissions::new(15).unwrap(),
    };
    memory
        .grant(
            grant.clone(),
            grant.encode(1).unwrap(),
            file.try_clone().unwrap().into(),
        )
        .unwrap();
    memory
        .install(pb::InstallSegment {
            segment_id: 1,
            purpose: purpose.into(),
            subject: if purpose == "O" {
                "overlay".into()
            } else {
                String::new()
            },
            regions: vec![pb::Region {
                offset: 64,
                extent: 256,
                discipline,
                producer,
                ..Default::default()
            }],
            ..Default::default()
        })
        .unwrap();
    memory.drain_events().unwrap();
    (memory, file)
}

fn park_copy(
    memory: &Memory,
    work: impl FnOnce(Memory) + Send + 'static,
) -> (thread::JoinHandle<()>, thread::JoinHandle<()>, Arc<Barrier>) {
    let (entered, wait) = mpsc::channel();
    let resume = Arc::new(Barrier::new(2));
    let barrier = resume.clone();
    let m = memory.clone();
    let copying = thread::spawn(move || {
        mapping::BEFORE_COPY.with(|h| {
            *h.borrow_mut() = Some(Box::new(move || {
                entered.send(()).unwrap();
                barrier.wait();
            }))
        });
        work(m);
    });
    wait.recv_timeout(Duration::from_secs(5)).unwrap();
    // The mapping lock is demonstrably held at the raw copy boundary.
    assert!(matches!(
        memory.inner.try_lock(),
        Err(std::sync::TryLockError::WouldBlock)
    ));
    let m = memory.clone();
    let (attempt, started) = mpsc::channel();
    let retiring = thread::spawn(move || {
        attempt.send(()).unwrap();
        m.retire(1).unwrap();
    });
    started.recv_timeout(Duration::from_secs(5)).unwrap();
    (copying, retiring, resume)
}

#[test]
fn overlay_retirement_waits_for_parked_copy_and_all_publications() {
    let (m, file) = arena("O", 4, 2);
    let (tx, rx) = mpsc::channel();
    let (copying, retiring, resume) = park_copy(&m, move |m| {
        tx.send(m.reserve(1, b"pixels").unwrap()).unwrap();
    });
    resume.wait();
    copying.join().unwrap();
    retiring.join().unwrap();
    let loan = rx.recv().unwrap();
    assert_eq!(m.query(1).state, 3);
    assert!(m.drain_events().unwrap().is_empty());
    let d = pb::SegmentRef {
        segment_id: 1,
        offset: loan.offset,
        len: loan.len,
        uses: 2,
        ..Default::default()
    };
    m.admit_loans(std::slice::from_ref(&d)).unwrap();
    m.loans_sent(std::slice::from_ref(&d));
    assert!(m.drain_events().unwrap().is_empty());
    m.admit_loans(std::slice::from_ref(&d)).unwrap();
    assert!(m.drain_events().unwrap().is_empty());
    let mut bytes = [0; 6];
    file.read_exact_at(&mut bytes, loan.offset).unwrap();
    assert_eq!(&bytes, b"pixels");
    m.loans_sent(&[d]);
    assert!(
        matches!(m.drain_events().unwrap().as_slice(), [Msg::SegmentReleased(r)] if r.segment_id == 1)
    );
}

#[test]
fn deposit_copy_finishes_and_disposes_before_unmap() {
    let (m, file) = arena("V", 4, 1);
    file.write_all_at(b"data", 64).unwrap();
    let (copying, retiring, resume) = park_copy(&m, |m| {
        let d = pb::SegmentRef {
            segment_id: 1,
            offset: 64,
            len: 4,
            dtype: 1,
            shape_zyx: vec![1, 1, 4],
            uses: 1,
        };
        assert_eq!(m.copy_deposit(&d).unwrap().bytes, b"data");
    });
    resume.wait();
    copying.join().unwrap();
    retiring.join().unwrap();
    assert!(matches!(
        m.drain_events().unwrap().as_slice(),
        [Msg::ReleaseDeposit(_), Msg::SegmentReleased(_)]
    ));
}

#[test]
fn staged_turn_finishes_after_retirement_before_release() {
    let (m, file) = arena("S", 5, 1);
    let word = (1_u64 << 48) | (1_u64 << 32) | 1;
    file.write_all_at(&word.to_le_bytes(), 64).unwrap();
    let mut turn = m.staged_turn(1, 0, 1, 1).unwrap().unwrap();
    m.retire(1).unwrap();
    assert!(m.drain_events().unwrap().is_empty());
    assert!(m.staged_turn(1, 0, 1, 1).unwrap().is_none());
    assert_eq!(m.retirement_drops(), 1);
    turn.write(32, b"output").unwrap();
    assert_eq!(turn.read(32, 6).unwrap(), b"output");
    turn.finish(1, 0).unwrap();
    drop(turn);
    assert!(matches!(
        m.drain_events().unwrap().as_slice(),
        [Msg::SegmentReleased(_)]
    ));
}

#[test]
fn concurrent_claim_end_and_settlement_emit_one_release() {
    let (m, _file) = arena("O", 4, 2);
    let a = m.reserve(1, b"a").unwrap();
    let b = m.reserve(1, b"b").unwrap();
    assert_eq!(m.inner.lock().unwrap().entries[&1].in_flight, 2);
    m.retire(1).unwrap();
    let go = Arc::new(Barrier::new(3));
    thread::scope(|scope| {
        for loan in [a, b] {
            let m = m.clone();
            let go = go.clone();
            scope.spawn(move || {
                go.wait();
                m.cancel_unsent(loan).unwrap();
            });
        }
        go.wait();
        m.inner.lock().unwrap().settle_retirement(1);
    });
    m.inner.lock().unwrap().settle_retirement(1);
    assert!(matches!(
        m.drain_events().unwrap().as_slice(),
        [Msg::SegmentReleased(_)]
    ));
    // Replay is reserved for a new explicit RetireSegment, not settlement.
    m.retire(1).unwrap();
    assert!(matches!(
        m.drain_events().unwrap().as_slice(),
        [Msg::SegmentReleased(_)]
    ));
}

#[test]
fn failed_allocation_releases_its_claim() {
    let (m, _file) = arena("O", 4, 2);
    let loan = m.reserve(1, &[0; 256]).unwrap();
    let d = pb::SegmentRef {
        segment_id: 1,
        offset: loan.offset,
        len: loan.len,
        uses: 1,
        ..Default::default()
    };
    m.admit_loans(std::slice::from_ref(&d)).unwrap();
    m.loans_sent(&[d]);
    assert!(m.try_reserve_chunk(1, b"full").unwrap().is_none());
    assert!(m.try_reserve_push(1, b"").is_err());
    assert_eq!(m.inner.lock().unwrap().entries[&1].in_flight, 0);
    m.retire(1).unwrap();
    assert!(matches!(
        m.drain_events().unwrap().as_slice(),
        [Msg::SegmentReleased(_)]
    ));
}

#[test]
fn stranded_promised_use_does_not_pin_retirement_but_admitted_uses_do() {
    for together in [false, true] {
        let (m, _file) = arena("O", 4, 2);
        let loan = m.reserve(1, b"payload").unwrap();
        let d = pb::SegmentRef {
            segment_id: 1,
            offset: loan.offset,
            len: loan.len,
            uses: 2,
            ..Default::default()
        };
        m.admit_loans(std::slice::from_ref(&d)).unwrap();
        if together {
            m.admit_loans(std::slice::from_ref(&d)).unwrap();
        }
        m.loans_sent(std::slice::from_ref(&d));
        if !together {
            m.strand_loans(std::slice::from_ref(&d));
        }
        assert_eq!(
            m.inner.lock().unwrap().entries[&1].in_flight,
            usize::from(together)
        );
        m.retire(1).unwrap();
        if together {
            assert!(m.drain_events().unwrap().is_empty());
            m.loans_sent(std::slice::from_ref(&d));
        } else {
            // The later message never entered the queue. It cannot keep the
            // original reservation alive just because uses promised it.
            assert!(m.admit_loans(std::slice::from_ref(&d)).is_err());
        }
        assert!(matches!(
            m.drain_events().unwrap().as_slice(),
            [Msg::SegmentReleased(_)]
        ));
    }
}

#[test]
fn later_publication_reacquires_a_claim_and_host_release_does_not_double_drop() {
    let (m, _file) = arena("O", 4, 2);
    let loan = m.reserve(1, b"payload").unwrap();
    let d = pb::SegmentRef {
        segment_id: 1,
        offset: loan.offset,
        len: loan.len,
        uses: 2,
        ..Default::default()
    };
    m.admit_loans(std::slice::from_ref(&d)).unwrap();
    m.loans_sent(std::slice::from_ref(&d));
    m.strand_loans(std::slice::from_ref(&d));
    assert_eq!(m.inner.lock().unwrap().entries[&1].in_flight, 0);
    m.admit_loans(std::slice::from_ref(&d)).unwrap();
    assert_eq!(m.inner.lock().unwrap().entries[&1].in_flight, 1);
    m.loans_sent(std::slice::from_ref(&d));
    m.release_loan(pb::ReleaseLoan {
        segment_id: 1,
        region: 0,
        offset: loan.offset,
        len: loan.len,
    })
    .unwrap();
    assert_eq!(m.inner.lock().unwrap().entries[&1].in_flight, 0);
    m.retire(1).unwrap();
    assert!(matches!(
        m.drain_events().unwrap().as_slice(),
        [Msg::SegmentReleased(_)]
    ));
}

#[test]
fn crossed_return_matches_the_exact_outstanding_tuple_once() {
    for bad in ["region", "offset", "length", "unknown segment"] {
        let (m, _file) = arena("O", 4, 2);
        let loan = m.reserve(1, b"payload").unwrap();
        let d = pb::SegmentRef {
            segment_id: 1,
            offset: loan.offset,
            len: loan.len,
            uses: 1,
            ..Default::default()
        };
        m.admit_loans(std::slice::from_ref(&d)).unwrap();
        m.loans_sent(&[d]);
        m.retire(1).unwrap();
        m.drain_events().unwrap();
        let release = pb::ReleaseLoan {
            segment_id: 1,
            region: 0,
            offset: loan.offset,
            len: loan.len,
        };
        let mut wrong = release;
        match bad {
            "region" => wrong.region += 1,
            "offset" => wrong.offset += 1,
            "length" => wrong.len += 1,
            _ => wrong.segment_id += 1,
        }
        assert!(m.release_loan(wrong).is_err());
        assert_eq!(m.crossed_releases(), 0);
        assert!(matches!(m.drain_events().unwrap().as_slice(),
            [Msg::ContractViolation(v)] if v.r#where == 12));
        m.release_loan(release).unwrap();
        assert_eq!(m.crossed_releases(), 1);
        assert!(m.release_loan(release).is_err());
        assert_eq!(m.crossed_releases(), 1);
    }
}
