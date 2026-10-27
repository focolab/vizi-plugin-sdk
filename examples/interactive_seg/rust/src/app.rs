use crate::{
    analysis::{self, Labels},
    overlay, plot, threshold,
};
use pb::{host_msg::Msg as Host, patch_op::Op, plugin_msg::Msg as Request, value::Kind};
use std::{
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc::{self, Receiver, SyncSender},
        Arc, Mutex,
    },
    time::Duration,
};
use vizi::{
    control::{Context, Input},
    declare::DeclaredPlugin,
    proto as pb, InputEvent, PaneEpoch, Phase, Position,
};
type Result<T> = std::result::Result<T, String>;
const ACTIVE: &str = "Lasso active — drag a loop in a 2D view";
const MIP: &str = "Turn off Z-MIP — the tool needs to know which plane you are pointing at.";
const LEVEL: &str = "Zoom in to full resolution — the tool segments level-0 voxels.";
const CHANNELS: &str = "Show a single channel — the tool cannot tell which one you mean.";
const BUSY: &str = "Busy — a job is already running.";

#[derive(Clone)]
struct Settings {
    min_size: usize,
    scale: f64,
    grow_3d: bool,
    add: bool,
}
impl Default for Settings {
    fn default() -> Self {
        Self {
            min_size: 50,
            scale: 1.,
            grow_3d: true,
            add: false,
        }
    }
}
struct Model {
    settings: Settings,
    status: String,
    seg: Option<Arc<Seg>>,
    regions: Vec<pb::RoiRecord>,
    region: usize,
    plot: Option<Vec<u8>>,
}
impl Default for Model {
    fn default() -> Self {
        Self {
            settings: Settings::default(),
            status: "Ready".into(),
            seg: None,
            regions: vec![],
            region: 0,
            plot: None,
        }
    }
}
struct Seg {
    labels: Labels,
    t: u32,
    c: u32,
    generation: u64,
}
struct Gesture {
    event: InputEvent,
    path: Vec<[f64; 3]>,
    z: Option<usize>,
    has_context: bool,
    c: u32,
    shape: Option<[usize; 3]>,
    refusal: Option<&'static str>,
    preview_failed: bool,
}
impl Gesture {
    fn observe(&mut self, input: &Input) {
        if !input.usable_context {
            return;
        }
        let e = &input.event;
        if !self.has_context {
            self.has_context = true;
            // Pane/interaction remain those of the press.
            self.event.t = e.t;
            self.event.level = e.level;
            self.event.channels = e.channels;
            self.event.gen = e.gen;
            let mut channels: Vec<u32> = (0..15).filter(|i| e.channels & (1 << i) != 0).collect();
            if e.channels & 0x8000 != 0 {
                if let Some(state) = &input.state {
                    channels.extend(&state.pane.channels_high);
                } else {
                    channels.extend([u32::MAX, u32::MAX]);
                }
            }
            self.c = channels.first().copied().unwrap_or(0);
            self.refusal = if matches!(
                e.position,
                Position::ColumnYx { .. } | Position::ColumnZx { .. } | Position::ColumnZy { .. }
            ) {
                Some(MIP)
            } else if e.level != 0 {
                Some(LEVEL)
            } else if channels.len() != 1 {
                Some(CHANNELS)
            } else {
                None
            };
            self.shape = input
                .state
                .as_ref()
                .and_then(|s| s.dataset.geometry.as_ref())
                .and_then(|g| g.extent.as_ref())
                .map(|e| [e.z_n as usize, e.y_n as usize, e.x_n as usize]);
        }
        if let Position::Voxel(p) = e.position {
            self.z
                .get_or_insert_with(|| p[0].round_ties_even().max(0.) as usize);
            if !e.miss {
                self.path.push(p.map(f64::from));
            }
        }
    }
}
enum Job {
    Segment(Box<Gesture>, Settings),
    Commit,
    Measure,
}
pub struct Busy(Arc<AtomicBool>);
impl Drop for Busy {
    fn drop(&mut self) {
        self.0.store(false, Ordering::Release);
    }
}
struct Work {
    job: Job,
    _busy: Busy,
}

pub struct App {
    model: Mutex<Model>,
    publication: Mutex<()>,
    gesture: Mutex<Option<Gesture>>,
    busy: Arc<AtomicBool>,
    pub stop: AtomicBool,
    panel_open: AtomicBool,
    trail_revision: AtomicU64,
    label_revision: AtomicU64,
    sender: SyncSender<Work>,
}
pub struct Worker(Receiver<Work>);
pub fn new() -> (Arc<App>, Worker) {
    let (sender, receiver) = mpsc::sync_channel(1);
    (
        Arc::new(App {
            model: Mutex::default(),
            publication: Mutex::default(),
            gesture: Mutex::default(),
            busy: Arc::new(AtomicBool::new(false)),
            stop: AtomicBool::new(false),
            panel_open: AtomicBool::new(false),
            trail_revision: AtomicU64::new(1),
            label_revision: AtomicU64::new(1),
            sender,
        }),
        Worker(receiver),
    )
}
fn op(widget: &str, op: Op) -> pb::PatchOp {
    pb::PatchOp {
        row_key: String::new(),
        widget_id: widget.into(),
        op: Some(op),
    }
}
fn value(widget: &str, kind: Kind) -> pb::PatchOp {
    op(widget, Op::SetValue(pb::Value { kind: Some(kind) }))
}
fn done(ok: bool, message: impl Into<String>) -> pb::CommandDone {
    pb::CommandDone {
        ok,
        message: message.into(),
        ..Default::default()
    }
}

impl App {
    fn status(&self, ctx: &Context, text: impl Into<String>) {
        let mut model = self.model.lock().unwrap();
        model.status = text.into();
        if !ctx.is_shutting_down() && self.panel_open.load(Ordering::Acquire) {
            if let Err(e) = ctx.panel("seg").set_value("status", model.status.clone()) {
                eprintln!("status: {e}");
            }
        }
    }
    fn restore(&self, ctx: &Context) -> vizi::control::Result<()> {
        if !self.panel_open.load(Ordering::Acquire) {
            return Ok(());
        }
        let model = self.model.lock().unwrap();
        let options = if model.regions.is_empty() {
            vec!["(none)".into()]
        } else {
            model.regions.iter().map(|r| r.name.clone()).collect()
        };
        let selected = model
            .regions
            .get(model.region)
            .map_or("(none)", |r| r.name.as_str());
        ctx.patch(
            "seg",
            vec![
                value("size", Kind::I(model.settings.min_size as i64)),
                value("thresh", Kind::F(model.settings.scale)),
                value("grow3d", Kind::B(model.settings.grow_3d)),
                value(
                    "mode",
                    Kind::S(if model.settings.add { "Add" } else { "Replace" }.into()),
                ),
                op("commit", Op::SetEnabled(model.seg.is_some())),
                op("rmeasure", Op::SetVisible(!model.regions.is_empty())),
                op("region", Op::SetOptions(pb::Options { options })),
                value("region", Kind::S(selected.into())),
                value("status", Kind::S(model.status.clone())),
            ],
        )?;
        if let Some(png) = &model.plot {
            ctx.push_image("seg", "plot", png)?;
        }
        Ok(())
    }
    fn acquire(&self) -> Result<Busy> {
        if self.stop.load(Ordering::Relaxed) {
            return Err("Plugin is shutting down".into());
        }
        self.busy
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .map_err(|_| BUSY.to_string())?;
        Ok(Busy(self.busy.clone()))
    }
    fn remove(&self, ctx: &Context, id: &str) -> Result<()> {
        let rev = if id == "seg.trail" {
            &self.trail_revision
        } else {
            &self.label_revision
        };
        ctx.push(Request::OverlayRemove(pb::OverlayRemove {
            overlay_id: id.into(),
            revision: rev.fetch_add(1, Ordering::Relaxed),
        }))
        .map_err(|e| e.to_string())
    }
    fn clear_gesture(&self, ctx: &Context) -> Result<()> {
        self.gesture.lock().unwrap().take();
        self.remove(ctx, "seg.trail")
    }
    fn input(&self, ctx: &Context, input: Input) -> vizi::control::Result<()> {
        let e = &input.event;
        if input
            .binding
            .as_ref()
            .is_none_or(|b| b.tool_id != "seg.lasso")
        {
            return Ok(());
        }
        if e.phase == Phase::Cancel {
            return self
                .clear_gesture(ctx)
                .map_err(vizi::control::Error::internal);
        }
        if e.phase == Phase::Fire {
            return Ok(());
        }
        if input.implicitly_closed.is_some() {
            self.clear_gesture(ctx)
                .map_err(vizi::control::Error::internal)?;
        }
        let mut held = self.gesture.lock().unwrap();
        if e.phase == Phase::Press {
            *held = Some(Gesture {
                event: e.clone(),
                z: None,
                has_context: false,
                c: 0,
                path: vec![],
                shape: None,
                refusal: None,
                preview_failed: false,
            });
        }
        if let Some(g) = held.as_mut() {
            if e.interaction != g.event.interaction {
                return Ok(());
            }
            g.observe(&input);
        }
        if e.phase == Phase::Release {
            let gesture = held.take();
            drop(held);
            self.remove(ctx, "seg.trail")
                .map_err(vizi::control::Error::internal)?;
            if let Some(g) = gesture {
                if !g.has_context {
                    return Ok(());
                }
                if let Some(reason) = g.refusal {
                    self.status(ctx, reason);
                    return Ok(());
                }
                let busy = match self.acquire() {
                    Ok(b) => b,
                    Err(e) => {
                        self.status(ctx, e);
                        return Ok(());
                    }
                };
                let settings = self.model.lock().unwrap().settings.clone();
                if self
                    .sender
                    .try_send(Work {
                        job: Job::Segment(Box::new(g), settings),
                        _busy: busy,
                    })
                    .is_err()
                {
                    self.status(ctx, "Segmentation worker unavailable");
                }
            }
        } else if let Some(g) = held.as_mut() {
            if g.refusal.is_none() && !g.path.is_empty() {
                let update = overlay::trail(
                    &g.path,
                    g.z.unwrap(),
                    self.trail_revision.fetch_add(1, Ordering::Relaxed),
                );
                // Capture the press context for the whole gesture, including preview.
                if let Err(error) = overlay::publish_trail(
                    ctx,
                    &update,
                    PaneEpoch {
                        view_id: g.event.view_id,
                        epoch: g.event.epoch,
                    },
                ) {
                    if !g.preview_failed {
                        g.preview_failed = true;
                        self.status(ctx,format!("Preview unavailable: {error}; segmentation will still run on release."));
                    }
                }
            }
        }
        Ok(())
    }
    fn command(
        &self,
        ctx: &Context,
        command: &pb::InvokeCommand,
    ) -> vizi::control::Result<pb::CommandDone> {
        let answer = self.command_inner(ctx, command);
        Ok(match answer {
            Ok(d) => d,
            Err(e) => {
                self.status(ctx, &e);
                done(false, e)
            }
        })
    }
    fn command_inner(&self, ctx: &Context, command: &pb::InvokeCommand) -> Result<pb::CommandDone> {
        let v = command.args.get("value").and_then(|v| v.kind.as_ref());
        let mut model = self.model.lock().unwrap();
        match command.command_id.as_str() {
            "seg.set_min_size" => match v {
                Some(Kind::I(n)) if (1..=5000).contains(n) => model.settings.min_size = *n as usize,
                _ => return Err("Minimum size must be an integer in [1, 5000]".into()),
            },
            "seg.set_threshold" => match v {
                Some(Kind::F(n)) if n.is_finite() && (0.5..=2.).contains(n) => {
                    model.settings.scale = *n
                }
                _ => return Err("Threshold scale must be in [0.5, 2.0]".into()),
            },
            "seg.set_3d" => match v {
                Some(Kind::B(b)) => model.settings.grow_3d = *b,
                _ => return Err("Expected a checkbox value".into()),
            },
            "seg.set_mode" => match v {
                Some(Kind::S(s)) if s == "Add" || s == "Replace" => model.settings.add = s == "Add",
                _ => return Err("Expected Replace or Add".into()),
            },
            "seg.set_region" => match command.args.get("index").and_then(|v| v.kind.as_ref()) {
                Some(Kind::I(i)) if *i >= 0 && (*i as usize) < model.regions.len().max(1) => {
                    model.region = *i as usize
                }
                _ => return Err("Invalid region index".into()),
            },
            "seg.reset" => {
                drop(model);
                let _publication = self.publication.lock().unwrap();
                self.clear_gesture(ctx)?;
                self.remove(ctx, "seg.labels")?;
                let mut model = self.model.lock().unwrap();
                model.seg = None;
                model.status = "Cleared".into();
                drop(model);
                if self.panel_open.load(Ordering::Acquire) {
                    ctx.patch(
                        "seg",
                        vec![
                            op("commit", Op::SetEnabled(false)),
                            value("status", Kind::S("Cleared".into())),
                        ],
                    )
                    .map_err(|e| e.to_string())?;
                }
                return Ok(done(true, "Cleared"));
            }
            "seg.pick" => {
                drop(model);
                let _busy = self.acquire()?;
                return self.pick(ctx).map(|message| done(true, message));
            }
            id @ ("seg.commit" | "seg.measure") => {
                if id == "seg.commit" && model.seg.is_none() {
                    return Err("No segmentation to commit".into());
                }
                if id == "seg.measure" && model.regions.get(model.region).is_none() {
                    return Err("No committed region selected".into());
                }
                drop(model);
                let busy = self.acquire()?;
                let job = match id {
                    "seg.commit" => Job::Commit,
                    "seg.measure" => Job::Measure,
                    _ => unreachable!(),
                };
                let sender = self.sender.clone();
                // The callback only queues work; the SDK orders it after acceptance.
                ctx.after_reply(move |_| {
                    sender
                        .try_send(Work { job, _busy: busy })
                        .map_err(|_| vizi::control::Error::internal("worker unavailable"))
                })
                .map_err(|e| e.to_string())?;
                return Ok(done(true, "Started"));
            }
            _ => return Err("Unknown command".into()),
        }
        Ok(done(true, ""))
    }
    fn segment(&self, ctx: &Context, g: Gesture, settings: Settings) -> Result<String> {
        let shape = match g.shape {
            Some(s) => s,
            None => {
                let v = view(ctx)?;
                v.shape_tczyx[2..]
                    .try_into()
                    .map(|a: [u32; 3]| a.map(|n| n as usize))
                    .map_err(|_| "Invalid volume shape")?
            }
        };
        let selection = analysis::select(&g.path, shape)?;
        self.status(ctx, "Reading selection…");
        let (w, generation) = read_volume(ctx, g.event.t, g.c, selection.origin, selection.shape)?;
        if generation as u32 != g.event.gen {
            return Err("Dataset changed during segmentation".into());
        }
        let threshold =
            threshold::threshold(&w, selection.shape, &selection.selected, settings.scale)
                .map_err(str::to_string)?;
        let component = analysis::grow(
            &w,
            &selection,
            g.z.ok_or("No usable voxel in gesture")?,
            threshold,
            settings.grow_3d,
            settings.min_size,
            &self.stop,
        )?;
        // Reset and the final merge/publish serialize, but computation and host
        // reads never hold this lock. A reset during a job starts accumulation
        // afresh when that job reaches this point.
        let _publication = self.publication.lock().unwrap();
        let previous = self.model.lock().unwrap().seg.clone();
        let same = previous
            .as_ref()
            .is_some_and(|s| (s.t, s.c, s.generation) == (g.event.t, g.c, generation));
        let empty = Labels::default();
        let old = previous.as_ref().map_or(&empty, |s| &s.labels);
        let (labels, id) = old.add(&component.voxels, !settings.add || !same)?;
        if self.stop.load(Ordering::Relaxed) {
            return Err("Canceled".into());
        }
        let revision = self.label_revision.fetch_add(1, Ordering::Relaxed);
        let reserved = overlay::label_item(ctx, &labels, g.event.t, revision)?;
        ctx.push_with_loans(
            Request::OverlayUpdate(pb::OverlayUpdate {
                overlay_id: "seg.labels".into(),
                items: vec![reserved.item],
                revision,
            }),
            reserved.loans,
        )
        .map_err(|e| e.to_string())?;
        self.model.lock().unwrap().seg = Some(Arc::new(Seg {
            labels,
            t: g.event.t,
            c: g.c,
            generation,
        }));
        if self.panel_open.load(Ordering::Acquire) {
            ctx.panel("seg")
                .set_enabled("commit", true)
                .map_err(|e| e.to_string())?;
        }
        Ok(format!(
            "Label {id}: {} voxels at threshold {}{}{}",
            component.voxels.len(),
            component.threshold,
            if component.clipped {
                "; clipped by the search window"
            } else {
                ""
            },
            if previous.is_some() && !same {
                "; new timepoint, channel, or dataset: accumulation restarted"
            } else {
                ""
            }
        ))
    }
    fn refresh_regions(&self, ctx: &Context) -> Result<()> {
        let regions = ctx.get_rois().map_err(|e| e.to_string())?;
        let mut model = self.model.lock().unwrap();
        model.regions = regions
            .into_iter()
            .filter(|r| r.origin == "plugin:org.example.interactive_seg")
            .collect();
        model.region = 0;
        drop(model);
        self.restore(ctx).map_err(|e| e.to_string())
    }
    fn commit(&self, ctx: &Context) -> Result<String> {
        let seg = self
            .model
            .lock()
            .unwrap()
            .seg
            .clone()
            .ok_or("No segmentation")?;
        let current = view(ctx)?;
        if current.open_generation != seg.generation {
            return Err("Dataset changed; draw a new segmentation before committing".into());
        }
        // Reserve again: a push's one-use loans can already have been released.
        let reserved = overlay::label_item(
            ctx,
            &seg.labels,
            seg.t,
            self.label_revision.load(Ordering::Relaxed),
        )?;
        let reply = ctx.request(Request::CompileLabelVolume(pb::CompileLabelVolume {
            overlay_id: "seg.labels".into(),
            item: Some(reserved.item),
            replace_all: true,
        }));
        // Only reservations never admitted can be reclaimed; sent ones stay lent.
        if reply.is_err() {
            for loan in reserved.loans {
                let _ = ctx.memory().cancel_unsent(loan);
            }
        }
        let reply = reply.map_err(|e| e.to_string())?;
        let Host::LabelVolumeCompiled(compiled) = reply.message else {
            return Err("Expected LabelVolumeCompiled".into());
        };
        self.remove(ctx, "seg.labels")?;
        self.refresh_regions(ctx)?;
        Ok(format!("Committed {} regions", compiled.region_count))
    }
    #[allow(
        deprecated,
        reason = "Exercise channel policy awaits coordinated migration from ViewState.c"
    )]
    fn measure(&self, ctx: &Context) -> Result<String> {
        let region = {
            let model = self.model.lock().unwrap();
            model.regions.get(model.region).cloned()
        };
        let Some(region) = region else {
            self.refresh_regions(ctx)?;
            return Ok("Region list refreshed; select a region and measure again.".into());
        };
        let mut reply = match ctx.request(Request::ReadMaskVoxels(pb::ReadMaskVoxels {
            region_id: region.region_id.clone(),
        })) {
            Ok(r) => r,
            Err(e) => {
                self.refresh_regions(ctx)?;
                return Err(format!("Region list refreshed: {e}"));
            }
        };
        let Host::MaskVoxels(mask) = &reply.message else {
            return Err("Expected MaskVoxels".into());
        };
        let descriptor = mask.segment.clone().ok_or("Missing mask descriptor")?;
        let generation = mask.open_generation;
        let origin: [usize; 3] = mask
            .origin_zyx
            .clone()
            .try_into()
            .map(|a: [u32; 3]| a.map(|v| v as usize))
            .map_err(|_| "Invalid mask origin")?;
        let shape = shape_of(&descriptor)?;
        if descriptor.dtype != pb::DType::DtypeU8 as i32 {
            return Err("Mask must contain u8 membership".into());
        }
        let bytes = reply.copy_payload().map_err(|e| e.to_string())?.bytes;
        let view = view(ctx)?;
        if view.open_generation != generation {
            return Err("Dataset changed during measurement".into());
        }
        let count = bytes.iter().filter(|&&v| v != 0).count();
        if count == 0 {
            return Err("Mask is empty".into());
        }
        let mut means = Vec::new();
        let channel = view.visible_channels.first().copied().unwrap_or(0);
        for t in 0..view.shape_tczyx[0] {
            if self.stop.load(Ordering::Relaxed) {
                return Err("Canceled".into());
            }
            self.status(
                ctx,
                format!(
                    "Measuring {}: {}/{}",
                    region.name,
                    t + 1,
                    view.shape_tczyx[0]
                ),
            );
            let (w, gen) = read_volume(ctx, t, channel, origin, shape)?;
            if gen != generation {
                return Err("Dataset changed during measurement".into());
            }
            means.push(
                w.iter()
                    .zip(&bytes)
                    .filter(|(_, b)| **b != 0)
                    .map(|(&v, _)| f64::from(v))
                    .sum::<f64>()
                    / count as f64,
            );
        }
        let png = plot::render(&means);
        let mut model = self.model.lock().unwrap();
        model.plot = Some(png);
        if self.panel_open.load(Ordering::Acquire) {
            ctx.push_image("seg", "plot", model.plot.as_ref().unwrap())
                .map_err(|e| e.to_string())?;
        }
        drop(model);
        Ok(format!(
            "Measured {} over {} timepoints",
            region.name,
            means.len()
        ))
    }
    fn pick(&self, ctx: &Context) -> Result<String> {
        if ctx
            .view_state_timeout(Duration::from_secs(2))
            .map_err(|e| e.to_string())?
            .mip
        {
            return Err(MIP.into());
        }
        let reply = ctx
            .request_timeout(
                Request::ActivateTool(pb::ActivateTool {
                    tool_id: "seg.lasso".into(),
                    deactivate: false,
                }),
                Duration::from_secs(2),
            )
            .map_err(|e| e.to_string())?;
        let Host::ToolActivationChanged(change) = &reply.message else {
            return Err("Expected ToolActivationChanged".into());
        };
        if !change.active {
            return Err("The host declined to activate the lasso".into());
        }
        self.status(ctx, ACTIVE);
        Ok(ACTIVE.into())
    }
}

impl Worker {
    pub fn run(self, app: Arc<App>, ctx: Context) {
        while !app.stop.load(Ordering::Relaxed) && !ctx.is_shutting_down() {
            let work = match self.0.recv_timeout(Duration::from_millis(50)) {
                Ok(w) => w,
                Err(mpsc::RecvTimeoutError::Timeout) => continue,
                Err(_) => break,
            };
            let result =
                std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| match work.job {
                    Job::Segment(g, s) => app.segment(&ctx, *g, s),
                    Job::Commit => app.commit(&ctx),
                    Job::Measure => app.measure(&ctx),
                }))
                .unwrap_or_else(|_| Err("Worker panicked".into()));
            app.status(
                &ctx,
                match result {
                    Ok(s) => s,
                    Err(e) => e,
                },
            );
            drop(work._busy);
        }
    }
}
pub fn declare(app: &Arc<App>) -> std::result::Result<DeclaredPlugin, String> {
    let mut plugin = crate::declaration::builder();
    let commands = crate::declaration::commands().map(|spec| {
        let app = app.clone();
        plugin.command(spec, move |ctx, command| app.command(ctx, command))
    });
    let input = app.clone();
    let cancel = app.clone();
    let panel = app.clone();
    let shutdown = app.clone();
    let activated = app.clone();
    let deactivated = app.clone();
    plugin
        .on_activated(move |ctx, binding| {
            if binding.tool_id == "seg.lasso" {
                activated.status(ctx, ACTIVE);
            }
            Ok(())
        })
        .on_deactivated(move |ctx, binding| {
            if binding.tool_id == "seg.lasso" {
                deactivated
                    .clear_gesture(ctx)
                    .map_err(vizi::control::Error::internal)?;
                deactivated.status(ctx, "Lasso inactive");
            }
            Ok(())
        })
        .on_input(move |ctx, event| input.input(ctx, event))
        .on_cancel(move |ctx, _| {
            cancel
                .clear_gesture(ctx)
                .map_err(vizi::control::Error::internal)
        })
        .on_panel_opened(move |ctx, event| {
            if event.panel_id == "seg" {
                panel.panel_open.store(true, Ordering::Release);
                panel.restore(ctx)
            } else {
                Ok(())
            }
        })
        .on_shutdown(move |_, _| {
            shutdown.stop.store(true, Ordering::Relaxed);
            Ok(())
        });
    crate::declaration::panels(&mut plugin, commands);
    plugin.build()
}
fn view(ctx: &Context) -> Result<pb::ViewState> {
    let v = ctx.view_state().map_err(|e| e.to_string())?;
    if v.shape_tczyx.len() != 5 || v.shape_tczyx.contains(&0) {
        return Err("No dataset is open".into());
    }
    Ok(v)
}
fn shape_of(d: &pb::SegmentRef) -> Result<[usize; 3]> {
    d.shape_zyx
        .clone()
        .try_into()
        .map(|a: [u32; 3]| a.map(|v| v as usize))
        .map_err(|_| "Invalid volume shape".into())
}
fn read_volume(
    ctx: &Context,
    t: u32,
    c: u32,
    origin: [usize; 3],
    shape: [usize; 3],
) -> Result<(Vec<f32>, u64)> {
    let lower = origin.map(|v| u32::try_from(v).map_err(|_| "ROI exceeds u32"));
    let lower = [lower[0]?, lower[1]?, lower[2]?];
    let mut upper = lower;
    for a in 0..3 {
        upper[a] = lower[a]
            .checked_add(u32::try_from(shape[a]).map_err(|_| "ROI exceeds u32")?)
            .ok_or("ROI exceeds u32")?;
    }
    let volume = ctx
        .read_volume(t, c, 0, Some((lower, upper)))
        .map_err(|e| e.to_string())?;
    Ok((volume.data.into_f32(), volume.open_generation))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn sample() -> Input {
        Input {
            event: InputEvent {
                gen: 4,
                t: 2,
                interaction: 1,
                seq: 0,
                epoch: 7,
                target: None,
                tool: 1,
                channels: 1,
                phase: Phase::Press,
                view_id: 3,
                level: 0,
                mods: 0,
                click_count: 1,
                miss: true,
                roll_ref_y: false,
                action: 0,
                position: Position::Voxel([6., -2., -2.]),
            },
            usable_context: true,
            implicitly_closed: None,
            state: None,
            binding: None,
        }
    }
    fn gesture(input: &Input) -> Gesture {
        Gesture {
            event: input.event.clone(),
            path: vec![],
            z: None,
            has_context: false,
            c: 0,
            shape: None,
            refusal: None,
            preview_failed: false,
        }
    }
    #[test]
    fn miss_press_fixes_plane_and_context_but_does_not_add_a_point() {
        let mut input = sample();
        let mut g = gesture(&input);
        g.observe(&input);
        assert!(g.has_context);
        assert_eq!(g.z, Some(6));
        assert!(g.path.is_empty());
        input.event.miss = false;
        input.event.t = 99;
        input.event.gen = 99;
        input.event.channels = 3;
        input.event.level = 2;
        input.event.view_id = 9;
        input.event.epoch = 9;
        input.event.position = Position::Voxel([8., 10., 10.]);
        g.observe(&input);
        assert_eq!(g.z, Some(6));
        assert_eq!((g.event.t, g.event.gen, g.c), (2, 4, 0));
        assert_eq!((g.event.view_id, g.event.epoch), (3, 7));
        assert_eq!(g.path, [[8., 10., 10.]]);
        assert!(g.refusal.is_none());
    }
    #[test]
    fn unusable_press_waits_for_context_and_projection_refuses() {
        let mut input = sample();
        input.usable_context = false;
        let mut g = gesture(&input);
        g.observe(&input);
        assert!(!g.has_context);
        assert_eq!(g.z, None);
        assert!(g.path.is_empty());
        input.usable_context = true;
        input.event.t = 3;
        input.event.miss = false;
        input.event.position = Position::ColumnYx {
            y: 2.,
            x: 2.,
            z_min: 0.,
            z_max: 12.,
        };
        g.observe(&input);
        assert!(g.has_context);
        assert_eq!(g.event.t, 3);
        assert_eq!(g.refusal, Some(MIP));
        assert!(g.path.is_empty());
    }
}
