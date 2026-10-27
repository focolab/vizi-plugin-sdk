//! Independent host double: header offsets, turn packing and handoff logic
//! here are literal readings of processing_stages.md, not SDK codecs.
use super::*;
use std::{fs::File, sync::atomic::AtomicU64};
use vizi_doorbell::{
    declare::{PluginBuilder, StageSpec},
    stage::{Outcome as StageOutcome, Refusal, Slot, Stage, Voxel},
};

type Plan = dyn Fn(&pb::StageShape) -> control::Result<pb::StagePlanned> + Send + Sync;
type Process =
    dyn Fn(&Context, &pb::StageArm, &mut Slot) -> control::Result<StageOutcome> + Send + Sync;
struct Handler {
    plan: Box<Plan>,
    process: Box<Process>,
}
impl Stage for Handler {
    fn plan(
        &self,
        _: &Context,
        input: &pb::StageShape,
        _: &vizi_doorbell::stage::Params,
    ) -> control::Result<pb::StagePlanned> {
        (self.plan)(input)
    }
    fn process(
        &self,
        ctx: &Context,
        arm: &pb::StageArm,
        slot: &mut Slot,
    ) -> control::Result<StageOutcome> {
        (self.process)(ctx, arm, slot)
    }
}
async fn session(handler: Handler) -> Session {
    session_many(vec![("edit", handler)]).await
}
async fn session_many(handlers: Vec<(&str, Handler)>) -> Session {
    let mut b = PluginBuilder::new("test.stage", "Stage", "1");
    for (id, handler) in handlers {
        b.stage(StageSpec::new(id), Arc::new(handler));
    }
    let p = b.build().unwrap();
    assert_eq!(
        p.manifest().value()["contributions"]["stages"][0]["name"],
        "edit"
    );
    let mut s = Session::with_runtime(
        |bell, listener| p.into_runtime(bell, listener),
        1,
        0,
        "Hello from stage host double",
    )
    .await
    .unwrap();
    s.recv().await;
    s
}
fn input() -> pb::StageShape {
    pb::StageShape {
        channels: vec![7],
        z: 1,
        y: 1,
        x: 4,
        dtype: 1,
    }
}
fn identity_plan(i: &pb::StageShape) -> control::Result<pb::StagePlanned> {
    Ok(pb::StagePlanned {
        output: Some(i.clone()),
        modifies: i
            .channels
            .iter()
            .map(|&channel| pb::StageChannelNeed {
                channel,
                needs: vec![channel],
            })
            .collect(),
        ..Default::default()
    })
}
async fn plan(s: &mut Session, input: pb::StageShape) -> pb::StagePlanned {
    plan_stage(s, "edit", input).await
}
async fn plan_stage(s: &mut Session, id: &str, input: pb::StageShape) -> pb::StagePlanned {
    s.send(
        "plan",
        Host::PlanStage(pb::PlanStage {
            params: Default::default(),
            stage_id: id.into(),
            input: Some(input),
        }),
    )
    .await;
    let reply = s.recv().await;
    assert_eq!(reply.id, "plan");
    match reply.msg {
        Some(Plugin::StagePlanned(p)) => p,
        other => panic!("{other:?}"),
    }
}
fn arrangement(input: pb::StageShape, position: u32) -> pb::StageArm {
    pb::StageArm {
        stage_id: "edit".into(),
        revision: 1,
        segment_id: 1,
        position,
        pipeline: 37,
        output: Some(input.clone()),
        input: Some(input),
        new_channels: Default::default(),
        params: Default::default(),
    }
}
async fn arm(s: &mut Session, a: pb::StageArm) {
    let revision = a.revision;
    let pair = (a.pipeline, a.position);
    let stage_id = a.stage_id.clone();
    s.send("", Host::StageArm(a)).await;
    assert!(
        matches!(s.recv().await.msg, Some(Plugin::StageArmAck(a)) if a.stage_id == stage_id && a.revision == revision && (a.pipeline, a.position) == pair)
    );
}
struct Ring {
    file: File,
    mapping: usize,
    len: usize,
}
impl Drop for Ring {
    fn drop(&mut self) {
        unsafe {
            libc::munmap(self.mapping as *mut _, self.len);
        }
    }
}
impl Ring {
    fn new(s: &Session) -> Self {
        Self::new_id(s, 1)
    }
    fn new_id(s: &Session, id: u64) -> Self {
        let file = grant(s, id, 4096);
        let p = unsafe {
            libc::mmap(
                std::ptr::null_mut(),
                4096,
                libc::PROT_READ | libc::PROT_WRITE,
                libc::MAP_SHARED,
                file.as_raw_fd(),
                0,
            )
        };
        assert_ne!(p, libc::MAP_FAILED);
        let ring = Self {
            file,
            mapping: p as usize,
            len: 4096,
        };
        ring.word(0).store(0, Ordering::Release);
        ring.word(1).store(0, Ordering::Release);
        ring
    }
    fn word(&self, slot: u32) -> &AtomicU64 {
        unsafe { &*((self.mapping + 64 + slot as usize * 384) as *const AtomicU64) }
    }
    fn fill(&self, slot: u32) {
        // Caller has awaited a host-owned position (including negative ones).
        self.word(slot).store(0, Ordering::Release);
        let mut b = [0u8; 376]; // excludes turn; header 320 + capacity 64
        for (offset, value) in [
            (16, 9u32),
            (28, 1),
            (32, 1),
            (36, 2),
            (40, 1),
            (44, 1),
            (48, 4),
            (68, 1),
            (76, 1),
            (84, 4),
        ] {
            b[offset - 8..offset - 4].copy_from_slice(&value.to_le_bytes());
        }
        b[56 - 8..64 - 8].copy_from_slice(&64u64.to_le_bytes());
        for offset in [112, 144, 176] {
            b[offset - 8..offset].copy_from_slice(&1f64.to_le_bytes());
        }
        b[208 - 8..216 - 8].copy_from_slice(&41u64.to_le_bytes());
        b[256 - 8..258 - 8].copy_from_slice(&7u16.to_le_bytes());
        b[320 - 8..324 - 8].copy_from_slice(&[1, 2, 3, 4]);
        self.file
            .write_all_at(&b, 64 + slot as u64 * 384 + 8)
            .unwrap();
    }
    fn hand(&self, slot: u32, iteration: u32, pipeline: u32, position: u32) {
        self.word(slot).store(
            (u64::from(iteration) | u64::from(pipeline) << 32 | u64::from(position) << 48).to_le(),
            Ordering::Release,
        );
    }
    async fn returned(&self, slot: u32, iteration: u32) {
        // Single stage at position 1: either success (2) or failure/refusal (-1).
        self.wait_position(slot, iteration, 37, &[2, -1]).await;
        let position = (u64::from_le(self.word(slot).load(Ordering::Acquire)) >> 48) as i16;
        assert_eq!(position, if self.u32(slot, 8) == 1 { 2 } else { -1 });
    }
    async fn wait_position(&self, slot: u32, iteration: u32, pipeline: u32, positions: &[i16]) {
        tokio::time::timeout(Duration::from_secs(3), async {
            loop {
                let w = u64::from_le(self.word(slot).load(Ordering::Acquire));
                if positions.contains(&((w >> 48) as i16)) {
                    assert_eq!(
                        w & 0x0000_ffff_ffff_ffff,
                        u64::from(iteration) | u64::from(pipeline) << 32
                    );
                    return;
                }
                tokio::time::sleep(Duration::from_millis(1)).await;
            }
        })
        .await
        .expect("stage handoff deadline");
    }
    fn u32(&self, slot: u32, offset: u64) -> u32 {
        let mut b = [0; 4];
        self.file
            .read_exact_at(&mut b, 64 + 384 * u64::from(slot) + offset)
            .unwrap();
        u32::from_le_bytes(b)
    }
    fn set_u32(&self, slot: u32, offset: u64, value: u32) {
        self.file
            .write_all_at(&value.to_le_bytes(), 64 + 384 * u64::from(slot) + offset)
            .unwrap();
    }
    fn hint(&self, s: &Session, slot: u32) {
        let mut mask = RegionMask::default();
        mask.set(slot as usize).unwrap();
        let bytes = Notification {
            segment_id: 1,
            mask,
            pane: None,
        }
        .encode(50 + slot)
        .unwrap();
        assert_eq!(
            unsafe {
                libc::send(
                    s.peer.as_raw_fd(),
                    bytes.as_ptr().cast(),
                    bytes.len(),
                    libc::MSG_NOSIGNAL,
                )
            },
            112
        );
    }
}
async fn install_ring(s: &mut Session) {
    install_ring_id(s, 1).await;
}
async fn install_ring_id(s: &mut Session, id: u64) {
    s.send(
        "",
        Host::InstallSegment(pb::InstallSegment {
            segment_id: id,
            purpose: "S".into(),
            regions: (0..2)
                .map(|index| pb::Region {
                    index,
                    offset: 64 + 384 * u64::from(index),
                    extent: 384,
                    discipline: 5,
                    producer: 1,
                    ..Default::default()
                })
                .collect(),
            ..Default::default()
        }),
    )
    .await;
    assert!(
        matches!(s.recv().await.msg, Some(Plugin::GrantAck(a)) if matches!(a.outcome, Some(pb::grant_ack::Outcome::Installed(_))))
    );
}
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn stage_repacking_addition_turn_identity_and_lost_hints() {
    let calls = Arc::new(AtomicUsize::new(0));
    let count = calls.clone();
    let mut s = session(Handler {
        plan: Box::new(|i| {
            Ok(pb::StagePlanned {
                output: Some(pb::StageShape {
                    x: 2,
                    dtype: 3,
                    ..i.clone()
                }),
                modifies: vec![pb::StageChannelNeed {
                    channel: 7,
                    needs: vec![7],
                }],
                adds: vec![pb::StageNewChannel {
                    key: "extra".into(),
                    name: "Extra".into(),
                    needs: vec![7],
                }],
                reads: vec![],
            })
        }),
        process: Box::new(move |ctx, a, slot| {
            // Blocking host access here must not hold the memory/dispatcher lock.
            let _ = ctx.memory().query(1);
            count.fetch_add(1, Ordering::SeqCst);
            assert_eq!(slot.t(), 9);
            assert_eq!(slot.open_generation(), 41);
            assert_eq!(slot.read_voxel(7, [0, 0, 1])?, Voxel::U8(2));
            let source = slot.read_bytes(0, 4)?;
            slot.layout.allocation = [1, 1, 2];
            slot.layout.dtype = pb::DType::DtypeF32;
            slot.layout.channels.push(a.new_channels["extra"] as u16);
            slot.extent = [[0, 1], [0, 1], [0, 2]];
            slot.origin = [0.0, 0.0, 1.5];
            for c in [7, 19] {
                for x in 0..2 {
                    slot.write_voxel(
                        c,
                        [0, 0, x],
                        Voxel::F32(f32::from(source[x as usize + 1]) + f32::from(c)),
                    )?;
                }
            }
            Ok(StageOutcome::Processed)
        }),
    })
    .await;
    let planned = plan(&mut s, input()).await;
    let ring = Ring::new(&s);
    install_ring(&mut s).await;
    ring.fill(0);
    ring.hand(0, 1, 37, 1);
    tokio::time::sleep(Duration::from_millis(30)).await;
    assert_eq!(calls.load(Ordering::SeqCst), 0, "unarmed stage ran");
    let mut a = arrangement(input(), 1);
    a.output = planned.output;
    a.new_channels.insert("extra".into(), 19);
    arm(&mut s, a.clone()).await;
    // No D: the independent periodic poll discovers the first turn.
    ring.returned(0, 1).await;
    // Independent host reads the actual return hint from its doorbell socket.
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            let mut bytes = [0u8; 112];
            let n = unsafe {
                libc::recv(
                    s.peer.as_raw_fd(),
                    bytes.as_mut_ptr().cast(),
                    bytes.len(),
                    libc::MSG_DONTWAIT,
                )
            };
            if n == 112 {
                assert_eq!(bytes[0], b'D');
                assert_eq!(u64::from_le_bytes(bytes[8..16].try_into().unwrap()), 1);
                assert_eq!(bytes[16] & 1, 1);
                break;
            }
            assert!(
                n < 0 && std::io::Error::last_os_error().kind() == std::io::ErrorKind::WouldBlock
            );
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    assert_eq!(
        (ring.u32(0, 8), ring.u32(0, 20), ring.u32(0, 24)),
        (1, 1, 0)
    );
    assert_eq!(
        (ring.u32(0, 28), ring.u32(0, 32), ring.u32(0, 48)),
        (3, 2, 2)
    );
    let mut b = [0; 16];
    ring.file.read_exact_at(&mut b, 64 + 320).unwrap();
    assert_eq!(
        b.as_chunks::<4>()
            .0
            .iter()
            .map(|b| f32::from_le_bytes(*b))
            .collect::<Vec<_>>(),
        vec![9., 10., 21., 22.]
    );
    arm(&mut s, a).await; // Re-ack must not reset processed iteration.
    ring.hand(0, 1, 37, 1);
    ring.hint(&s, 0);
    tokio::time::sleep(Duration::from_millis(30)).await;
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    ring.hand(0, 1, 37, 0); // Host double repairs its intentional duplicate.
    for iteration in [u32::MAX, 1] {
        ring.fill(0);
        ring.hand(0, iteration, 38, 1);
        ring.hint(&s, 0);
        tokio::time::sleep(Duration::from_millis(20)).await;
        assert_ne!(u64::from_le(ring.word(0).load(Ordering::Acquire)) >> 48, 0);
        ring.hand(0, iteration, 37, 1);
        ring.hint(&s, 0);
        ring.returned(0, iteration).await;
    }
    assert_eq!(calls.load(Ordering::SeqCst), 3);
    ring.fill(1);
    ring.hand(1, 1, 37, 1);
    ring.returned(1, 1).await;
    assert_eq!(calls.load(Ordering::SeqCst), 4, "iterations are per slot");
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn stage_invalid_header_missing_input_and_callback_outcomes() {
    let calls = Arc::new(AtomicUsize::new(0));
    let count = calls.clone();
    let mut s = session(Handler {
        plan: Box::new(|i| {
            Ok(pb::StagePlanned {
                modifies: vec![pb::StageChannelNeed {
                    channel: 7,
                    needs: vec![7],
                }],
                ..identity_plan(i)?
            })
        }),
        process: Box::new(move |_, _, _| match count.fetch_add(1, Ordering::SeqCst) {
            0 => Ok(StageOutcome::Refused(Refusal::NeedsEarlierState)),
            1 => Err(control::Error::internal("test failure")),
            2 => panic!("test panic"),
            _ => Ok(StageOutcome::Processed),
        }),
    })
    .await;
    plan(&mut s, input()).await;
    let ring = Ring::new(&s);
    install_ring(&mut s).await;
    arm(&mut s, arrangement(input(), 1)).await;
    // Invalid header: never invoke application code, fail and report site 31.
    ring.fill(0);
    ring.set_u32(0, 36, 65537);
    ring.hand(0, 1, 37, 1);
    ring.returned(0, 1).await;
    assert_eq!((ring.u32(0, 8), ring.u32(0, 24)), (3, 1));
    assert!(matches!(s.recv().await.msg,Some(Plugin::ContractViolation(v)) if v.r#where==31));
    assert_eq!(calls.load(Ordering::SeqCst), 0);
    ring.fill(0);
    ring.set_u32(0, 32, 0);
    ring.hand(0, 2, 37, 1);
    ring.returned(0, 2).await;
    assert_eq!((ring.u32(0, 8), ring.u32(0, 24)), (2, 2));
    assert_eq!(calls.load(Ordering::SeqCst), 0);
    for (iteration, outcome, reason) in [(3, 2, 1), (4, 3, 2), (5, 3, 2), (6, 1, 0)] {
        ring.fill(0);
        ring.hand(0, iteration, 37, 1);
        ring.returned(0, iteration).await;
        assert_eq!((ring.u32(0, 8), ring.u32(0, 24)), (outcome, reason));
        assert_eq!(ring.u32(0, 20), if outcome == 1 { 1 } else { 0 });
    }
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn stage_deadline_is_hosts_and_retirement_waits_for_writer() {
    let (entered_tx, entered_rx) = std::sync::mpsc::channel();
    let (release_tx, release_rx) = std::sync::mpsc::channel();
    let release_rx = Mutex::new(release_rx);
    let mut s = session(Handler {
        plan: Box::new(identity_plan),
        process: Box::new(move |_, _, slot| {
            entered_tx.send(()).unwrap();
            release_rx.lock().unwrap().recv().unwrap();
            slot.write_voxel(7, [0, 0, 0], Voxel::U8(99))?;
            Ok(StageOutcome::Processed)
        }),
    })
    .await;
    plan(&mut s, input()).await;
    let ring = Ring::new(&s);
    install_ring(&mut s).await;
    arm(&mut s, arrangement(input(), 1)).await;
    ring.fill(0);
    ring.hand(0, 1, 37, 1);
    entered_rx.recv_timeout(Duration::from_secs(2)).unwrap();
    // This host's configurable deadline expires. Quarantine means do NOT steal
    // the turn or refill the slot while its stage can still be writing.
    let deadline = Duration::from_millis(20);
    tokio::time::sleep(deadline).await;
    assert_ne!(u64::from_le(ring.word(0).load(Ordering::Acquire)) >> 48, 0);
    s.send("", Host::RetireSegment(pb::RetireSegment { segment_id: 1 }))
        .await;
    s.barrier().await; // Control dispatch is not held by the blocked callback.
    assert_ne!(s.ctx().memory().query(1).state, 5);
    release_tx.send(()).unwrap();
    ring.returned(0, 1).await;
    assert!(matches!(s.recv().await.msg,Some(Plugin::SegmentReleased(r)) if r.segment_id==1));
    assert_eq!(s.ctx().memory().query(1).state, 5);
    // Discard this late result, as §7 requires. Never treat it as a success.
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn stage_bad_plans_and_revisions_are_not_applied_but_arms_are_authoritative() {
    let calls = Arc::new(AtomicUsize::new(0));
    let count = calls.clone();
    let mut s = session(Handler {
        plan: Box::new(move |i| {
            let mut p = identity_plan(i)?;
            p.output.as_mut().unwrap().x = if count.fetch_add(1, Ordering::SeqCst) == 0 {
                4
            } else {
                2
            };
            Ok(p)
        }),
        process: Box::new(|_, a, slot| {
            let x = a.output.as_ref().unwrap().x;
            slot.layout.allocation[2] = x;
            slot.extent[2][1] = x;
            Ok(StageOutcome::Processed)
        }),
    })
    .await;
    s.send(
        "bad",
        Host::PlanStage(pb::PlanStage {
            params: Default::default(),
            stage_id: "edit".into(),
            input: Some(pb::StageShape {
                channels: vec![65536],
                ..input()
            }),
        }),
    )
    .await;
    assert!(matches!(s.recv().await.msg,Some(Plugin::Error(e)) if e.kind==6));
    assert_eq!(calls.load(Ordering::SeqCst), 0);
    plan(&mut s, input()).await;
    let ring = Ring::new(&s);
    install_ring(&mut s).await;
    let a = arrangement(input(), 1);
    let mut out_of_range = a.clone();
    out_of_range.position = 32767;
    s.send("", Host::StageArm(out_of_range)).await;
    assert!(matches!(s.recv().await.msg,Some(Plugin::ContractViolation(v)) if v.r#where==32));
    arm(&mut s, a.clone()).await;
    let mut wrong = a.clone();
    wrong.output.as_mut().unwrap().x = 3;
    s.send("", Host::StageArm(wrong)).await;
    assert!(matches!(s.recv().await.msg,Some(Plugin::ContractViolation(v)) if v.r#where==19));
    plan(&mut s, input()).await; // Different output, same input: no ambiguity.
    arm(&mut s, a.clone()).await; // Duplicate arm keeps the old immutable shape.
    let mut next = a.clone();
    next.revision = 2;
    next.output.as_mut().unwrap().x = 2;
    arm(&mut s, next).await;
    s.send("", Host::StageArm(a)).await;
    assert!(matches!(s.recv().await.msg,Some(Plugin::ContractViolation(v)) if v.r#where==20));
    ring.fill(0);
    ring.hand(0, 1, 37, 1);
    ring.returned(0, 1).await;
    assert_eq!((ring.u32(0, 8), ring.u32(0, 48)), (1, 2));
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn unknown_dtype_refuses_the_plan_and_the_arm_alone() {
    let calls = Arc::new(AtomicUsize::new(0));
    let count = calls.clone();
    let mut s = session(Handler {
        plan: Box::new(move |i| {
            count.fetch_add(1, Ordering::SeqCst);
            identity_plan(i)
        }),
        process: Box::new(|_, _, _| Ok(StageOutcome::Processed)),
    })
    .await;
    for dtype in [5, 1000] {
        s.send(
            "later",
            Host::PlanStage(pb::PlanStage {
                params: Default::default(),
                stage_id: "edit".into(),
                input: Some(pb::StageShape { dtype, ..input() }),
            }),
        )
        .await;
        let reply = s.recv().await;
        assert_eq!(reply.id, "later");
        assert!(
            matches!(&reply.msg, Some(Plugin::Error(e)) if e.kind == pb::error::Kind::Unsupported as i32 && e.message.contains(&format!("dtype {dtype}"))),
            "{reply:?}"
        );
    }
    assert_eq!(calls.load(Ordering::SeqCst), 0);
    plan(&mut s, input()).await;
    let _ring = Ring::new(&s);
    install_ring(&mut s).await;
    let a = arrangement(input(), 1);
    let mut later_input = a.clone();
    later_input.input.as_mut().unwrap().dtype = 5;
    let mut later_output = a.clone();
    later_output.output.as_mut().unwrap().dtype = 1000;
    for later in [later_input, later_output] {
        s.send("", Host::StageArm(later)).await;
        // Neither acknowledged nor reported: the barrier's reply is next.
        s.barrier().await;
    }
    arm(&mut s, a).await;
    s.stop().await;
}

fn grant_same_ring(s: &Session, ring: &Ring) {
    let bytes = MemoryGrant {
        segment_id: 1,
        size: ring.len as u64,
        permissions: Permissions::new(15).unwrap(),
    }
    .encode(1)
    .unwrap();
    unsafe {
        let mut iov = libc::iovec {
            iov_base: bytes.as_ptr().cast_mut().cast(),
            iov_len: bytes.len(),
        };
        let mut control = [0usize; 8];
        let mut msg: libc::msghdr = std::mem::zeroed();
        msg.msg_iov = &mut iov;
        msg.msg_iovlen = 1;
        msg.msg_control = control.as_mut_ptr().cast();
        msg.msg_controllen = libc::CMSG_SPACE(4) as usize;
        let c = libc::CMSG_FIRSTHDR(&msg);
        (*c).cmsg_level = libc::SOL_SOCKET;
        (*c).cmsg_type = libc::SCM_RIGHTS;
        (*c).cmsg_len = libc::CMSG_LEN(4) as usize;
        std::ptr::write_unaligned(libc::CMSG_DATA(c).cast::<i32>(), ring.file.as_raw_fd());
        assert_eq!(
            libc::sendmsg(s.peer.as_raw_fd(), &msg, libc::MSG_NOSIGNAL),
            112
        );
    }
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn consecutive_sessions_share_one_ring_without_relay_and_stop_on_refusal() {
    let refuse = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let refused = refuse.clone();
    let mut first = session(Handler {
        plan: Box::new(identity_plan),
        process: Box::new(move |_, _, slot| {
            if refused.load(Ordering::SeqCst) {
                return Ok(StageOutcome::Refused(Refusal::Other));
            }
            slot.write_voxel(7, [0, 0, 0], Voxel::U8(40))?;
            Ok(StageOutcome::Processed)
        }),
    })
    .await;
    let second_calls = Arc::new(AtomicUsize::new(0));
    let count = second_calls.clone();
    let mut second = session(Handler {
        plan: Box::new(identity_plan),
        process: Box::new(move |_, _, slot| {
            count.fetch_add(1, Ordering::SeqCst);
            assert_eq!(slot.read_voxel(7, [0, 0, 0])?, Voxel::U8(40));
            slot.write_voxel(7, [0, 0, 0], Voxel::U8(42))?;
            Ok(StageOutcome::Processed)
        }),
    })
    .await;
    let planned = plan(&mut first, input()).await;
    let mut next_input = planned.output.unwrap();
    next_input.channels = input().channels;
    plan(&mut second, next_input).await;
    let ring = Ring::new(&first);
    install_ring(&mut first).await;
    grant_same_ring(&second, &ring);
    install_ring(&mut second).await;
    arm(&mut first, arrangement(input(), 1)).await;
    arm(&mut second, arrangement(input(), 2)).await;
    for iteration in [1, 2] {
        refuse.store(iteration == 2, Ordering::SeqCst);
        ring.fill(0);
        ring.hand(0, iteration, 37, 1);
        ring.hint(&first, 0);
        // Deliberately send no D to the second session. It must poll and take
        // position 2 without waiting for the host to relay either turn or hint.
        ring.wait_position(0, iteration, 37, &[if iteration == 1 { 3 } else { -1 }])
            .await;
        if iteration == 1 {
            assert_eq!((ring.u32(0, 8), ring.u32(0, 20)), (1, 2));
            let mut byte = [0];
            ring.file.read_exact_at(&mut byte, 64 + 320).unwrap();
            assert_eq!(byte, [42]);
        } else {
            assert_eq!((ring.u32(0, 8), ring.u32(0, 20)), (2, 0));
        }
    }
    assert_eq!(
        second_calls.load(Ordering::SeqCst),
        1,
        "refusal must end the chain"
    );
    first.barrier().await;
    second.barrier().await;
    first.stop().await;
    second.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn invalid_stage_headers_are_failed_without_entering_application() {
    let mut s = session(Handler {
        plan: Box::new(identity_plan),
        process: Box::new(|_, _, _| panic!("invalid header reached callback")),
    })
    .await;
    plan(&mut s, input()).await;
    let ring = Ring::new(&s);
    install_ring(&mut s).await;
    arm(&mut s, arrangement(input(), 1)).await;
    for (index, (offset, value)) in [
        (36, 65537),
        (32, 3),
        (28, 0),
        (28, 2), // Known dtype, but differs from armed input.
        (48, 5), // Fits capacity, but exceeds armed input.
        (48, u32::MAX),
        (68, 2),
        (64, 2),
        (56, u32::MAX),
        (12, 1),
    ]
    .into_iter()
    .enumerate()
    {
        ring.fill(0);
        ring.set_u32(0, offset, value);
        ring.hand(0, index as u32 + 1, 37, 1);
        ring.returned(0, index as u32 + 1).await;
        assert_eq!((ring.u32(0, 8), ring.u32(0, 24)), (3, 1), "offset {offset}");
    }
    for (index, offset) in [88, 112, 184].into_iter().enumerate() {
        ring.fill(0);
        ring.file
            .write_all_at(&f64::NAN.to_le_bytes(), 64 + offset)
            .unwrap();
        ring.hand(0, index as u32 + 20, 37, 1);
        ring.returned(0, index as u32 + 20).await;
        assert_eq!((ring.u32(0, 8), ring.u32(0, 24)), (3, 1));
    }
    ring.fill(0);
    ring.set_u32(0, 32, 2);
    ring.file
        .write_all_at(&7u16.to_le_bytes(), 64 + 258)
        .unwrap();
    ring.hand(0, 30, 37, 1);
    ring.returned(0, 30).await;
    assert_eq!(
        (ring.u32(0, 8), ring.u32(0, 24)),
        (3, 1),
        "duplicate header channel"
    );
    assert!(matches!(s.recv().await.msg,Some(Plugin::ContractViolation(v)) if v.r#where==31));
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn maximum_plane_capacity_uses_variable_header_and_channel_65535() {
    let shape = pb::StageShape {
        channels: vec![65535],
        ..input()
    };
    let mut s = session(Handler {
        plan: Box::new(identity_plan),
        process: Box::new(|_, _, slot| {
            assert_eq!(slot.plane_cap(), 65536);
            assert_eq!(slot.read_voxel(65535, [0, 0, 3])?, Voxel::U8(44));
            slot.write_voxel(65535, [0, 0, 0], Voxel::U8(55))?;
            Ok(StageOutcome::Processed)
        }),
    })
    .await;
    plan(&mut s, shape.clone()).await;
    let file = grant(&s, 1, 200000);
    let p = unsafe {
        libc::mmap(
            std::ptr::null_mut(),
            200000,
            libc::PROT_READ | libc::PROT_WRITE,
            libc::MAP_SHARED,
            file.as_raw_fd(),
            0,
        )
    };
    assert_ne!(p, libc::MAP_FAILED);
    let ring = Ring {
        file,
        mapping: p as usize,
        len: 200000,
    };
    ring.word(0).store(0, Ordering::Release);
    s.send(
        "",
        Host::InstallSegment(pb::InstallSegment {
            segment_id: 1,
            purpose: "S".into(),
            regions: vec![pb::Region {
                index: 0,
                offset: 64,
                extent: 131328 + 128, // Rounded region may exceed header + capacity.
                discipline: 5,
                producer: 1,
                ..Default::default()
            }],
            ..Default::default()
        }),
    )
    .await;
    assert!(
        matches!(s.recv().await.msg,Some(Plugin::GrantAck(a)) if matches!(a.outcome,Some(pb::grant_ack::Outcome::Installed(_))))
    );
    arm(&mut s, arrangement(shape, 1)).await;
    ring.fill(0);
    ring.set_u32(0, 36, 65536);
    ring.file
        .write_all_at(&65535u16.to_le_bytes(), 64 + 256)
        .unwrap();
    ring.file
        .write_all_at(&[11, 22, 33, 44], 64 + 131328)
        .unwrap();
    ring.hand(0, 1, 37, 1);
    ring.returned(0, 1).await;
    assert_eq!(ring.u32(0, 8), 1);
    let mut byte = [0];
    ring.file.read_exact_at(&mut byte, 64 + 131328).unwrap();
    assert_eq!(byte, [55]);
    // The region also fits a 65537-plane header, but that cap is forbidden.
    ring.set_u32(0, 36, 65537);
    ring.hand(0, 2, 37, 1);
    ring.returned(0, 2).await;
    assert_eq!((ring.u32(0, 8), ring.u32(0, 24)), (3, 1));
    assert!(matches!(s.recv().await.msg, Some(Plugin::ContractViolation(v)) if v.r#where == 31));
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn stage_can_request_host_data_and_session_end_blocks_late_writes() {
    let (waiting_tx, waiting_rx) = std::sync::mpsc::channel();
    let (release_tx, release_rx) = std::sync::mpsc::channel();
    let release_rx = Mutex::new(release_rx);
    let (finished_tx, finished_rx) = std::sync::mpsc::channel();
    let mut s = session(Handler {
        plan: Box::new(identity_plan),
        process: Box::new(move |ctx, _, slot| {
            let state = ctx.view_state_timeout(Duration::from_secs(2))?;
            assert_eq!(state.open_generation, 41);
            waiting_tx.send(()).unwrap();
            release_rx.lock().unwrap().recv().unwrap();
            finished_tx
                .send(slot.write_voxel(7, [0, 0, 0], Voxel::U8(255)).is_err())
                .unwrap();
            Ok(StageOutcome::Processed)
        }),
    })
    .await;
    plan(&mut s, input()).await;
    let ring = Ring::new(&s);
    install_ring(&mut s).await;
    arm(&mut s, arrangement(input(), 1)).await;
    ring.fill(0);
    ring.hand(0, 1, 37, 1);
    let request = s.recv().await;
    assert!(matches!(request.msg, Some(Plugin::GetViewState(_))));
    s.send(
        &request.id,
        Host::ViewState(pb::ViewState {
            open_generation: 41,
            ..Default::default()
        }),
    )
    .await;
    waiting_rx.recv_timeout(Duration::from_secs(2)).unwrap();
    s.stop().await;
    // Host takes ownership after session end and discards/refills the slot.
    ring.hand(0, 1, 37, 0);
    ring.fill(0);
    release_tx.send(()).unwrap();
    assert!(finished_rx.recv_timeout(Duration::from_secs(2)).unwrap());
    let mut byte = [0];
    ring.file.read_exact_at(&mut byte, 64 + 320).unwrap();
    assert_eq!(byte, [1]);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn rearm_during_a_callback_applies_before_acquiring_the_next_slot() {
    let (entered_tx, entered_rx) = std::sync::mpsc::channel();
    let (release_tx, release_rx) = std::sync::mpsc::channel();
    let release_rx = Mutex::new(release_rx);
    let calls = AtomicUsize::new(0);
    let mut s = session(Handler {
        plan: Box::new(identity_plan),
        process: Box::new(move |_, arm, slot| {
            if calls.fetch_add(1, Ordering::SeqCst) == 0 {
                entered_tx.send(()).unwrap();
                release_rx.lock().unwrap().recv().unwrap();
            }
            assert_eq!(arm.input.as_ref().unwrap().x, slot.layout.allocation[2]);
            Ok(StageOutcome::Processed)
        }),
    })
    .await;
    plan(&mut s, input()).await;
    let ring = Ring::new(&s);
    install_ring(&mut s).await;
    arm(&mut s, arrangement(input(), 1)).await;
    ring.fill(0);
    ring.hand(0, 1, 37, 1);
    entered_rx.recv_timeout(Duration::from_secs(2)).unwrap();
    let next_input = pb::StageShape { x: 2, ..input() };
    plan(&mut s, next_input.clone()).await;
    let mut next = arrangement(next_input, 1);
    next.revision = 2;
    arm(&mut s, next).await;
    ring.fill(1);
    ring.set_u32(1, 48, 2);
    ring.set_u32(1, 84, 2);
    ring.hand(1, 1, 37, 1);
    release_tx.send(()).unwrap();
    ring.returned(0, 1).await;
    ring.returned(1, 1).await;
    assert_eq!(ring.u32(0, 8), 1);
    assert_eq!(ring.u32(1, 8), 1);
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn one_stage_can_hold_several_positions_and_pipelines_with_independent_iterations() {
    let calls = Arc::new(Mutex::new(Vec::new()));
    let recorded = calls.clone();
    let mut s = session(Handler {
        plan: Box::new(identity_plan),
        process: Box::new(move |_, a, slot| {
            recorded.lock().unwrap().push((a.pipeline, a.position));
            let Voxel::U8(v) = slot.read_voxel(7, [0, 0, 0])? else {
                unreachable!()
            };
            slot.write_voxel(7, [0, 0, 0], Voxel::U8(v + 1))?;
            Ok(StageOutcome::Processed)
        }),
    })
    .await;
    let ring = Ring::new(&s);
    install_ring(&mut s).await;
    for (pipeline, position) in [(37, 1), (37, 2), (38, 1), (65535, 32766)] {
        plan(&mut s, input()).await;
        let mut a = arrangement(input(), position);
        a.pipeline = pipeline;
        arm(&mut s, a).await;
    }
    // Never-filled words are ignored, even at an armed position.
    ring.fill(0);
    ring.hand(0, 0, 37, 1);
    tokio::time::sleep(Duration::from_millis(30)).await;
    assert!(calls.lock().unwrap().is_empty());
    for (pipeline, position, end) in [(37, 1, 3), (38, 1, 2), (65535, 32766, 32767)] {
        ring.fill(0);
        ring.hand(0, 1, pipeline, position);
        ring.wait_position(0, 1, pipeline, &[end]).await;
        assert_eq!(ring.u32(0, 20), if pipeline == 37 { 2 } else { position });
    }
    assert_eq!(
        *calls.lock().unwrap(),
        [(37, 1), (37, 2), (38, 1), (65535, 32766)]
    );
    // Exact retransmission cannot reset this position's iteration history.
    arm(&mut s, arrangement(input(), 1)).await;
    ring.hand(0, 1, 37, 1);
    tokio::time::sleep(Duration::from_millis(30)).await;
    assert_eq!(calls.lock().unwrap().len(), 4);
    ring.hand(0, 1, 37, 0); // Repair intentional duplicate before teardown.
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn host_keeps_even_positions_and_negative_position_names_the_refusing_stage() {
    let mut s = session(Handler {
        plan: Box::new(identity_plan),
        process: Box::new(|_, a, slot| {
            if a.position == 3 {
                // The preceding result is intact, and refusal makes no writes.
                assert_eq!(slot.read_voxel(7, [0, 0, 0])?, Voxel::U8(80));
                return Ok(StageOutcome::Refused(Refusal::Other));
            }
            slot.write_voxel(7, [0, 0, 0], Voxel::U8(80))?;
            Ok(StageOutcome::Processed)
        }),
    })
    .await;
    let ring = Ring::new(&s);
    install_ring(&mut s).await;
    for position in [1, 3] {
        plan(&mut s, input()).await;
        arm(&mut s, arrangement(input(), position)).await;
    }
    ring.fill(0);
    ring.hand(0, 1, 37, 1);
    ring.wait_position(0, 1, 37, &[2]).await;
    tokio::time::sleep(Duration::from_millis(30)).await;
    assert_eq!(u64::from_le(ring.word(0).load(Ordering::Acquire)) >> 48, 2);
    assert_eq!((ring.u32(0, 8), ring.u32(0, 20)), (1, 1));
    // The host observes the even position, then hands on without resetting outcome.
    ring.hand(0, 1, 37, 3);
    ring.wait_position(0, 1, 37, &[-3]).await;
    assert_eq!(
        (ring.u32(0, 8), ring.u32(0, 24), ring.u32(0, 20)),
        (2, 3, 1)
    );
    let mut byte = [0];
    ring.file.read_exact_at(&mut byte, 64 + 320).unwrap();
    assert_eq!(byte, [80]);
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn unusable_arms_report_site_32_and_are_never_held_for_a_grant() {
    for case in [
        "ring",
        "stage",
        "position-zero",
        "position-large",
        "pipeline-zero",
        "pipeline-large",
    ] {
        let calls = Arc::new(AtomicUsize::new(0));
        let count = calls.clone();
        let mut s = session(Handler {
            plan: Box::new(identity_plan),
            process: Box::new(move |_, _, _| {
                count.fetch_add(1, Ordering::SeqCst);
                Ok(StageOutcome::Processed)
            }),
        })
        .await;
        plan(&mut s, input()).await;
        let mut a = arrangement(input(), 1);
        match case {
            "stage" => a.stage_id = "undeclared".into(),
            "position-zero" => a.position = 0,
            "position-large" => a.position = 32767,
            "pipeline-zero" => a.pipeline = 0,
            "pipeline-large" => a.pipeline = 65536,
            _ => {}
        }
        s.send("", Host::StageArm(a)).await;
        assert!(
            matches!(s.recv().await.msg, Some(Plugin::ContractViolation(v)) if v.r#where == 32),
            "{case}"
        );
        s.barrier().await; // Any accidental acknowledgement would fail this barrier.
        let ring = Ring::new(&s);
        install_ring(&mut s).await;
        ring.fill(0);
        ring.hand(0, 1, 37, 1);
        tokio::time::sleep(Duration::from_millis(20)).await;
        assert_eq!(calls.load(Ordering::SeqCst), 0, "{case}");
        ring.hand(0, 1, 37, 0);
        s.stop().await;
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn arms_capture_dependencies_before_the_next_plan_for_the_same_stage() {
    let plans = AtomicUsize::new(0);
    let calls = Arc::new(AtomicUsize::new(0));
    let count = calls.clone();
    let mut s = session(Handler {
        plan: Box::new(move |i| {
            Ok(pb::StagePlanned {
                output: Some(i.clone()),
                modifies: vec![pb::StageChannelNeed {
                    channel: 7,
                    needs: vec![if plans.fetch_add(1, Ordering::SeqCst) == 0 {
                        7
                    } else {
                        8
                    }],
                }],
                ..Default::default()
            })
        }),
        process: Box::new(move |_, a, _| {
            assert_eq!(a.position, 1);
            count.fetch_add(1, Ordering::SeqCst);
            Ok(StageOutcome::Processed)
        }),
    })
    .await;
    let shape = pb::StageShape {
        channels: vec![7, 8],
        ..input()
    };
    let ring = Ring::new(&s);
    install_ring(&mut s).await;
    for position in [1, 2] {
        plan(&mut s, shape.clone()).await;
        arm(&mut s, arrangement(shape.clone(), position)).await;
    }
    ring.fill(0); // Only channel 7 was loaded.
    ring.hand(0, 1, 37, 1);
    ring.wait_position(0, 1, 37, &[-2]).await;
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    assert_eq!(
        (ring.u32(0, 8), ring.u32(0, 24), ring.u32(0, 20)),
        (2, 2, 1)
    );
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn different_stage_ids_share_one_grant_and_revision_identity_is_the_position() {
    let calls = Arc::new(Mutex::new(Vec::new()));
    let handlers = ["edit", "other"]
        .into_iter()
        .map(|id| {
            let calls = calls.clone();
            (
                id,
                Handler {
                    plan: Box::new(identity_plan),
                    process: Box::new(move |_, a, _| {
                        calls.lock().unwrap().push((id, a.position));
                        Ok(StageOutcome::Processed)
                    }),
                },
            )
        })
        .collect();
    let mut s = session_many(handlers).await;
    let ring = Ring::new(&s);
    install_ring(&mut s).await;
    for (id, position) in [("edit", 1), ("other", 2)] {
        plan_stage(&mut s, id, input()).await;
        let mut a = arrangement(input(), position);
        a.stage_id = id.into();
        arm(&mut s, a).await;
    }
    ring.fill(0);
    ring.hand(0, 1, 37, 1);
    ring.wait_position(0, 1, 37, &[3]).await;
    assert_eq!(*calls.lock().unwrap(), [("edit", 1), ("other", 2)]);
    // Changing the stage ID at one existing position is still a new revision
    // of that pair, not a first revision for the new stage ID.
    plan_stage(&mut s, "other", input()).await;
    let mut replacement = arrangement(input(), 1);
    replacement.stage_id = "other".into();
    s.send("", Host::StageArm(replacement.clone())).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::ContractViolation(v)) if v.r#where == 19));
    replacement.revision = 2;
    arm(&mut s, replacement).await;
    ring.fill(0);
    ring.hand(0, 2, 37, 1);
    ring.wait_position(0, 2, 37, &[3]).await;
    assert_eq!(
        *calls.lock().unwrap(),
        [("edit", 1), ("other", 2), ("other", 1), ("other", 2)]
    );
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn stage_plan_errors_distinguish_host_input_from_author_failures() {
    let mut s = session(Handler {
        plan: Box::new(|i| {
            let mut p = identity_plan(i)?;
            match i.x {
                1 => panic!("plan panic"),
                2 => return Err(control::Error::unsupported("author refusal")),
                3 => p.output.as_mut().unwrap().x += 1,
                4 => p.modifies[0].channel = 8, // Legal index absent from input.
                5 => p.output.as_mut().unwrap().dtype = 9,
                6 => p.output.as_mut().unwrap().channels = vec![65536],
                7 => p.modifies[0].needs = vec![65536],
                8 => p.reads = vec![65536],
                9 => p.reads = vec![8], // In range, but absent from input.
                10 => p.output.as_mut().unwrap().z = 0,
                11 => p.output.as_mut().unwrap().y = 0,
                12 => p.output.as_mut().unwrap().x = 0,
                _ => {}
            }
            Ok(p)
        }),
        process: Box::new(|_, _, _| unreachable!()),
    })
    .await;
    use pb::error::Kind;
    let mut requests = vec![
        ("undeclared", input(), Kind::NotFound),
        (
            "edit",
            pb::StageShape {
                dtype: 0,
                ..input()
            },
            Kind::Protocol,
        ),
        (
            "edit",
            pb::StageShape {
                dtype: 9, // a later minor's: the plan alone is refused
                ..input()
            },
            Kind::Unsupported,
        ),
        (
            "edit",
            pb::StageShape {
                channels: vec![65536],
                ..input()
            },
            Kind::Protocol,
        ),
    ];
    for (x, kind) in [
        (1, Kind::Internal),
        (2, Kind::Unsupported),
        (3, Kind::Internal),
        (4, Kind::Internal),
        (5, Kind::Internal),
        (6, Kind::Protocol),
        (7, Kind::Protocol),
        (8, Kind::Protocol),
        (9, Kind::Internal),
        (10, Kind::Internal),
        (11, Kind::Internal),
        (12, Kind::Internal),
    ] {
        requests.push(("edit", pb::StageShape { x, ..input() }, kind));
    }
    for (stage_id, shape, kind) in requests {
        s.send(
            "bad-plan",
            Host::PlanStage(pb::PlanStage {
                params: Default::default(),
                stage_id: stage_id.into(),
                input: Some(shape),
            }),
        )
        .await;
        let reply = s.recv().await;
        assert_eq!(reply.id, "bad-plan");
        assert!(
            matches!(reply.msg, Some(Plugin::Error(e)) if e.kind == kind as i32),
            "expected {kind:?}"
        );
    }
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn modified_channel_itself_is_required_even_when_its_needs_are_empty() {
    let mut s = session(Handler {
        plan: Box::new(|i| {
            Ok(pb::StagePlanned {
                output: Some(i.clone()),
                modifies: vec![pb::StageChannelNeed {
                    channel: 7,
                    needs: vec![],
                }],
                ..Default::default()
            })
        }),
        process: Box::new(|_, _, _| panic!("absent modified channel reached author")),
    })
    .await;
    plan(&mut s, input()).await;
    let ring = Ring::new(&s);
    install_ring(&mut s).await;
    arm(&mut s, arrangement(input(), 1)).await;
    ring.fill(0);
    ring.set_u32(0, 32, 0);
    ring.hand(0, 1, 37, 1);
    ring.returned(0, 1).await;
    assert_eq!((ring.u32(0, 8), ring.u32(0, 24)), (2, 2));
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn read_only_dependencies_are_required_and_snapshotted_without_rewriting_planes() {
    let calls = Arc::new(AtomicUsize::new(0));
    let count = calls.clone();
    let plans = AtomicUsize::new(0);
    let mut s = session(Handler {
        plan: Box::new(move |i| {
            Ok(pb::StagePlanned {
                output: Some(i.clone()),
                reads: vec![if plans.fetch_add(1, Ordering::SeqCst) == 0 {
                    8
                } else {
                    7
                }],
                ..Default::default()
            })
        }),
        process: Box::new(move |_, _, slot| {
            count.fetch_add(1, Ordering::SeqCst);
            assert_eq!(slot.read_voxel(8, [0, 0, 0])?, Voxel::U8(1));
            slot.pose.translation[0] = 5.;
            Ok(StageOutcome::Processed)
        }),
    })
    .await;
    let offered = pb::StageShape {
        channels: vec![7, 8],
        ..input()
    };
    let p = plan(&mut s, offered.clone()).await;
    assert_eq!(p.reads, vec![8]);
    assert!(p.modifies.is_empty() && p.adds.is_empty());
    let ring = Ring::new(&s);
    install_ring(&mut s).await;
    arm(&mut s, arrangement(offered.clone(), 1)).await;
    plan(&mut s, offered).await; // Must not replace the accepted arm's reads.
    ring.fill(0); // Only channel 7 loaded: read-only dependency 8 is missing.
    ring.hand(0, 1, 37, 1);
    ring.returned(0, 1).await;
    assert_eq!((ring.u32(0, 8), ring.u32(0, 24)), (2, 2));
    assert_eq!(calls.load(Ordering::SeqCst), 0);
    ring.fill(0);
    ring.file
        .write_all_at(&8u16.to_le_bytes(), 64 + 256)
        .unwrap();
    ring.hand(0, 2, 37, 1);
    ring.returned(0, 2).await;
    assert_eq!(ring.u32(0, 8), 1);
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    let mut bytes = [0; 4];
    ring.file.read_exact_at(&mut bytes, 64 + 320).unwrap();
    assert_eq!(bytes, [1, 2, 3, 4]);
    let mut pose = [0; 8];
    ring.file.read_exact_at(&mut pose, 64 + 184).unwrap();
    assert_eq!(f64::from_le_bytes(pose), 5.);
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn output_exceeding_the_arm_is_failed_reason_two_without_publishing_the_header() {
    let mut s = session(Handler {
        plan: Box::new(|i| {
            let mut p = identity_plan(i)?;
            p.output = Some(pb::StageShape {
                z: 1,
                y: 1,
                x: 2,
                ..i.clone()
            });
            Ok(p)
        }),
        process: Box::new(|_, _, slot| {
            slot.layout.allocation = [1, 1, 2];
            slot.extent = [[0, 1], [0, 1], [0, 2]];
            match slot.iteration() {
                1 => slot.layout.dtype = pb::DType::DtypeF32,
                i => slot.layout.allocation[(i - 2) as usize] += 1,
            }
            Ok(StageOutcome::Processed)
        }),
    })
    .await;
    let offered = pb::StageShape {
        z: 2,
        y: 2,
        ..input()
    };
    let p = plan(&mut s, offered.clone()).await;
    let ring = Ring::new(&s);
    install_ring(&mut s).await;
    let mut a = arrangement(offered, 1);
    a.output = p.output;
    arm(&mut s, a).await;
    for iteration in 1..=4 {
        ring.fill(0);
        ring.hand(0, iteration, 37, 1);
        ring.returned(0, iteration).await;
        assert_eq!((ring.u32(0, 8), ring.u32(0, 24)), (3, 2));
        assert_eq!(ring.u32(0, 20), 0);
        assert_eq!(
            (
                ring.u32(0, 28),
                ring.u32(0, 40),
                ring.u32(0, 44),
                ring.u32(0, 48)
            ),
            (1, 1, 1, 4)
        );
    }
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn missing_shapes_plan_and_duplicate_arm_channels_report_site_32() {
    let mut s = session(Handler {
        plan: Box::new(identity_plan),
        process: Box::new(|_, _, _| panic!("refused arm reached author")),
    })
    .await;
    let _ring = Ring::new(&s);
    install_ring(&mut s).await;
    for case in [
        "no-plan",
        "input",
        "output",
        "duplicate-input",
        "duplicate-new",
        "overlap",
    ] {
        let mut a = arrangement(input(), 1);
        match case {
            "input" => a.input = None,
            "output" => a.output = None,
            "duplicate-input" => a.input.as_mut().unwrap().channels.push(7),
            "duplicate-new" => {
                a.new_channels = [("a".into(), 8), ("b".into(), 8)].into_iter().collect()
            }
            "overlap" => {
                a.new_channels.insert("a".into(), 7);
            }
            _ => {}
        }
        s.send("", Host::StageArm(a)).await;
        assert!(
            matches!(s.recv().await.msg, Some(Plugin::ContractViolation(v)) if v.r#where == 32),
            "{case}"
        );
        s.barrier().await; // No acknowledgement or Error may accompany the refusal.
        if case == "no-plan" {
            plan(&mut s, input()).await;
        }
    }
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn arm_revision_outlives_retirement_and_a_later_ring() {
    let mut s = session(Handler {
        plan: Box::new(identity_plan),
        process: Box::new(|_, _, _| Ok(StageOutcome::Processed)),
    })
    .await;
    plan(&mut s, input()).await;
    let old_ring = Ring::new(&s);
    install_ring(&mut s).await;
    let a = arrangement(input(), 1);
    arm(&mut s, a.clone()).await;
    s.send("", Host::RetireSegment(pb::RetireSegment { segment_id: 1 }))
        .await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::SegmentReleased(r)) if r.segment_id == 1));
    old_ring.fill(0);
    old_ring.hand(0, 1, 37, 1);
    tokio::time::sleep(Duration::from_millis(30)).await;
    assert_eq!(old_ring.u32(0, 8), 0, "retired arm must be inert");
    let ring = Ring::new_id(&s, 2);
    install_ring_id(&mut s, 2).await;
    let mut next = a;
    next.segment_id = 2;
    // A different ring with revision 1 is still conflicting content, not new.
    s.send("", Host::StageArm(next.clone())).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::ContractViolation(v)) if v.r#where == 19));
    next.revision = 2;
    arm(&mut s, next).await;
    s.barrier().await; // No erroneous first-revision diagnostic at site 26.
    ring.fill(0);
    ring.hand(0, 1, 37, 1);
    ring.returned(0, 1).await;
    assert_eq!(ring.u32(0, 8), 1);
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn refused_and_failed_turns_publish_only_outcome_and_reason_in_the_header() {
    let mut s = session(Handler {
        plan: Box::new(identity_plan),
        process: Box::new(|_, _, slot| {
            slot.origin = [3., 4., 5.];
            slot.extent[2] = [1, 2];
            slot.layout.allocation[2] = 2;
            if slot.iteration() == 1 {
                Ok(StageOutcome::Refused(Refusal::Other))
            } else {
                slot.write_bytes(0, &[99])?;
                Ok(StageOutcome::Failed(vizi_doorbell::stage::Failure::Other))
            }
        }),
    })
    .await;
    plan(&mut s, input()).await;
    let ring = Ring::new(&s);
    install_ring(&mut s).await;
    arm(&mut s, arrangement(input(), 1)).await;
    for iteration in [1, 2] {
        ring.fill(0);
        let mut before = [0; 312];
        ring.file.read_exact_at(&mut before, 64 + 8).unwrap();
        ring.hand(0, iteration, 37, 1);
        ring.returned(0, iteration).await;
        let mut after = [0; 312];
        ring.file.read_exact_at(&mut after, 64 + 8).unwrap();
        assert_eq!(
            (ring.u32(0, 8), ring.u32(0, 24)),
            (if iteration == 1 { 2 } else { 3 }, 3)
        );
        after[..4].fill(0); // outcome
        after[16..20].fill(0); // reason
        assert_eq!(after, before);
        let mut byte = [0];
        ring.file.read_exact_at(&mut byte, 64 + 320).unwrap();
        assert_eq!(byte, [if iteration == 1 { 1 } else { 99 }]);
    }
    s.barrier().await;
    s.stop().await;
}

mod params;

#[test]
fn staged_acquisition_error_is_diagnosed_once_per_arm_and_other_slots_continue() {
    const CHILD: &str = "VIZI_RUST_STAGE_DIAGNOSTIC_CHILD";
    if std::env::var_os(CHILD).is_none() {
        let output = std::process::Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "stage_cases::staged_acquisition_error_is_diagnosed_once_per_arm_and_other_slots_continue", "--nocapture"])
            .env(CHILD, "1")
            .output().unwrap();
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(
            output.status.success(),
            "{}\n{stderr}",
            String::from_utf8_lossy(&output.stdout)
        );
        assert_eq!(
            stderr
                .lines()
                .filter(|line| line.contains("stage \"edit\" turn")
                    && line.contains("region quarantined"))
                .count(),
            2,
            "{stderr}"
        );
        return;
    }
    tokio::runtime::Runtime::new().unwrap().block_on(async {
        let mut s = session(Handler {
            plan: Box::new(identity_plan),
            process: Box::new(|_, _, _| Ok(StageOutcome::Processed)),
        })
        .await;
        let ring = Ring::new(&s);
        install_ring(&mut s).await;
        plan(&mut s, input()).await;
        // A bad host release quarantines slot 0. Its acquisition now fails on
        // every scan, while slot 1 remains usable.
        s.send(
            "",
            Host::ReleaseLoan(pb::ReleaseLoan {
                segment_id: 1,
                region: 0,
                offset: 64,
                len: 1,
            }),
        )
        .await;
        assert!(matches!(s.recv().await.msg,
            Some(Plugin::ContractViolation(v)) if v.r#where == 11));
        for revision in [1, 2] {
            let mut a = arrangement(input(), 1);
            a.revision = revision;
            arm(&mut s, a).await;
            ring.fill(1);
            ring.hand(1, revision, 37, 1);
            ring.returned(1, revision).await;
            // Several periodic scans of the same failing slot must stay quiet.
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        s.stop().await;
    });
}
