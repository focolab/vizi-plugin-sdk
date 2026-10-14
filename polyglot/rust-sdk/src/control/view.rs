//! User-caused navigation. The host judges authority, freshness and idempotence.
use super::*;

/// All requested targets apply together or none do. Coordinates are level-0,
/// voxel-centred z,y,x. The host judges all targets against the user's cause
/// and the switch on the plugin's open, visible panel.
#[derive(Clone, Debug, Default)]
pub struct ViewMove {
    pub t: Option<u32>,
    pub z: Option<u32>,
    pub select_region: Option<String>,
    pub frame_region: Option<String>,
    pub look_at: Option<pb::LookAt>,
    pub orbit_point: Option<pb::OrbitPoint>,
}

/// A command's host-minted cause, or a FIRE's captured (tool, interaction).
/// Answer every cause promptly (preferably within 1 s; stale after 2 s), even
/// when declining. Identical retries are idempotent at the host; a different
/// request for a spent cause is CONSUMED. Cloning never creates a fresh cause.
#[derive(Clone)]
pub struct ViewControl {
    ctx: Context,
    cause: u64,
    tool_cause: Option<pb::ToolCause>,
}
impl Context {
    /// Bind navigation to this invocation, refusing an ordinary command locally.
    pub fn command_view(&self, command: &pb::InvokeCommand) -> Result<ViewControl> {
        if command.cause == 0 {
            return Err(Error::protocol("command has no view cause"));
        }
        Ok(ViewControl {
            ctx: self.clone(),
            cause: command.cause,
            tool_cause: None,
        })
    }
    /// Only FIRE is a cause, including an action's FIRE. Use start_move/start_decline
    /// in an input callback and wait for the result on an application worker.
    pub fn input_view(&self, input: &Input) -> Result<ViewControl> {
        let event = &input.event;
        if event.phase != crate::Phase::Fire || event.tool == 0 || event.interaction == 0 {
            return Err(Error::protocol(
                "view navigation requires a FIRE with a nonzero tool and interaction",
            ));
        }
        Ok(ViewControl {
            ctx: self.clone(),
            cause: 0,
            tool_cause: Some(pb::ToolCause {
                tool: event.tool,
                interaction: event.interaction,
            }),
        })
    }
    /// Lower-level checked submission; normal code uses a cause-bound ViewControl.
    /// Refusals, including UNSUPPORTED, are successful ViewRequestResult replies.
    pub fn start_view_request(&self, request: pb::ViewRequest) -> Result<ViewRequestHandle> {
        validate(&request)?;
        let cause = request.cause;
        let tool_cause = request.tool_cause;
        let pending = self.start_request(Plugin::ViewRequest(request))?;
        Ok(ViewRequestHandle {
            pending,
            cause,
            tool_cause,
        })
    }
    pub fn request_view(&self, request: pb::ViewRequest) -> Result<pb::ViewRequestResult> {
        self.start_view_request(request)?
            .wait(Duration::from_secs(30))
    }
}
impl ViewControl {
    fn request(&self, targets: ViewMove, decline: bool) -> pb::ViewRequest {
        pb::ViewRequest {
            cause: self.cause,
            tool_cause: self.tool_cause,
            decline,
            t: targets.t,
            z: targets.z,
            select_region: targets.select_region,
            frame_region: targets.frame_region,
            look_at: targets.look_at,
            orbit_point: targets.orbit_point,
        }
    }
    /// Submit immediately without waiting. Safe to use in a doorbell input callback.
    pub fn start_move(&self, targets: ViewMove) -> Result<ViewRequestHandle> {
        self.ctx.start_view_request(self.request(targets, false))
    }
    pub fn start_decline(&self) -> Result<ViewRequestHandle> {
        self.ctx
            .start_view_request(self.request(ViewMove::default(), true))
    }
    /// Wait on a command/application worker, never an input callback. The reply,
    /// not a later ViewChanged push, acknowledges whether the request was applied.
    pub fn move_view(&self, targets: ViewMove) -> Result<pb::ViewRequestResult> {
        self.start_move(targets)?.wait(Duration::from_secs(30))
    }
    pub fn decline_view(&self) -> Result<pb::ViewRequestResult> {
        self.start_decline()?.wait(Duration::from_secs(30))
    }
}

/// Retains the normal request deadline/shutdown lifecycle and validates the
/// echoed cause. Timing out means unknown, not refused; it does not cancel a move.
pub struct ViewRequestHandle {
    pending: HostRequest,
    cause: u64,
    tool_cause: Option<pb::ToolCause>,
}
impl ViewRequestHandle {
    pub fn id(&self) -> &str {
        self.pending.id()
    }
    pub fn wait(self, timeout: Duration) -> Result<pb::ViewRequestResult> {
        let reply = self.pending.wait(timeout)?;
        decode(reply, self.cause, self.tool_cause)
    }
    /// None leaves the waiter active, allowing another finite wait on a worker.
    pub fn wait_timeout(&mut self, timeout: Duration) -> Result<Option<pb::ViewRequestResult>> {
        self.pending
            .wait_timeout(timeout)?
            .map(|reply| decode(reply, self.cause, self.tool_cause))
            .transpose()
    }
}
impl pb::ViewRequestResult {
    pub fn applied(&self) -> bool {
        self.outcome == pb::ViewOutcome::Applied as i32
    }
}
fn decode(
    reply: HostReply,
    cause: u64,
    tool_cause: Option<pb::ToolCause>,
) -> Result<pb::ViewRequestResult> {
    let Host::ViewRequestResult(result) = &reply.message else {
        return Err(Error::protocol("expected ViewRequestResult"));
    };
    if result.cause != cause || result.tool_cause != tool_cause {
        return Err(Error::protocol(
            "ViewRequestResult echoed a different cause",
        ));
    }
    // Preserve unknown future outcomes and reasons numerically.
    Ok(result.clone())
}
fn validate(request: &pb::ViewRequest) -> Result<()> {
    if (request.cause != 0) == request.tool_cause.is_some() {
        return Err(Error::protocol("ViewRequest requires exactly one cause"));
    }
    if request
        .tool_cause
        .is_some_and(|v| v.tool == 0 || v.interaction == 0)
    {
        return Err(Error::protocol(
            "ToolCause requires a nonzero tool and interaction",
        ));
    }
    let targets = request.t.is_some()
        || request.z.is_some()
        || request.select_region.is_some()
        || request.frame_region.is_some()
        || request.look_at.is_some()
        || request.orbit_point.is_some();
    if request.decline == targets {
        return Err(Error::protocol(
            "a move needs targets; a decline must not have targets",
        ));
    }
    Ok(())
}
