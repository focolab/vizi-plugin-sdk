//! Person-started movement, independent of command causes and callback workers.
use super::*;
use std::sync::Condvar;

#[derive(Default)]
struct State {
    seq: u64,
    ended: bool,
    result: Option<pb::FollowEnded>,
}
pub(super) struct Running {
    started: pb::FollowStarted,
    state: Mutex<State>,
    wake: Condvar,
}
impl Running {
    fn stop(&self, result: Option<pb::FollowEnded>) {
        let mut state = self.state.lock().unwrap();
        if !state.ended {
            state.ended = true;
            state.result = result;
        }
        self.wake.notify_all();
    }
}

/// One Follow started by the person. Clones share sequence, end state and wakeups.
/// The SDK ends it when its handler returns, errors or panics. Session shutdown
/// wakes waiters without sending EndFollow or inventing a host end reason.
#[derive(Clone)]
pub struct Follow {
    ctx: Context,
    running: Arc<Running>,
}
impl Follow {
    pub fn id(&self) -> u64 {
        self.running.started.follow_id
    }
    pub fn panel_id(&self) -> &str {
        &self.running.started.panel_id
    }
    pub fn targets(&self) -> &[String] {
        &self.running.started.targets
    }
    pub fn resumed(&self) -> bool {
        self.running.started.resumed
    }
    pub fn active(&self) -> bool {
        !self.running.state.lock().unwrap().ended
    }
    /// Host reason/detail, or local PLUGIN_ENDED. None while active or after
    /// session shutdown; unknown future host reasons are preserved numerically.
    pub fn ended(&self) -> Option<pb::FollowEnded> {
        self.running.state.lock().unwrap().result.clone()
    }
    /// Publish a whole state, not a delta. True means queued, not applied;
    /// false means this Follow has ended. Undeclared/empty moves fail locally.
    pub fn move_view(&self, targets: ViewMove) -> Result<bool> {
        let mut state = self.running.state.lock().unwrap();
        if state.ended {
            return Ok(false);
        }
        let fields = [
            ("t", targets.t.is_some()),
            ("z", targets.z.is_some()),
            ("select_region", targets.select_region.is_some()),
            ("frame_region", targets.frame_region.is_some()),
            ("look_at", targets.look_at.is_some()),
            ("orbit_point", targets.orbit_point.is_some()),
        ];
        if !fields.iter().any(|(_, set)| *set) {
            return Err(Error::protocol("a Follow state needs at least one target"));
        }
        for (name, set) in fields {
            if set && !self.targets().iter().any(|v| v == name) {
                return Err(Error::protocol(format!(
                    "this Follow does not drive {name}"
                )));
            }
        }
        let seq = state
            .seq
            .checked_add(1)
            .ok_or_else(|| Error::internal("Follow sequence exhausted"))?;
        self.ctx.push(Plugin::FollowState(pb::FollowState {
            follow_id: self.id(),
            seq,
            t: targets.t,
            z: targets.z,
            select_region: targets.select_region,
            frame_region: targets.frame_region,
            look_at: targets.look_at,
            orbit_point: targets.orbit_point,
        }))?;
        state.seq = seq;
        Ok(true)
    }
    /// Wait up to this duration; false as soon as the host, plugin or session
    /// ends the Follow. Use on its worker, not the input callback thread.
    pub fn wait(&self, duration: Duration) -> bool {
        let state = self.running.state.lock().unwrap();
        let (state, _) = self
            .running
            .wake
            .wait_timeout_while(state, duration, |s| !s.ended)
            .unwrap();
        !state.ended
    }
    /// End once; after a host end this is a no-op. Admission failure is an error
    /// and leaves the Follow active, allowing the caller to retry.
    pub fn end(&self) -> Result<()> {
        let mut state = self.running.state.lock().unwrap();
        if state.ended {
            return Ok(());
        }
        if self.ctx.is_shutting_down() {
            state.ended = true;
        } else {
            self.ctx.push(Plugin::EndFollow(pb::EndFollow {
                follow_id: self.id(),
            }))?;
            state.ended = true;
            state.result = Some(pb::FollowEnded {
                follow_id: self.id(),
                reason: pb::FollowEndReason::FollowEndPluginEnded as i32,
                detail: String::new(),
            });
        }
        self.running.wake.notify_all();
        Ok(())
    }
}

pub(super) fn same_key(a: &pb::PluginMsg, b: &pb::PluginMsg) -> bool {
    match (&a.msg, &b.msg) {
        (Some(Plugin::OfferFollow(a)), Some(Plugin::OfferFollow(b))) => a.panel_id == b.panel_id,
        (Some(Plugin::FollowState(a)), Some(Plugin::FollowState(b))) => a.follow_id == b.follow_id,
        _ => false,
    }
}
impl Core {
    pub(super) fn follow_ended(&self, ended: pb::FollowEnded) {
        if let Some(running) = self.follows.lock().unwrap().remove(&ended.follow_id) {
            running.stop(Some(ended));
        }
    }
    pub(super) fn stop_follows(&self) {
        for (_, running) in std::mem::take(&mut *self.follows.lock().unwrap()) {
            running.stop(None);
        }
    }
}
pub(super) fn start(
    ctx: &Context,
    app: Arc<dyn crate::plugin::Plugin>,
    started: pb::FollowStarted,
) -> Result<()> {
    let id = started.follow_id;
    let running = Arc::new(Running {
        started,
        state: Mutex::new(State::default()),
        wake: Condvar::new(),
    });
    {
        let mut follows = ctx.core.follows.lock().unwrap();
        if ctx.is_shutting_down() {
            return Ok(());
        }
        if follows.contains_key(&id) {
            return Err(Error::protocol("duplicate FollowStarted id"));
        }
        follows.insert(id, running.clone());
    }
    let follow = Follow {
        ctx: ctx.clone(),
        running,
    };
    let fallback = follow.clone();
    if let Err(error) = std::thread::Builder::new()
        .name(format!("vizi-follow-{id}"))
        .spawn(move || {
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                app.follow(&follow.ctx, &follow)
            }));
            match result {
                Ok(Ok(())) => {}
                // A request that met the session's end: a quiet end, not a fault.
                Ok(Err(e)) if e.is_session_ended() => {}
                Ok(Err(e)) => eprintln!("vizi SDK Follow handler error: {e}"),
                Err(_) => eprintln!("vizi SDK Follow handler panicked; ending Follow"),
            }
            if let Err(e) = follow.end() {
                follow.ctx.core.fault(format!("cannot end Follow: {e}"));
            }
            follow.ctx.core.follows.lock().unwrap().remove(&id);
        })
    {
        fallback.end()?;
        ctx.core.follows.lock().unwrap().remove(&id);
        return Err(error.into());
    }
    Ok(())
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;
    #[test]
    fn coalescing_keys_byte_accounting_and_terminal_order() {
        let manifest = crate::manifest::Manifest::parse(r#"{"manifest_version":1,"id":"org.example.test","name":"Test","version":"1","engines":{"vizi":"1.0"},"entry":{"command":["test"]}}"#).unwrap();
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
        *ctx.core.peer.lock().unwrap() = Some((1, 0));
        ctx.panel("p").offer_follow("first").unwrap();
        ctx.panel("q").offer_follow("other panel").unwrap();
        assert!(ctx.panel("p").offer_follow("").is_err());
        ctx.panel("p").withdraw_follow().unwrap();
        for id in [7, 8] {
            for seq in 1..1000 {
                ctx.push(Plugin::FollowState(pb::FollowState {
                    follow_id: id,
                    seq,
                    t: Some(seq as u32),
                    ..Default::default()
                }))
                .unwrap();
            }
            ctx.push(Plugin::EndFollow(pb::EndFollow { follow_id: id }))
                .unwrap();
        }
        let q = ctx.core.queues.lock().unwrap();
        assert_eq!(q.reliable.len(), 6);
        assert!(q.pushes.is_empty());
        assert_eq!(
            q.bytes,
            q.reliable.iter().map(Message::encoded_len).sum::<usize>()
        );
        assert!(
            matches!(&q.reliable[0].msg, Some(Plugin::OfferFollow(v)) if v.panel_id == "p" && v.label.is_empty())
        );
        assert!(
            matches!(&q.reliable[1].msg, Some(Plugin::OfferFollow(v)) if v.panel_id == "q" && v.label == "other panel")
        );
        for (offset, id) in [(2, 7), (4, 8)] {
            assert!(
                matches!(&q.reliable[offset].msg, Some(Plugin::FollowState(v)) if v.follow_id == id && v.seq == 999)
            );
            assert!(
                matches!(&q.reliable[offset + 1].msg, Some(Plugin::EndFollow(v)) if v.follow_id == id)
            );
        }
        drop(q);
        runtime.stop();
    }
}
