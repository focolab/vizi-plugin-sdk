use super::*;
use crate::stage::{self, Outcome, Slot, Stage};
use std::{collections::BTreeSet, sync::Condvar};

#[derive(Clone)]
struct Armed {
    message: pb::StageArm,
    plan: pb::StagePlanned,
    params: stage::Params,
}
#[derive(Default)]
struct State {
    plans: Vec<Planned>,
    wake: bool,
}
struct Planned {
    input: pb::StageShape,
    params: stage::Params,
    reply: pb::StagePlanned,
}
struct Entry {
    params: Vec<stage::params::Declaration>,
    stage: Arc<dyn Stage>,
    state: Mutex<State>,
    wake: Condvar,
}
pub(super) struct Stages {
    entries: BTreeMap<String, Arc<Entry>>,
    arms: Arc<Mutex<BTreeMap<(u32, u32), Armed>>>,
}
impl Stages {
    pub fn new(
        manifest: &crate::manifest::Manifest,
        stages: BTreeMap<String, Arc<dyn Stage>>,
    ) -> Result<Self> {
        let declared = manifest.contribution_ids("stages");
        if stages.keys().any(|id| !declared.contains(id)) {
            return Err(Error::protocol("registered stage is not declared"));
        }
        Ok(Self {
            arms: Arc::new(Mutex::new(BTreeMap::new())),
            entries: stages
                .into_iter()
                .map(|(id, stage)| {
                    let declaration = manifest.value()["contributions"]["stages"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .find(|v| v["id"].as_str() == Some(&id))
                        .unwrap();
                    let params =
                        stage::params::parse(declaration.get("params")).map_err(Error::protocol)?;
                    Ok((
                        id,
                        Arc::new(Entry {
                            params,
                            stage,
                            state: Mutex::new(State::default()),
                            wake: Condvar::new(),
                        }),
                    ))
                })
                .collect::<Result<_>>()?,
        })
    }
    pub fn plan(&self, ctx: &Context, request: &pb::PlanStage) -> Result<pb::StagePlanned> {
        let entry = self
            .entries
            .get(&request.stage_id)
            .ok_or_else(|| Error::new(pb::error::Kind::NotFound, "stage not registered"))?;
        let input = request
            .input
            .as_ref()
            .ok_or_else(|| Error::protocol("stage input missing"))?;
        stage::validate_shape(input)?;
        let params = stage::params::check(&entry.params, &request.params)?;
        let mut plan = entry.stage.plan(ctx, input, &params)?;
        stage::validate_plan(input, &plan)?;
        bounds::plugin(&pb::PluginMsg {
            id: String::new(),
            msg: Some(Plugin::StagePlanned(plan.clone())),
        })?;
        plan.output.as_mut().unwrap().channels.clear();
        // Keep the latest answer per input AND settings, so the host can plan
        // several positions before sizing/granting their shared ring. Arming
        // captures dependencies; a later plan cannot mutate an existing arm.
        let mut state = entry.state.lock().unwrap();
        let recorded = Planned {
            input: input.clone(),
            params,
            reply: plan.clone(),
        };
        if let Some(old) = state
            .plans
            .iter_mut()
            .find(|p| p.input == recorded.input && p.params == recorded.params)
        {
            *old = recorded;
        } else {
            state.plans.push(recorded);
        }
        Ok(plan)
    }
    pub fn arm(&self, core: &Core, id: String, arm: pb::StageArm) -> Result<()> {
        let reject = |message: &str| {
            core.memory.report(32, message, vec![]);
            Ok(())
        };
        let Some(entry) = self.entries.get(&arm.stage_id) else {
            return reject("stage not registered");
        };
        if arm.revision == 0 {
            core.memory.report(4, "zero stage revision", vec![]);
            return Ok(());
        }
        if !(1..=32766).contains(&arm.position) || !(1..=65535).contains(&arm.pipeline) {
            return reject("stage pipeline/position out of range");
        }
        let params = match stage::params::check(&entry.params, &arm.params) {
            Ok(params) => params,
            Err(e) => return reject(&e.message),
        };
        let Some(input) = &arm.input else {
            return reject("stage input missing");
        };
        let mut channels = BTreeSet::new();
        if input
            .channels
            .iter()
            .chain(arm.new_channels.values())
            .any(|c| !channels.insert(c))
        {
            return reject("stage arm repeats a channel index");
        }
        if stage::validate_shape(input).is_err() || arm.new_channels.values().any(|&v| v > 65535) {
            return core.send(
                id,
                Plugin::Error(Error::protocol("invalid stage channel indices or dtype").wire()),
                0,
            );
        }
        if let Some(output) = &arm.output {
            if stage::validate_shape(output).is_err() {
                return core.send(
                    id,
                    Plugin::Error(Error::protocol("invalid stage output").wire()),
                    0,
                );
            }
        } else {
            return reject("stage output missing");
        }
        if !core
            .memory
            .installed()
            .iter()
            .any(|i| i.segment_id == arm.segment_id && i.purpose == "S")
        {
            return reject("stage ring not installed");
        }
        let key = (arm.pipeline, arm.position);
        let mut arms = self.arms.lock().unwrap();
        let ack = || {
            core.push(Plugin::StageArmAck(pb::StageArmAck {
                stage_id: arm.stage_id.clone(),
                revision: arm.revision,
                pipeline: arm.pipeline,
                position: arm.position,
            }))
        };
        if let Some(old) = arms.get(&key) {
            if arm.revision < old.message.revision {
                core.memory.report(20, "stage revision regressed", vec![]);
                return Ok(());
            }
            if arm.revision == old.message.revision {
                if arm != old.message {
                    core.memory
                        .report(19, "stage arrangement content mismatch", vec![]);
                    return Ok(());
                }
                return ack();
            }
        }
        let mut state = entry.state.lock().unwrap();
        let Some(planned) = state
            .plans
            .iter()
            .find(|p| &p.input == input && p.params == params)
        else {
            return reject("stage has not been planned for this input and params");
        };
        let mut plan = planned.reply.clone();
        plan.output = arm.output.clone();
        if stage::validate_plan(input, &plan).is_err() {
            return reject("stage output exceeds input or has invalid dependencies");
        }
        let keys: BTreeSet<_> = plan.adds.iter().map(|a| a.key.as_str()).collect();
        let assigned: BTreeSet<_> = arm.new_channels.values().copied().collect();
        if keys != arm.new_channels.keys().map(String::as_str).collect()
            || assigned.len() != arm.new_channels.len()
            || input.channels.iter().any(|c| assigned.contains(c))
        {
            return reject("assigned channels differ from planned additions");
        }
        if !arms.contains_key(&key) && arm.revision != 1 {
            core.memory
                .report(26, "first stage revision is not 1", vec![]);
        }
        arms.insert(
            key,
            Armed {
                message: arm.clone(),
                plan,
                params,
            },
        );
        state.wake = true;
        entry.wake.notify_one();
        // Install before acknowledging; each acquisition checks this same mutex.
        ack()
    }
    pub fn hint(&self, id: u64) {
        let arms = self.arms.lock().unwrap();
        for (stage_id, entry) in &self.entries {
            if arms
                .values()
                .any(|a| a.message.stage_id == *stage_id && a.message.segment_id == id)
            {
                entry.state.lock().unwrap().wake = true;
                entry.wake.notify_one();
            }
        }
    }
    pub fn start(&self, ctx: &Context) -> Result<()> {
        for (stage_id, entry) in &self.entries {
            let stage_id = stage_id.clone();
            let arms = self.arms.clone();
            let entry = entry.clone();
            let ctx = ctx.clone();
            std::thread::Builder::new()
                .name("vizi-stage".into())
                .spawn(move || {
                    let mut reported = BTreeSet::new();
                    while !ctx.core.ended.load(Ordering::SeqCst) {
                        {
                            let s = entry.state.lock().unwrap();
                            let (mut s, _) = entry
                                .wake
                                .wait_timeout_while(s, Duration::from_millis(10), |s| !s.wake)
                                .unwrap();
                            s.wake = false;
                        };
                        if ctx.core.shutting_down.load(Ordering::SeqCst) {
                            continue;
                        }
                        let snapshots: Vec<_> = arms
                            .lock()
                            .unwrap()
                            .values()
                            .filter(|a| a.message.stage_id == stage_id)
                            .cloned()
                            .collect();
                        // Keep one diagnostic per armed region, without disabling
                        // retries after a transient acquisition failure.
                        reported.retain(|&(segment, pipeline, position, revision, _)| {
                            snapshots.iter().any(|a| {
                                (a.message.segment_id, a.message.pipeline,
                                    a.message.position, a.message.revision)
                                    == (segment, pipeline, position, revision)
                            })
                        });
                        for arm in snapshots {
                            let installed = ctx.memory().installed();
                            let Some(ring) = installed
                                .iter()
                                .find(|i| i.segment_id == arm.message.segment_id)
                            else {
                                continue;
                            };
                            for region in &ring.regions {
                                if ctx.core.shutting_down.load(Ordering::SeqCst) {
                                    break;
                                }
                                let acquired = {
                                    // Re-arming may happen while a previous slot's
                                    // author is running. Check the installed revision
                                    // under the same lock used by arm() before EACH
                                    // acquisition, never run a new turn on a stale plan.
                                    let current = arms.lock().unwrap();
                                    if current
                                        .get(&(arm.message.pipeline, arm.message.position))
                                        .is_none_or(|current| current.message != arm.message)
                                    {
                                        break;
                                    }
                                    ctx.memory().staged_turn(
                                        ring.segment_id,
                                        region.index,
                                        arm.message.pipeline,
                                        arm.message.position,
                                    )
                                };
                                let turn = match acquired {
                                    Ok(Some(turn)) => turn,
                                    Ok(None) => continue,
                                    Err(error) => {
                                        let key = (ring.segment_id, arm.message.pipeline,
                                            arm.message.position, arm.message.revision, region.index);
                                        if reported.insert(key) && !ctx.is_shutting_down() {
                                            eprintln!("vizi SDK: stage {stage_id:?} turn {key:?}: {error}");
                                        }
                                        continue;
                                    }
                                };
                                let result = match Slot::open(turn, arm.params.clone()) {
                                    Err(error) => {
                                        let (mut turn, error) = *error;
                                        ctx.memory().report(31, error.message, vec![]);
                                        turn.finish(3, 1).map_err(Error::from)
                                    }
                                    Ok(mut slot) => process(&entry.stage, &ctx, &arm, &mut slot),
                                };
                                if let Err(e) = result {
                                    eprintln!("vizi SDK: stage completion: {e}");
                                }
                                let mut mask = crate::RegionMask::default();
                                mask.set(region.index as usize).unwrap();
                                let _ = ctx.notify(&crate::Notification {
                                    segment_id: ring.segment_id,
                                    mask,
                                    pane: None,
                                });
                                ctx.core.flush_memory();
                            }
                        }
                    }
                })?;
        }
        Ok(())
    }
}
fn process(stage: &Arc<dyn Stage>, ctx: &Context, arm: &Armed, slot: &mut Slot) -> Result<()> {
    let input = arm.message.input.as_ref().unwrap();
    let max = [input.z, input.y, input.x];
    if slot.layout.dtype as i32 != input.dtype || (0..3).any(|i| slot.layout.allocation[i] > max[i])
    {
        ctx.memory()
            .report(31, "stage slot differs from armed input", vec![]);
        return slot.finish(3, 1);
    }
    let needs = arm
        .plan
        .modifies
        .iter()
        .flat_map(|m| std::iter::once(&m.channel).chain(m.needs.iter()))
        .chain(arm.plan.adds.iter().flat_map(|a| a.needs.iter()))
        .chain(&arm.plan.reads);
    if needs
        .into_iter()
        .any(|c| !slot.layout.channels.contains(&(*c as u16)))
    {
        return slot.finish(2, 2);
    }
    let original = slot.layout.clone();
    let original_extent = slot.extent;
    let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        stage.process(ctx, &arm.message, slot)
    }));
    let result = match outcome {
        Ok(Ok(Outcome::Processed)) => {
            let output = arm.plan.output.as_ref().unwrap();
            let declared = [output.z, output.y, output.x];
            // This is an author breaking its accepted plan, not a malformed
            // incoming header. Leave the header unchanged and report failed/2.
            if slot.layout.dtype as i32 != output.dtype
                || (0..3).any(|i| slot.layout.allocation[i] > declared[i])
            {
                return slot.finish(3, 2);
            }
            let allowed: BTreeSet<_> = arm
                .message
                .new_channels
                .values()
                .map(|&c| c as u16)
                .collect();
            if (0..3).any(|i| {
                slot.extent[i][1].saturating_sub(slot.extent[i][0])
                    > original_extent[i][1] - original_extent[i][0]
            }) || (0..3).any(|i| slot.layout.allocation[i] > original.allocation[i])
                || !slot.layout.channels.starts_with(&original.channels)
                || slot.layout.channels[original.channels.len()..]
                    .iter()
                    .any(|c| !allowed.contains(c))
            {
                Err(Error::protocol(
                    "stage output differs from negotiated layout/channels",
                ))
            } else {
                slot.publish()
            }
        }
        Ok(Ok(Outcome::Refused(reason))) => return slot.finish(2, reason as u32),
        Ok(Ok(Outcome::Failed(reason))) => return slot.finish(3, reason as u32),
        Ok(Err(e)) => {
            eprintln!("vizi SDK: stage callback failed: {e}");
            return slot.finish(3, 2);
        }
        Err(_) => {
            eprintln!("vizi SDK: stage callback panicked");
            return slot.finish(3, 2);
        }
    };
    match result {
        Ok(()) => slot.finish(1, 0),
        Err(e) => {
            ctx.memory().report(31, e.message, vec![]);
            slot.finish(3, 1)
        }
    }
}
