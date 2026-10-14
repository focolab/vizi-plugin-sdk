use crate::{
    geometry::{self, Bounds, Cell, Point},
    overlay, plot, roi,
};
use pb::{host_msg::Msg as Host, plugin_msg::Msg as Request, value::Kind};
use std::{
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc::{self, Receiver, Sender},
        Arc, Mutex,
    },
    time::Duration,
};
use vizi::{
    control::{Context, Input},
    declare::DeclaredPlugin,
    proto as pb, PaneEpoch, Phase, Position,
};
type Result<T> = std::result::Result<T, String>;
const NOTHING: &str = "Nothing under the pointer";
#[derive(Clone)]
struct Pick {
    position: Point,
    value: f32,
    t: u32,
    time: Option<f64>,
    channel: u32,
    level: u8,
    refined: bool,
}
struct Model {
    picks: Vec<Pick>,
    radius: f64,
    refine: bool,
    status: String,
    label: String,
    plot: Option<Vec<u8>>,
}
impl Default for Model {
    fn default() -> Self {
        Self {
            picks: vec![],
            radius: 3.,
            refine: true,
            status: "Ready".into(),
            label: "No pick yet".into(),
            plot: None,
        }
    }
}
enum Job {
    Pick(Box<Input>),
    Edit(&'static str),
    Command(pb::InvokeCommand, Sender<pb::CommandDone>),
}
pub struct App {
    model: Mutex<Model>,
    // Serialize reservation + enqueue, so an action cannot overtake a release.
    submission: Mutex<()>,
    busy: AtomicBool,
    aim: Mutex<(u64, bool)>, // overlay revision and once-per-gesture diagnostic
    picks_revision: AtomicU64,
    tx: Sender<Job>,
    pub stop: AtomicBool,
    probe_open: AtomicBool,
    profile_open: AtomicBool,
}
pub struct Worker(Receiver<Job>);
pub fn new() -> (Arc<App>, Worker) {
    let (tx, rx) = mpsc::channel();
    (
        Arc::new(App {
            model: Mutex::default(),
            submission: Mutex::new(()),
            busy: AtomicBool::new(false),
            aim: Mutex::new((1, false)),
            picks_revision: AtomicU64::new(1),
            tx,
            stop: AtomicBool::new(false),
            probe_open: AtomicBool::new(false),
            profile_open: AtomicBool::new(false),
        }),
        Worker(rx),
    )
}
fn patch(widget: &str, kind: Kind) -> pb::PatchOp {
    pb::PatchOp {
        row_key: String::new(),
        widget_id: widget.into(),
        op: Some(pb::patch_op::Op::SetValue(pb::Value { kind: Some(kind) })),
    }
}
fn count(n: usize) -> String {
    match n {
        0 => "No picks".into(),
        1 => "1 pick".into(),
        _ => format!("{n} picks"),
    }
}
fn done(result: Result<String>) -> pb::CommandDone {
    match result {
        Ok(message) => pb::CommandDone {
            ok: true,
            message,
            ..Default::default()
        },
        Err(message) => pb::CommandDone {
            ok: false,
            message,
            ..Default::default()
        },
    }
}
impl App {
    fn status(&self, ctx: &Context, text: impl Into<String>) {
        let mut m = self.model.lock().unwrap();
        m.status = text.into();
        if !self.probe_open.load(Ordering::Acquire) {
            return;
        }
        if let Err(e) = ctx.panel("probe").set_value("status", m.status.clone()) {
            eprintln!("status: {e}");
        }
    }
    fn restore(&self, ctx: &Context, panel: &str) -> vizi::control::Result<()> {
        match panel {
            "probe" if self.probe_open.load(Ordering::Acquire) => {}
            "profile" if self.profile_open.load(Ordering::Acquire) => {}
            _ => return Ok(()),
        }
        let m = self.model.lock().unwrap();
        match panel {
            "probe" => ctx.patch(
                "probe",
                vec![
                    patch("status", Kind::S(m.status.clone())),
                    patch("count", Kind::S(count(m.picks.len()))),
                    patch("radius", Kind::S(m.radius.to_string())),
                    patch("refine", Kind::B(m.refine)),
                ],
            ),
            "profile" => {
                if let Some(png) = &m.plot {
                    ctx.push_image("profile", "plot", png)?;
                }
                ctx.panel("profile").set_value("pick", m.label.clone())
            }
            _ => Ok(()),
        }
    }
    fn clear_aim(&self, ctx: &Context) -> vizi::control::Result<()> {
        let mut aim = self.aim.lock().unwrap();
        let update = pb::OverlayUpdate {
            overlay_id: "probe.aim".into(),
            revision: aim.0,
            items: vec![],
        };
        aim.0 += 1;
        aim.1 = false;
        ctx.set_overlay(update)
    }
    fn input(&self, ctx: &Context, input: Input) -> vizi::control::Result<()> {
        let event = &input.event;
        if event.phase == Phase::Fire {
            let action = match input.action() {
                Some("undo") => "undo",
                Some("clear") => "clear",
                _ => return Ok(()),
            };
            let _submit = self.submission.lock().unwrap();
            self.tx
                .send(Job::Edit(action))
                .map_err(|_| vizi::control::Error::internal("Worker stopped"))?;
            return Ok(());
        }
        if event.phase == Phase::Cancel {
            return self.clear_aim(ctx);
        }
        if event.phase == Phase::Release {
            self.clear_aim(ctx)?;
            let refusal = if !input.usable_context {
                Some("Input context unavailable")
            } else if event.miss || matches!(event.position, Position::None) {
                Some(NOTHING)
            } else if channel(&input).is_none() {
                Some("Show a channel before picking")
            } else {
                None
            };
            if let Some(reason) = refusal {
                self.status(ctx, reason);
                return Ok(());
            }
            let _submit = self.submission.lock().unwrap();
            if self.busy.swap(true, Ordering::AcqRel) {
                self.status(ctx, "Busy: a pick is still running");
                return Ok(());
            }
            if self.tx.send(Job::Pick(Box::new(input))).is_err() {
                self.busy.store(false, Ordering::Release);
                self.status(ctx, "Worker stopped");
            }
            return Ok(());
        }
        if event.phase == Phase::Press {
            self.aim.lock().unwrap().1 = false;
        }
        if !input.usable_context || event.miss {
            return Ok(());
        }
        if let Some(points) = geometry::aim_segment(&event.position) {
            let scale = scale(&input).map_err(vizi::control::Error::internal)?;
            let points = points.map(|p| std::array::from_fn(|a| geometry::level0(p[a], scale[a])));
            let mut aim = self.aim.lock().unwrap();
            let update = overlay::aim(points, aim.0);
            aim.0 += 1;
            if let Err(e) = ctx.set_overlay_live(
                &update,
                Some(PaneEpoch {
                    view_id: event.view_id,
                    epoch: event.epoch,
                }),
            ) {
                if !aim.1 {
                    aim.1 = true;
                    self.status(
                        ctx,
                        format!("Aim unavailable: {e}; picking remains available"),
                    );
                }
            }
        }
        Ok(())
    }
    fn publish_picks(&self, ctx: &Context) -> Result<()> {
        let m = self.model.lock().unwrap();
        if self.probe_open.load(Ordering::Acquire) {
            ctx.panel("probe")
                .set_value("count", count(m.picks.len()))
                .map_err(|e| e.to_string())?;
        }
        let positions: Vec<_> = m.picks.iter().map(|p| p.position).collect();
        ctx.set_overlay(overlay::picks(
            &positions,
            self.picks_revision.fetch_add(1, Ordering::Relaxed),
        ))
        .map_err(|e| e.to_string())
    }
    fn edit(&self, ctx: &Context, action: &str) -> Result<String> {
        let mut m = self.model.lock().unwrap();
        let text = if action == "undo" {
            if m.picks.pop().is_some() {
                "Undid the last pick"
            } else {
                "No picks to undo"
            }
        } else {
            m.picks.clear();
            "Cleared every pick"
        };
        drop(m);
        self.publish_picks(ctx)?;
        Ok(text.into())
    }
    fn pick(&self, ctx: &Context, input: Input) -> Result<String> {
        let state = input.state.as_ref().ok_or("Input context unavailable")?;
        let g = state.dataset.geometry.as_ref().ok_or("Missing geometry")?;
        let ext = g.extent.as_ref().ok_or("Missing extent")?;
        let extent = (
            [ext.z0 as i64, ext.y0 as i64, ext.x0 as i64],
            [ext.z_n as i64, ext.y_n as i64, ext.x_n as i64],
        );
        let crop = g.crop.as_ref().ok_or("Missing crop")?;
        let scale = scale(&input)?;
        let shape = std::array::from_fn(|a| {
            ((extent.1[a] - extent.0[a]) as f64 / scale[a])
                .floor()
                .max(1.) as i64
        });
        let bounds = geometry::crop(
            [[crop.z0, crop.z1], [crop.y0, crop.y1], [crop.x0, crop.x1]],
            scale,
            shape,
        )
        .ok_or(NOTHING)?;
        let cells = geometry::cells(&input.event.position, bounds)?;
        let bbox = geometry::aabb(&cells).ok_or(NOTHING)?;
        geometry::budget(bbox)?;
        let e = &input.event;
        let c = channel(&input).ok_or("No visible channels")?;
        let volume = read(ctx, e.t, c, e.level as u32, e.gen, bbox)?;
        let shape = std::array::from_fn::<_, 3, _>(|a| (bbox.1[a] - bbox.0[a]) as usize);
        let samples: Vec<_> = cells
            .iter()
            .map(|p| {
                let local: [usize; 3] = std::array::from_fn(|a| (p[a] - bbox.0[a]) as usize);
                volume[(local[0] * shape[1] + local[1]) * shape[2] + local[2]]
            })
            .collect();
        let best = geometry::argmax(&samples).ok_or(NOTHING)?;
        let coarse = cells[best];
        let refined = e.level > 0 && self.model.lock().unwrap().refine;
        let (position, value) = if refined {
            let block = geometry::block(coarse, scale, extent);
            geometry::budget(block)?;
            let data = read(ctx, e.t, c, 0, e.gen, block)?;
            let i = geometry::argmax(&data).ok_or(NOTHING)?;
            let sy = (block.1[1] - block.0[1]) as usize;
            let sx = (block.1[2] - block.0[2]) as usize;
            (
                [
                    block.0[0] as f64 + (i / (sy * sx)) as f64,
                    block.0[1] as f64 + ((i / sx) % sy) as f64,
                    block.0[2] as f64 + (i % sx) as f64,
                ],
                data[i],
            )
        } else if e.level == 0 {
            (coarse.map(|v| v as f64), samples[best])
        } else {
            (
                std::array::from_fn(|a| geometry::level0(coarse[a] as f64, scale[a])),
                samples[best],
            )
        };
        let pick = Pick {
            position,
            value,
            t: e.t,
            time: state.time_at(e.t),
            channel: c,
            level: e.level,
            refined,
        };
        let spacing = state.physical_spacing();
        let distance_scale = spacing.map_or([1.; 3], |s| std::array::from_fn(|a| s[a] * scale[a]));
        let distance = geometry::distances(&cells, distance_scale);
        let unit = if spacing.is_some() {
            g.spacing_unit.as_str()
        } else {
            "voxels"
        };
        let png = plot::render(&distance, &samples, best, unit);
        let number = {
            let mut m = self.model.lock().unwrap();
            m.picks.push(pick.clone());
            m.picks.len()
        };
        self.publish_picks(ctx)?;
        let mut label = format!(
            "Pick {number}: (z, y, x) = ({}, {}, {}) voxels, value {}, c={}, t={}",
            position[0], position[1], position[2], pick.value, pick.channel, pick.t
        );
        if let Some(t) = pick.time {
            label.push_str(&format!(" ({t} s)"));
        }
        if let Some(s) = spacing {
            label.push_str(&format!(
                "; position ({}, {}, {}) {}",
                position[0] * s[0],
                position[1] * s[1],
                position[2] * s[2],
                unit
            ));
        }
        label.push_str(&format!("; level {}, refined={}", pick.level, pick.refined));
        {
            let mut m = self.model.lock().unwrap();
            m.label = label.clone();
            m.plot = Some(png.clone());
        }
        // Panel publication has its own outcome; the pick above succeeded.
        // The SDK holds the plot if the panel has never opened.
        let profile = ctx
            .push_image("profile", "plot", &png)
            .and_then(|()| ctx.panel("profile").set_value("pick", label));
        if let Err(error) = profile {
            if !ctx.is_shutting_down() {
                eprintln!("profile panel update failed: {error}");
            }
        }
        Ok(format!("Pick {number}: value {value}"))
    }
    fn command(&self, ctx: &Context, c: &pb::InvokeCommand) -> Result<String> {
        match c.command_id.as_str() {
            "probe.undo" => self.edit(ctx, "undo"),
            "probe.clear" => self.edit(ctx, "clear"),
            "probe.set_radius" => {
                let value = c.args.get("value").and_then(|v| v.kind.as_ref());
                let radius = match value {
                    Some(Kind::S(s)) => s.trim().parse::<f64>().ok(),
                    Some(Kind::F(v)) => Some(*v),
                    Some(Kind::I(v)) => Some(*v as f64),
                    _ => None,
                };
                if let Some(radius) = radius.filter(|r| r.is_finite() && *r > 0.) {
                    self.model.lock().unwrap().radius = radius;
                    self.restore(ctx, "probe").map_err(|e| e.to_string())?;
                    Ok(format!("ROI radius {radius} voxels"))
                } else {
                    self.restore(ctx, "probe").map_err(|e| e.to_string())?;
                    Err("ROI radius must be positive and finite".into())
                }
            }
            "probe.set_refine" => {
                let Some(Kind::B(refine)) = c.args.get("value").and_then(|v| v.kind.as_ref())
                else {
                    return Err("Refine must be a boolean".into());
                };
                self.model.lock().unwrap().refine = *refine;
                self.restore(ctx, "probe").map_err(|e| e.to_string())?;
                Ok(format!("Refine at level 0: {refine}"))
            }
            "probe.contribute" => {
                let rois = {
                    let m = self.model.lock().unwrap();
                    m.picks
                        .iter()
                        .enumerate()
                        .map(|(i, p)| roi::ellipsoid(p.position, m.radius, i + 1))
                        .collect::<Vec<_>>()
                };
                let count = rois.len();
                let reply = ctx
                    .request(Request::ContributeRois(pb::ContributeRois {
                        rois,
                        replace_all: true,
                    }))
                    .map_err(|e| e.to_string())?;
                if !matches!(reply.message, Host::RoisContributed(_)) {
                    return Err("Expected RoisContributed".into());
                }
                Ok(if count == 0 {
                    "Cleared the contributed ROIs".into()
                } else {
                    format!("Contributed {count} ROIs")
                })
            }
            _ => Err("Unknown command".into()),
        }
    }
}
impl Worker {
    pub fn run(self, app: Arc<App>, ctx: Context) {
        while !app.stop.load(Ordering::Relaxed) && !ctx.is_shutting_down() {
            let job = match self.0.recv_timeout(Duration::from_millis(25)) {
                Ok(j) => j,
                Err(mpsc::RecvTimeoutError::Timeout) => continue,
                Err(_) => break,
            };
            let (result, reply, pick) = match job {
                Job::Pick(input) => (app.pick(&ctx, *input), None, true),
                Job::Edit(action) => (app.edit(&ctx, action), None, false),
                Job::Command(c, tx) => (app.command(&ctx, &c), Some(tx), false),
            };
            app.status(
                &ctx,
                match &result {
                    Ok(s) | Err(s) => s,
                },
            );
            if pick {
                app.busy.store(false, Ordering::Release);
            }
            if let Some(tx) = reply {
                let _ = tx.send(done(result));
            }
        }
    }
}
fn channel(input: &Input) -> Option<u32> {
    (0..15)
        .filter(|i| input.event.channels & (1 << i) != 0)
        .chain(
            input
                .state
                .iter()
                .flat_map(|s| s.pane.channels_high.iter().copied())
                .filter(|_| input.event.channels & 0x8000 != 0),
        )
        .min()
}
fn scale(input: &Input) -> Result<Point> {
    let s = input
        .state
        .as_ref()
        .and_then(|s| s.dataset.geometry.as_ref())
        .and_then(|g| g.levels.get(input.event.level as usize))
        .ok_or("Undeclared level")?;
    Ok([s.z, s.y, s.x])
}
fn read(
    ctx: &Context,
    t: u32,
    c: u32,
    level: u32,
    generation: u32,
    bounds: Bounds,
) -> Result<Vec<f32>> {
    let convert = |p: Cell| -> Result<[u32; 3]> {
        Ok([
            u32::try_from(p[0]).map_err(|e| e.to_string())?,
            u32::try_from(p[1]).map_err(|e| e.to_string())?,
            u32::try_from(p[2]).map_err(|e| e.to_string())?,
        ])
    };
    let roi = (convert(bounds.0)?, convert(bounds.1)?);
    let v = ctx
        .read_volume(t, c, level, Some(roi))
        .map_err(|e| e.to_string())?;
    if v.open_generation as u32 != generation {
        return Err("The dataset changed".into());
    }
    Ok(v.data.into_f32())
}
pub fn declare(app: &Arc<App>) -> std::result::Result<DeclaredPlugin, String> {
    let mut plugin = crate::declaration::builder();
    let commands = crate::declaration::commands().map(|spec| {
        let app = app.clone();
        plugin.command(spec, move |_ctx, c| {
            let (tx, rx) = mpsc::channel();
            {
                let _submit = app.submission.lock().unwrap();
                app.tx
                    .send(Job::Command(c.clone(), tx))
                    .map_err(|_| vizi::control::Error::internal("Worker stopped"))?;
            }
            // SDK dispatches command callbacks on its request worker, not the listener.
            rx.recv()
                .map_err(|_| vizi::control::Error::internal("Worker stopped"))
        })
    });
    let input = app.clone();
    let cancel = app.clone();
    let panel = app.clone();
    let active = app.clone();
    let inactive = app.clone();
    let shutdown = app.clone();
    plugin
        .on_input(move |ctx, i| input.input(ctx, i))
        .on_cancel(move |ctx, _| cancel.clear_aim(ctx))
        .on_panel_opened(move |ctx, p| {
            match p.panel_id.as_str() {
                "probe" => panel.probe_open.store(true, Ordering::Release),
                "profile" => panel.profile_open.store(true, Ordering::Release),
                _ => return Ok(()),
            }
            panel.restore(ctx, &p.panel_id)
        })
        .on_activated(move |ctx, b| {
            active.status(ctx, activation_status(&b));
            Ok(())
        })
        .on_deactivated(move |ctx, _| {
            inactive.status(ctx, "Ready");
            Ok(())
        })
        .on_shutdown(move |_, _| {
            shutdown.stop.store(true, Ordering::Relaxed);
            Ok(())
        });
    crate::declaration::panels(&mut plugin, commands);
    plugin.build()
}

pub(crate) fn activation_status(binding: &pb::ToolBinding) -> String {
    let triggers = binding
        .triggers
        .iter()
        .map(|t| {
            let name = if !t.key.is_empty() {
                t.key.clone()
            } else {
                let mut name = String::new();
                for (bit, label) in [(1, "Shift+"), (2, "Ctrl+"), (4, "Alt+"), (8, "Meta+")] {
                    if t.mods & bit != 0 {
                        name.push_str(label);
                    }
                }
                name.push_str(match t.button {
                    1 => "left button",
                    2 => "right button",
                    3 => "middle button",
                    _ => "mouse button",
                });
                name
            };
            format!(
                "{name} {}",
                if t.hold == pb::TriggerHold::Momentary as i32 {
                    "holds"
                } else {
                    "toggles"
                }
            )
        })
        .collect::<Vec<_>>();
    let actions = binding
        .actions
        .iter()
        .filter(|a| !a.key.is_empty())
        .map(|a| format!("{}: {}", a.action_id, a.key))
        .collect::<Vec<_>>();
    format!(
        "Probe active; drag in a view and release to pick; honoured triggers: {}; action keys: {}",
        triggers.join(", "),
        actions.join(", ")
    )
}
