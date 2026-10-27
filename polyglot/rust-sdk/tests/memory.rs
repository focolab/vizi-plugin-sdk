#![cfg(target_os = "linux")]
use std::{
    fs::File,
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::fs::FileExt,
    },
    sync::atomic::{AtomicU64, Ordering},
};
use vizi_doorbell::{memory::*, proto as pb, *};

struct Peer {
    file: File,
    grant: MemoryGrant,
}
impl Peer {
    fn new(id: u64, size: u64, writable: bool) -> Self {
        let fd = unsafe {
            libc::memfd_create(
                c"rust-sdk-spec-peer".as_ptr(),
                libc::MFD_CLOEXEC | libc::MFD_ALLOW_SEALING,
            )
        };
        assert!(fd >= 0);
        let file = unsafe { File::from_raw_fd(fd) };
        file.set_len(size).unwrap();
        let mut p = [0; 32];
        p[..8].copy_from_slice(&id.to_le_bytes());
        p[12..28].copy_from_slice(b"0123456789abcdef");
        let crc = crc32(&p[..28]);
        p[28..].copy_from_slice(&crc.to_le_bytes());
        file.write_all_at(&p, 0).unwrap();
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
        Self {
            file,
            grant: MemoryGrant {
                segment_id: id,
                size,
                permissions: Permissions::new(if writable { 15 } else { 13 }).unwrap(),
            },
        }
    }
    fn send(&self, m: &Memory) {
        m.grant(
            self.grant.clone(),
            self.grant.encode(7).unwrap(),
            self.file.try_clone().unwrap().into(),
        )
        .unwrap();
    }
    fn word(&self, offset: u64) -> u64 {
        let mut b = [0; 8];
        self.file.read_exact_at(&mut b, offset).unwrap();
        u64::from_le_bytes(b)
    }
    fn set_word(&self, offset: u64, word: u64) {
        self.file.write_all_at(&word.to_le_bytes(), offset).unwrap();
    }
}
fn region(discipline: i32, producer: i32) -> pb::Region {
    pb::Region {
        index: 0,
        offset: 64,
        extent: 256,
        discipline,
        producer,
        ..Default::default()
    }
}
fn install(id: u64, r: pb::Region) -> pb::InstallSegment {
    pb::InstallSegment {
        segment_id: id,
        purpose: if r.producer == 2 { "G" } else { "V" }.into(),
        regions: vec![r],
        ..Default::default()
    }
}
fn ack(m: &Memory) -> pb::GrantAck {
    m.drain_events()
        .unwrap()
        .into_iter()
        .find_map(|m| {
            if let pb::plugin_msg::Msg::GrantAck(a) = m {
                Some(a)
            } else {
                None
            }
        })
        .unwrap()
}
fn descriptor(id: u64, offset: u64, len: u64) -> pb::SegmentRef {
    pb::SegmentRef {
        segment_id: id,
        offset,
        len,
        shape_zyx: vec![1, 1, len as u32],
        dtype: 1,
        ..Default::default()
    }
}

#[test]
fn both_arrival_orders_prove_the_mapping_and_replay() {
    for capability_first in [true, false] {
        let m = Memory::default();
        let p = Peer::new(1, 512, true);
        let i = install(1, region(4, 1));
        if capability_first {
            p.send(&m);
        } else {
            m.install(i.clone()).unwrap();
        }
        let pending = m.query(1);
        assert_eq!(pending.state, 2);
        assert_eq!(pending.have_capability, Some(capability_first));
        assert_eq!(pending.have_install, Some(!capability_first));
        assert!(m.drain_events().unwrap().is_empty());
        if capability_first {
            m.install(i.clone()).unwrap();
        } else {
            p.send(&m);
        }
        let a = ack(&m);
        assert!(
            matches!(&a.outcome,Some(pb::grant_ack::Outcome::Installed(v)) if v.identity_proof==b"0123456789abcdef")
        );
        p.send(&m);
        assert_eq!(ack(&m), a);
        m.install(i).unwrap();
        assert_eq!(ack(&m), a);
        assert_eq!(
            m.query(1).identity_proof,
            Some(b"0123456789abcdef".to_vec())
        );
    }
}
#[test]
fn failure_is_immediate_sticky_and_conflicts_never_remap() {
    let m = Memory::default();
    let mut i = install(1, region(4, 1));
    i.purpose = "future".into();
    m.install(i).unwrap();
    let a = ack(&m);
    assert!(matches!(&a.outcome,Some(pb::grant_ack::Outcome::Failed(f)) if f.failure_class==4));
    let p = Peer::new(1, 512, true);
    p.send(&m);
    assert_eq!(ack(&m), a);
    assert!(m.installed().is_empty());
    let m = Memory::default();
    let p = Peer::new(2, 512, true);
    p.send(&m);
    m.install(install(2, region(4, 1))).unwrap();
    ack(&m);
    let different = Peer::new(2, 1024, true);
    different.send(&m);
    assert!(matches!(
        ack(&m).outcome,
        Some(pb::grant_ack::Outcome::Failed(_))
    ));
    assert_eq!(m.query(2).state, 3);
}
#[test]
fn corrupt_preamble_fails_without_meaning() {
    let m = Memory::default();
    let p = Peer::new(1, 512, true);
    p.file.write_all_at(&[1], 12).unwrap();
    p.send(&m);
    assert_eq!(m.query(1).state, 4);
    let a = ack(&m);
    assert!(matches!(a.outcome,Some(pb::grant_ack::Outcome::Failed(f)) if f.failure_class==3));
}
/// The same bytes every port's suite installs: a non-zero reserved padding
/// word `[8..12)` (checksummed), and host-reserved garbage in `[32..64)` below
/// a first region at 64.
#[test]
fn reserved_preamble_padding_and_host_reserved_bytes_install() {
    for (padding, reserved) in [(true, false), (false, true), (true, true)] {
        let m = Memory::default();
        let p = Peer::new(1, 512, true);
        if padding {
            let mut head = [0; 28];
            p.file.read_exact_at(&mut head, 0).unwrap();
            head[8..12].copy_from_slice(&[0xA5; 4]);
            p.file.write_all_at(&head, 0).unwrap();
            p.file
                .write_all_at(&crc32(&head).to_le_bytes(), 28)
                .unwrap();
        }
        let garbage: Vec<u8> = (32..64u8).map(|i| i.wrapping_mul(37) ^ 0xC3).collect();
        if reserved {
            p.file.write_all_at(&garbage, 32).unwrap();
        }
        p.send(&m);
        m.install(install(1, region(4, 1))).unwrap();
        assert!(
            matches!(&ack(&m).outcome, Some(pb::grant_ack::Outcome::Installed(v)) if v.identity_proof == b"0123456789abcdef"),
            "padding={padding} reserved={reserved}"
        );
        assert_eq!(m.query(1).state, 3);
        if reserved {
            let mut after = [0; 32];
            p.file.read_exact_at(&mut after, 32).unwrap();
            assert_eq!(after.as_slice(), garbage.as_slice());
        }
    }
}
#[test]
fn validate_layout_matrix_and_reserved_loan_alignment() {
    let mut cases = Vec::new();
    let mut r = region(4, 1);
    r.offset = 0;
    cases.push(r);
    let mut r = region(4, 1);
    r.index = 1;
    cases.push(r);
    let mut r = region(4, 1);
    r.extent = 0;
    cases.push(r);
    let mut r = region(4, 1);
    r.offset = u64::MAX;
    cases.push(r);
    let mut r = region(4, 1);
    r.buffers = 3;
    cases.push(r);
    let mut r = region(4, 1);
    r.epoch_offset = Some(250);
    cases.push(r);
    cases.push(region(0, 1));
    cases.push(region(4, 0));
    for r in cases {
        let m = Memory::default();
        m.install(install(1, r)).unwrap();
        assert_eq!(m.query(1).state, 4);
    }
    for slot in [0, 3, 64, u64::MAX] {
        let m = Memory::default();
        let mut r = region(4, 1);
        r.slot_bytes = slot;
        m.install(install(1, r)).unwrap();
        assert_eq!(m.query(1).state, 2);
    }
}
#[test]
fn permissions_overlap_and_subject_checks() {
    let p = Peer::new(1, 512, false);
    let m = Memory::default();
    p.send(&m);
    m.install(install(1, region(4, 2))).unwrap();
    assert_eq!(m.query(1).state, 4);
    let m = Memory::default();
    let mut i = install(1, region(4, 1));
    let mut r = region(4, 1);
    r.index = 1;
    r.offset = 128;
    i.regions.push(r);
    m.install(i).unwrap();
    assert_eq!(m.query(1).state, 4);
    let m = Memory::default();
    let mut i = install(1, region(4, 2));
    i.purpose = "P".into();
    m.install(i.clone()).unwrap();
    assert_eq!(m.query(1).state, 4);
    i.segment_id = 2;
    i.subject = "panel".into();
    m.install(i).unwrap();
    assert_eq!(m.query(2).state, 2, "unknown manifest trusts subject names");
}
#[test]
fn supersession_waits_for_success_and_released_ids_never_revive() {
    let m = Memory::default();
    let old = Peer::new(1, 512, true);
    old.send(&m);
    m.install(install(1, region(4, 2))).unwrap();
    ack(&m);
    let mut i = install(2, region(4, 2));
    i.supersedes = 1;
    m.install(i).unwrap();
    assert_eq!(m.query(1).state, 3);
    let pending = m.reserve(1, b"hello").unwrap();
    let bad = Peer::new(2, 512, true);
    bad.file.write_all_at(&[0; 32], 0).unwrap();
    bad.send(&m);
    assert_eq!(m.query(1).state, 3);
    let mut i = install(3, region(4, 2));
    i.supersedes = 1;
    m.install(i).unwrap();
    Peer::new(3, 512, true).send(&m);
    assert_eq!(
        m.query(1).state,
        3,
        "pending publication pins the retired arena"
    );
    m.cancel_unsent(pending).unwrap();
    assert_eq!(m.query(1).state, 5);
    let next = m.reserve(1, b"x").unwrap();
    assert_eq!(next.segment_id, 3);
    m.cancel_unsent(next).unwrap();
    m.retire(99).unwrap();
    m.install(install(99, region(4, 1))).unwrap();
    Peer::new(99, 512, true).send(&m);
    assert_eq!(m.query(99).state, 5);
    assert!(
        m.drain_events()
            .unwrap()
            .iter()
            .filter(|m| matches!(m,pb::plugin_msg::Msg::SegmentReleased(r) if r.segment_id==99))
            .count()
            >= 3
    );
}
#[test]
fn returned_loans_are_unknown_even_after_retirement() {
    for state in ["unknown", "live", "retired", "superseded"] {
        let m = Memory::default();
        if state != "unknown" {
            Peer::new(1, 512, true).send(&m);
            m.install(install(1, region(4, 2))).unwrap();
            ack(&m);
            let loan = m.reserve(1, b"hello").unwrap();
            m.release_loan(pb::ReleaseLoan {
                segment_id: 1,
                region: 0,
                offset: loan.offset,
                len: loan.len,
            })
            .unwrap();
            if state == "retired" {
                m.retire(1).unwrap();
            }
        }
        Peer::new(2, 512, true).send(&m);
        let mut successor = install(2, region(4, 2));
        if state == "superseded" {
            successor.supersedes = 1;
        }
        m.install(successor).unwrap();
        ack(&m);
        assert_eq!(m.crossed_releases(), 0); // Live returns and retirement do not count.
        let release = pb::ReleaseLoan {
            segment_id: 1,
            region: 0,
            offset: 64,
            len: 5,
        };
        let result = m.release_loan(release);
        assert!(result.is_err());
        assert_eq!(m.crossed_releases(), 0);
        assert!(matches!(m.drain_events().unwrap().as_slice(),
            [pb::plugin_msg::Msg::ContractViolation(v)] if v.r#where == 12 && v.count == 1));
        assert_eq!(
            m.query(1).state,
            match state {
                "unknown" => 1,
                "live" => 3,
                _ => 5,
            }
        );
        assert_eq!(m.query(2).state, 3);
        // Reporting an old loan must not quarantine the replacement arena.
        let loan = m.reserve(2, b"still usable").unwrap();
        m.cancel_unsent(loan).unwrap();
    }
}
#[test]
fn shared_arena_never_reuses_live_bytes_and_uses_requested_release_length() {
    let m = Memory::default();
    let p = Peer::new(1, 512, true);
    p.send(&m);
    m.install(install(1, region(4, 2))).unwrap();
    ack(&m);
    let a = m.reserve(1, b"first").unwrap();
    let b = m.clone().reserve(1, b"second").unwrap();
    assert_eq!(b.offset - a.offset, 64);
    m.release_loan(pb::ReleaseLoan {
        segment_id: 1,
        region: 0,
        offset: a.offset,
        len: a.len,
    })
    .unwrap();
    let c = m.reserve(1, &[7; 128]).unwrap();
    assert!(m.reserve(1, b"no space").is_err());
    let mut bytes = [0; 6];
    p.file.read_exact_at(&mut bytes, b.offset).unwrap();
    assert_eq!(&bytes, b"second");
    m.cancel_unsent(b).unwrap();
    m.cancel_unsent(c).unwrap();
    assert_eq!(m.reserve(1, b"new").unwrap().offset, a.offset);
}
#[test]
fn bad_release_quarantines_only_the_region() {
    let m = Memory::default();
    let p = Peer::new(1, 1024, true);
    p.send(&m);
    let mut i = install(1, region(4, 2));
    i.regions.push(pb::Region {
        index: 1,
        offset: 512,
        extent: 8,
        discipline: 1,
        producer: 1,
        ..Default::default()
    });
    m.install(i).unwrap();
    ack(&m);
    let a = m.reserve(1, b"first").unwrap();
    assert!(m
        .release_loan(pb::ReleaseLoan {
            segment_id: 1,
            region: 0,
            offset: a.offset,
            len: 64
        })
        .is_err());
    assert!(m.reserve(1, b"next").is_err());
    assert!(m.read_constant(1, 1).is_ok());
}

#[test]
fn unsent_rollback_errors_do_not_report_or_quarantine() {
    let m = Memory::default();
    let p = Peer::new(1, 512, true);
    p.send(&m);
    m.install(install(1, region(4, 2))).unwrap();
    ack(&m);
    let loan = m.reserve(1, b"first").unwrap();
    let mut wrong = loan.clone();
    wrong.len += 1;
    assert!(m.cancel_unsent(wrong).is_err());
    m.cancel_unsent(loan.clone()).unwrap();
    assert!(m.cancel_unsent(loan.clone()).is_err());
    assert!(m.drain_events().unwrap().is_empty());
    let next = m.reserve(1, b"next").unwrap();
    assert_eq!(next.offset, loan.offset);
    m.release_loan(pb::ReleaseLoan {
        segment_id: next.segment_id,
        region: next.region,
        offset: next.offset,
        len: next.len,
    })
    .unwrap();
    assert!(m.drain_events().unwrap().is_empty());
}

#[test]
fn unordered_regions_use_declared_indices_for_all_operations() {
    let m = Memory::default();
    let p = Peer::new(1, 4096, true);
    p.send(&m);
    let mut i = install(1, region(1, 1));
    i.regions = [(1, 1), (2, 1), (4, 1), (4, 2), (3, 1), (3, 2)]
        .into_iter()
        .enumerate()
        .map(|(index, (discipline, producer))| {
            let mut r = if discipline == 3 {
                lane(producer)
            } else {
                region(discipline, producer)
            };
            r.index = index as u32;
            r.offset = 64 + index as u64 * 512;
            r
        })
        .rev()
        .collect();
    m.install(i.clone()).unwrap();
    assert!(matches!(
        ack(&m).outcome,
        Some(pb::grant_ack::Outcome::Installed(_))
    ));
    m.install(i).unwrap();
    assert!(matches!(
        ack(&m).outcome,
        Some(pb::grant_ack::Outcome::Installed(_))
    ));
    p.file.write_all_at(b"constant", 64).unwrap();
    assert_eq!(&m.read_constant(1, 0).unwrap().bytes[..8], b"constant");
    p.file.write_all_at(b"owned", 576).unwrap();
    assert_eq!(
        m.authorize_owned(1, 1, 576, 5).unwrap().read().unwrap(),
        b"owned"
    );
    p.file.write_all_at(b"deposit", 1088).unwrap();
    assert_eq!(
        m.copy_deposit(&descriptor(1, 1088, 7)).unwrap().bytes,
        b"deposit"
    );
    assert!(matches!(m.drain_events().unwrap().as_slice(),
        [pb::plugin_msg::Msg::ReleaseDeposit(r)] if r.region == 2));
    let loan = m.reserve(1, b"loan").unwrap();
    assert_eq!((loan.region, loan.offset), (3, 1600));
    m.release_loan(pb::ReleaseLoan {
        segment_id: 1,
        region: 3,
        offset: loan.offset,
        len: loan.len,
    })
    .unwrap();
    let loan = m.reserve(1, b"cancel").unwrap();
    m.cancel_unsent(loan).unwrap();
    p.file.write_all_at(b"pulsed", 2112 + 48).unwrap();
    p.set_word(2112 + 8, 6);
    p.set_word(2112, 1);
    assert_eq!(m.read_pulsed(1, 4).unwrap().unwrap().bytes, b"pulsed");
    assert!(m
        .publish(
            1,
            5,
            b"published",
            Some(PaneEpoch {
                view_id: 0,
                epoch: 1
            })
        )
        .unwrap());
    let mut mask = RegionMask::default();
    mask.set(1).unwrap();
    m.notification(&Notification {
        segment_id: 1,
        mask,
        pane: None,
    });
    assert!(matches!(m.drain_events().unwrap().as_slice(),
        [pb::plugin_msg::Msg::ContractViolation(v)] if v.r#where == 7));
    assert!(m.authorize_owned(1, 1, 576, 5).unwrap().read().is_ok());
}

#[test]
fn region_indices_still_reject_duplicates_gaps_and_out_of_range() {
    for indices in [[0, 0], [0, 2], [1, 2], [0, u32::MAX]] {
        let m = Memory::default();
        let mut i = install(1, region(1, 1));
        i.regions = indices
            .into_iter()
            .enumerate()
            .map(|(n, index)| {
                let mut r = region(1, 1);
                r.index = index;
                r.offset += n as u64 * 512;
                r
            })
            .collect();
        m.install(i).unwrap();
        assert!(matches!(ack(&m).outcome,
            Some(pb::grant_ack::Outcome::Failed(f)) if f.failure_class == 3));
    }
}

#[test]
fn pulsed_unaligned_slot_size_uses_declared_stride_and_header() {
    for producer in [1, 2] {
        let m = Memory::default();
        let p = Peer::new(1, 512, true);
        p.send(&m);
        let mut r = lane(producer);
        r.slot_bytes = 25;
        r.epoch_offset = Some(17);
        r.extent = 3 * (16 + 25);
        m.install(install(1, r)).unwrap();
        assert!(matches!(
            ack(&m).outcome,
            Some(pb::grant_ack::Outcome::Installed(_))
        ));
        let pane = Some(PaneEpoch {
            view_id: 2,
            epoch: 7,
        });
        if producer == 1 {
            // The second slot and its header are deliberately unaligned.
            p.file.write_all_at(b"value", 112 + 25).unwrap();
            p.set_word(112 + 25 + 17, (7u64 << 32) | 2);
            p.set_word(80 + 8, 5);
            p.set_word(80, 1);
            let payload = m.read_pulsed(1, 0).unwrap().unwrap();
            assert_eq!(payload.bytes, b"value");
            assert_eq!(payload.pane, pane);
        } else {
            assert!(m.publish(1, 0, b"value", pane).unwrap());
            let slot = (0..3).find(|i| p.word(64 + i * 16) == 1).unwrap();
            let mut bytes = [0; 5];
            p.file.read_exact_at(&mut bytes, 112 + slot * 25).unwrap();
            assert_eq!(&bytes, b"value");
            assert_eq!(p.word(112 + slot * 25 + 17), (7u64 << 32) | 2);
        }
    }
}
#[test]
fn deposit_copy_and_drop_account_all_uses_without_padding_or_offset_conversion() {
    let m = Memory::default();
    let p = Peer::new(1, 512, true);
    p.file.write_all_at(b"hello", 80).unwrap();
    p.send(&m);
    m.install(install(1, region(4, 1))).unwrap();
    ack(&m);
    let mut d = descriptor(1, 80, 5);
    d.uses = 2;
    let value = m.copy_deposit(&d).unwrap();
    assert_eq!(value.bytes, b"hello");
    assert!(m.drain_events().unwrap().is_empty());
    m.discard_deposit(&d).unwrap();
    let events = m.drain_events().unwrap();
    assert!(
        matches!(&events[0],pb::plugin_msg::Msg::ReleaseDeposit(r) if r.offset==80&&r.len==5&&r.region==0)
    );
    p.file.write_all_at(b"other", 80).unwrap();
    assert_eq!(value.bytes, b"hello", "copy cannot alias reclaimed memory");
}
#[test]
fn bad_shape_still_releases_and_bad_pane_header_keeps_payload() {
    let m = Memory::default();
    let p = Peer::new(1, 512, true);
    p.send(&m);
    let mut r = region(4, 1);
    r.epoch_offset = Some(0);
    m.install(install(1, r)).unwrap();
    ack(&m);
    p.file.write_all_at(&[255; 8], 64).unwrap();
    let d = descriptor(1, 64, 8);
    let payload = m.copy_deposit(&d).unwrap();
    assert_eq!(payload.pane, None);
    assert_eq!(payload.bytes, vec![255; 8]);
    m.drain_events().unwrap();
    let mut d = descriptor(1, 64, 8);
    d.shape_zyx = vec![1, 1, 7];
    assert!(m.copy_deposit(&d).is_err());
    assert!(m
        .drain_events()
        .unwrap()
        .iter()
        .any(|m| matches!(m, pb::plugin_msg::Msg::ReleaseDeposit(_))));
}
fn lane(producer: i32) -> pb::Region {
    pb::Region {
        index: 0,
        offset: 64,
        extent: 3 * (16 + 32),
        discipline: 3,
        producer,
        buffers: 3,
        slot_bytes: 32,
        epoch_offset: Some(24),
        ..Default::default()
    }
}
#[test]
fn pulsed_publisher_preserves_latest_and_uses_trailing_epoch_header() {
    let m = Memory::default();
    let p = Peer::new(1, 512, true);
    p.send(&m);
    m.install(install(1, lane(2))).unwrap();
    ack(&m);
    let pane = Some(PaneEpoch {
        view_id: 0,
        epoch: 7,
    });
    let oversized = m.publish(1, 0, &[0; 25], pane).unwrap_err();
    assert!(oversized.detail.contains("25 bytes"));
    assert!(oversized.detail.contains("24-byte payload capacity"));
    assert!(oversized.detail.contains("32-byte slot"));
    assert!(m.publish(1, 0, b"one", pane).unwrap());
    assert!(m.publish(1, 0, b"two", pane).unwrap());
    assert!(m.publish(1, 0, b"three", pane).unwrap());
    assert!(m.publish(1, 0, b"four", pane).unwrap());
    let states = [p.word(64), p.word(80), p.word(96)];
    assert!(states.contains(&3));
    let i = states.iter().position(|n| *n == 4).unwrap();
    assert_eq!(p.word(64 + i as u64 * 16 + 8), 4);
    let mut out = [0; 4];
    p.file
        .read_exact_at(&mut out, 64 + 48 + i as u64 * 32)
        .unwrap();
    assert_eq!(&out, b"four");
    assert_eq!(p.word(64 + 48 + i as u64 * 32 + 24), 7u64 << 32);
    m.retire(1).unwrap();
    assert!(!m.publish(1, 0, b"late", pane).unwrap());
    assert_eq!(m.retirement_drops(), 1);
}
#[test]
fn pulsed_reader_consumes_newest_once_and_quarantines_corruption() {
    let m = Memory::default();
    let p = Peer::new(1, 512, true);
    p.send(&m);
    m.install(install(1, lane(1))).unwrap();
    ack(&m);
    p.file.write_all_at(b"payload", 112).unwrap();
    p.set_word(120 + 16, 9u64 << 32); // slot's trailing header at 136
    p.set_word(72, 7);
    p.set_word(64, 1);
    let read = m.read_pulsed(1, 0).unwrap().unwrap();
    assert_eq!(read.bytes, b"payload");
    assert_eq!(
        read.pane,
        Some(PaneEpoch {
            view_id: 0,
            epoch: 9
        })
    );
    assert_eq!(p.word(64), 0);
    assert!(m.read_pulsed(1, 0).unwrap().is_none());
    p.set_word(80, (1 << 62) | (1 << 63) | 2);
    assert!(m.read_pulsed(1, 0).is_err());
    assert_eq!(p.word(80), (1 << 62) | (1 << 63) | 2);
    p.set_word(80, 0);
    assert!(m.read_pulsed(1, 0).is_err());
}
#[test]
fn invalid_pulsed_length_releases_our_claim_before_quarantine() {
    let m = Memory::default();
    let p = Peer::new(1, 512, true);
    p.send(&m);
    m.install(install(1, lane(1))).unwrap();
    ack(&m);
    p.set_word(72, 25);
    p.set_word(64, 1);
    assert!(m.read_pulsed(1, 0).is_err());
    assert_eq!(p.word(64), 0);
}
#[test]
fn concurrent_process_style_pulsed_handoff_never_tears() {
    let m = Memory::default();
    let p = Peer::new(1, 512, true);
    p.send(&m);
    let mut r = lane(2);
    r.epoch_offset = None;
    m.install(install(1, r)).unwrap();
    ack(&m);
    let ptr = unsafe {
        libc::mmap(
            std::ptr::null_mut(),
            512,
            libc::PROT_READ | libc::PROT_WRITE,
            libc::MAP_SHARED,
            p.file.as_raw_fd(),
            0,
        )
    };
    assert_ne!(ptr, libc::MAP_FAILED);
    let address = ptr as usize;
    let done = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let end = done.clone();
    let consumer = std::thread::spawn(move || {
        let p = address as *mut u8;
        let mut count = 0;
        while !end.load(Ordering::Acquire) {
            for i in 0..3 {
                let state = unsafe { &*p.add(64 + i * 16).cast::<AtomicU64>() };
                let revision = state.load(Ordering::Acquire);
                if revision == 0 || revision >> 62 != 0 {
                    continue;
                }
                if state
                    .compare_exchange(
                        revision,
                        revision | (1 << 63),
                        Ordering::AcqRel,
                        Ordering::Acquire,
                    )
                    .is_err()
                {
                    continue;
                }
                let data = unsafe { std::slice::from_raw_parts(p.add(112 + i * 32), 32) };
                assert!(data.iter().all(|b| *b == data[0]));
                assert!(state
                    .compare_exchange(revision | (1 << 63), 0, Ordering::AcqRel, Ordering::Acquire)
                    .is_ok());
                count += 1;
            }
        }
        count
    });
    for i in 0..10000 {
        m.publish(1, 0, &[i as u8; 32], None).unwrap();
    }
    done.store(true, Ordering::Release);
    assert!(consumer.join().unwrap() > 0);
    unsafe {
        libc::munmap(ptr, 512);
    }
}
use std::sync::Arc;

#[test]
fn owned_authorization_pins_retiring_mapping_and_hints_do_not_quarantine() {
    let m = Memory::default();
    let p = Peer::new(1, 512, true);
    p.send(&m);
    let mut i = install(1, region(2, 1));
    i.regions.push(pb::Region {
        index: 1,
        offset: 400,
        extent: 8,
        discipline: 1,
        producer: 1,
        ..Default::default()
    });
    m.install(i).unwrap();
    ack(&m);
    let access = m.authorize_owned(1, 0, 64, 4).unwrap();
    let mut mask = RegionMask::default();
    mask.set(0).unwrap();
    m.notification(&Notification {
        segment_id: 1,
        mask,
        pane: None,
    });
    m.retire(1).unwrap();
    assert_eq!(m.query(1).state, 3, "authorization still holds mapping");
    assert!(m.authorize_owned(1, 0, 64, 4).is_err());
    assert!(m.read_constant(1, 1).is_ok());
    assert!(!m
        .drain_events()
        .unwrap()
        .iter()
        .any(|m| matches!(m, pb::plugin_msg::Msg::SegmentReleased(_))));
    access.write(b"done").unwrap();
    assert_eq!(m.query(1).state, 5);
    let mut out = [0; 4];
    p.file.read_exact_at(&mut out, 64).unwrap();
    assert_eq!(&out, b"done");
    assert!(m
        .drain_events()
        .unwrap()
        .iter()
        .any(|m| matches!(m, pb::plugin_msg::Msg::SegmentReleased(_))));
    assert!(m.read_constant(1, 1).is_err());
}
#[test]
fn notification_counts_records_and_first_reports_are_immediate() {
    let m = Memory::default();
    let p = Peer::new(1, 1024, true);
    p.send(&m);
    let mut i = install(1, region(2, 1));
    let mut second = region(2, 1);
    second.index = 1;
    second.offset = 512;
    i.regions.push(second);
    m.install(i).unwrap();
    ack(&m);
    let mut mask = RegionMask::default();
    mask.set(0).unwrap();
    mask.set(1).unwrap();
    let n = Notification {
        segment_id: 1,
        mask,
        pane: None,
    };
    m.notification(&n);
    let events = m.drain_events().unwrap();
    assert!(
        matches!(&events[0],pb::plugin_msg::Msg::ContractViolation(v) if v.r#where==7&&v.count==1)
    );
    for _ in 0..1000 {
        m.notification(&n);
    }
    assert!(m.drain_events().unwrap().is_empty());
    assert!(m.authorize_owned(1, 0, 64, 4).is_ok());
}

#[test]
fn unsealed_backing_installs_with_a_local_diagnostic() {
    if std::env::var_os("VIZI_RUST_AUDIT_UNSEALED_CHILD").is_none() {
        let child = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "unsealed_backing_installs_with_a_local_diagnostic",
                "--nocapture",
            ])
            .env("VIZI_RUST_AUDIT_UNSEALED_CHILD", "1")
            .output()
            .unwrap();
        assert!(
            child.status.success(),
            "{}",
            String::from_utf8_lossy(&child.stderr)
        );
        let stderr = String::from_utf8(child.stderr).unwrap();
        assert_eq!(
            stderr
                .matches("Linux grant 1 lacks shrink/grow seals; continuing installation")
                .count(),
            4,
            "{stderr}"
        );
        return;
    }
    let source = Peer::new(1, 512, true);
    for regular_file in [false, true] {
        for capability_first in [false, true] {
            let file = if regular_file {
                tempfile::tempfile().unwrap()
            } else {
                let fd = unsafe {
                    libc::memfd_create(c"unsealed-spec-peer".as_ptr(), libc::MFD_CLOEXEC)
                };
                assert!(fd >= 0);
                unsafe { File::from_raw_fd(fd) }
            };
            file.set_len(512).unwrap();
            let mut preamble = [0; 32];
            source.file.read_exact_at(&mut preamble, 0).unwrap();
            file.write_all_at(&preamble, 0).unwrap();
            file.write_all_at(b"data", 64).unwrap();
            let m = Memory::default();
            if !capability_first {
                m.install(install(1, region(1, 1))).unwrap();
            }
            m.grant(
                source.grant.clone(),
                source.grant.encode(7).unwrap(),
                file.into(),
            )
            .unwrap();
            if capability_first {
                m.install(install(1, region(1, 1))).unwrap();
            }
            assert!(matches!(
                ack(&m).outcome,
                Some(pb::grant_ack::Outcome::Installed(_))
            ));
            assert_eq!(m.query(1).state, 3);
            assert_eq!(&m.read_constant(1, 0).unwrap().bytes[..4], b"data");
        }
    }
}

#[test]
fn wrong_sized_backing_is_still_refused() {
    let source = Peer::new(1, 512, true);
    let file = tempfile::tempfile().unwrap();
    file.set_len(1024).unwrap();
    let m = Memory::default();
    m.grant(
        source.grant.clone(),
        source.grant.encode(7).unwrap(),
        file.into(),
    )
    .unwrap();
    assert!(
        matches!(ack(&m).outcome, Some(pb::grant_ack::Outcome::Failed(f)) if f.failure_class == 3)
    );
    assert_eq!(m.query(1).state, 4);
}

#[test]
fn constant_and_owned_apply_only_the_receiver_producer_matrix() {
    for discipline in [1, 2] {
        for writable in [false, true] {
            for capability_first in [false, true] {
                let m = Memory::default();
                let p = Peer::new(1, 512, writable);
                let i = install(1, region(discipline, 2));
                if capability_first {
                    p.send(&m);
                    m.install(i).unwrap();
                } else {
                    m.install(i).unwrap();
                    p.send(&m);
                }
                let answer = ack(&m);
                if writable {
                    assert!(
                        matches!(answer.outcome, Some(pb::grant_ack::Outcome::Installed(_))),
                        "{answer:?}"
                    );
                    if discipline == 1 {
                        assert_eq!(m.read_constant(1, 0).unwrap().bytes, vec![0; 256]);
                    } else {
                        m.authorize_owned(1, 0, 64, 4)
                            .unwrap()
                            .write(b"data")
                            .unwrap();
                        let mut bytes = [0; 4];
                        p.file.read_exact_at(&mut bytes, 64).unwrap();
                        assert_eq!(&bytes, b"data");
                    }
                } else {
                    assert!(
                        matches!(answer.outcome, Some(pb::grant_ack::Outcome::Failed(f)) if f.failure_class == 3)
                    );
                }
            }
        }
    }
}

#[test]
fn staged_ring_installation_checks_layout_and_write_permission() {
    for case in [
        "valid",
        "empty",
        "alignment",
        "short",
        "producer",
        "discipline",
        "readonly",
        "epoch",
        "buffers",
        "slot_bytes",
        "subject",
    ] {
        let m = Memory::default();
        let mut i = install(1, region(5, 1));
        i.purpose = "S".into();
        i.subject.clear();
        match case {
            "empty" => i.regions.clear(),
            "alignment" => i.regions[0].offset = 65,
            "short" => i.regions[0].extent = 255,
            "producer" => i.regions[0].producer = 2,
            "discipline" => i.regions[0].discipline = 4,
            "epoch" => i.regions[0].epoch_offset = Some(0),
            "subject" => i.subject = "filter".into(),
            "buffers" => i.regions[0].buffers = 3,
            "slot_bytes" => i.regions[0].slot_bytes = 64,
            _ => {}
        }
        m.install(i).unwrap();
        Peer::new(1, 512, case != "readonly").send(&m);
        assert_eq!(
            matches!(ack(&m).outcome, Some(pb::grant_ack::Outcome::Installed(_))),
            case == "valid",
            "{case}"
        );
    }
}
