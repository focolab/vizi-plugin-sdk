mod analysis;
mod declaration;
mod roi;
#[cfg(test)]
mod tests;

use pb::{host_msg::Msg as Host, plugin_msg::Msg as Request, value::Kind};
use std::{
    panic::{catch_unwind, AssertUnwindSafe},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    thread::JoinHandle,
    time::Duration,
};
use vizi::{
    control::Context,
    declare::{DeclaredPlugin, ManagementCommand},
    proto as pb,
};

const MODES: [&str; 2] = ["Current timepoint", "All timepoints"];

struct Model {
    extents: [String; 3],
    all: bool,
    status: String,
    busy: bool,
}
impl Default for Model {
    fn default() -> Self {
        Self {
            extents: ["4".into(), "8".into(), "8".into()],
            all: false,
            status: "Ready".into(),
            busy: false,
        }
    }
}
#[derive(Default)]
struct App {
    model: Mutex<Model>,
    stop: AtomicBool,
    panel_open: AtomicBool,
    worker: Mutex<Option<JoinHandle<()>>>,
}

fn value(widget: &str, text: impl Into<String>) -> pb::PatchOp {
    pb::PatchOp {
        row_key: String::new(),
        widget_id: widget.into(),
        op: Some(pb::patch_op::Op::SetValue(pb::Value {
            kind: Some(Kind::S(text.into())),
        })),
    }
}
fn done(ok: bool, message: impl Into<String>) -> pb::CommandDone {
    pb::CommandDone {
        ok,
        message: message.into(),
        ..Default::default()
    }
}

impl App {
    // Serialize model updates with patches, including callbacks on panel reopen.
    // Never hold this lock during a host request or computation.
    fn status(&self, ctx: &Context, status: String) {
        let mut model = self.model.lock().unwrap();
        model.status = status;
        if ctx.is_shutting_down() || !self.panel_open.load(Ordering::Acquire) {
            return;
        }
        if let Err(e) = ctx.patch("peak", vec![value("status", &model.status)]) {
            eprintln!("Peak ROI status: {e}");
        }
    }

    fn restore(&self, ctx: &Context) -> vizi::control::Result<()> {
        if !self.panel_open.load(Ordering::Acquire) {
            return Ok(());
        }
        let model = self.model.lock().unwrap();
        let mut ops: Vec<_> = ["ez", "ey", "ex"]
            .iter()
            .zip(&model.extents)
            .map(|(widget, text)| value(widget, text))
            .collect();
        ops.push(value("mode", MODES[usize::from(model.all)]));
        ops.push(value("status", &model.status));
        ctx.patch("peak", ops)
    }

    fn run(self: &Arc<Self>, ctx: &Context) -> pb::CommandDone {
        let (extents, all) = {
            let mut model = self.model.lock().unwrap();
            if model.busy {
                return done(
                    false,
                    "Analysis already in progress; busy with the current run",
                );
            }
            if self.stop.load(Ordering::Relaxed) || ctx.is_shutting_down() {
                return done(false, "Plugin is shutting down");
            }
            let parsed: Result<Vec<f64>, _> =
                model.extents.iter().map(|s| s.trim().parse()).collect();
            let extents: [f64; 3] = match parsed {
                Ok(v) => v.try_into().unwrap(),
                Err(_) => {
                    drop(model);
                    self.status(
                        ctx,
                        "Invalid extent: enter a finite, strictly positive number".into(),
                    );
                    return done(false, "Invalid extent");
                }
            };
            if let Err(e) = analysis::validate_extents(extents) {
                drop(model);
                self.status(ctx, e.clone());
                return done(false, e);
            }
            model.busy = true;
            (extents, model.all)
        };
        // A short preflight bounds the initial wait; supervised commands have no deadline.
        let view = match view_state(ctx, Duration::from_secs(2)) {
            Ok(view) => view,
            Err(e) => {
                self.model.lock().unwrap().busy = false;
                self.status(ctx, format!("Cannot start: {e}"));
                return done(false, e);
            }
        };
        if all && view.shape_tczyx[0] > i32::MAX as u32 {
            self.model.lock().unwrap().busy = false;
            return done(false, "Time domain exceeds ROI frame index range");
        }
        let channel = view.visible_channels.first().copied().unwrap_or(0);
        self.status(ctx, format!("Started: channel {channel}"));
        let app = Arc::clone(self);
        let context = ctx.clone();
        // The preceding worker has finished before busy becomes false. Join it
        // before replacing its handle; the shutdown hook only requests cancel.
        let mut worker = self.worker.lock().unwrap();
        if let Some(previous) = worker.take() {
            let _ = previous.join();
        }
        match std::thread::Builder::new()
            .name("peak-roi-analysis".into())
            .spawn(move || {
                let result = catch_unwind(AssertUnwindSafe(|| {
                    app.analyze(&context, &view, extents, all)
                }))
                .unwrap_or_else(|_| Err("Analysis worker panicked".into()));
                let message = match result {
                    Ok(message) => message,
                    Err(error) => format!("Analysis failed: {error}"),
                };
                let mut model = app.model.lock().unwrap();
                model.busy = false;
                model.status = message;
                if !context.is_shutting_down() && app.panel_open.load(Ordering::Acquire) {
                    if let Err(e) = context.patch("peak", vec![value("status", &model.status)]) {
                        eprintln!("Peak ROI status: {e}");
                    }
                }
            }) {
            Ok(handle) => {
                *worker = Some(handle);
                done(true, "Peak search started")
            }
            Err(e) => {
                self.model.lock().unwrap().busy = false;
                self.status(ctx, format!("Could not start worker: {e}"));
                done(false, e.to_string())
            }
        }
    }

    fn analyze(
        &self,
        ctx: &Context,
        view: &pb::ViewState,
        extents: [f64; 3],
        all: bool,
    ) -> analysis::Result<String> {
        let channel = view.visible_channels.first().copied().unwrap_or(0);
        let frames = if all {
            0..view.shape_tczyx[0]
        } else {
            view.t..view.t + 1
        };
        let total = frames.len();
        let cancelled = || self.stop.load(Ordering::Relaxed) || ctx.is_shutting_down();
        let mut peaks = Vec::new();
        for (i, t) in frames.enumerate() {
            if cancelled() {
                return Err("Analysis cancelled".into());
            }
            self.status(
                ctx,
                format!(
                    "Reading frame {}/{} (t={}, channel {})",
                    i + 1,
                    total,
                    t,
                    channel
                ),
            );
            let mut reply = ctx
                .request(Request::ReadDataVolume(pb::ReadDataVolume {
                    t,
                    c: channel,
                    level: 0,
                    ..Default::default()
                }))
                .map_err(|e| e.to_string())?;
            let Host::DataVolume(volume) = &reply.message else {
                return Err("Expected DataVolume".into());
            };
            if volume.open_generation != view.open_generation {
                return Err("Dataset changed during analysis".into());
            }
            if (volume.t, volume.c, volume.level) != (t, channel, 0) {
                return Err("Host returned a different frame, channel or level".into());
            }
            let descriptor = volume.segment.clone().ok_or("Missing volume descriptor")?;
            if descriptor.shape_zyx != view.shape_tczyx[2..] {
                return Err("Level-0 volume shape changed during analysis".into());
            }
            // SDK validates region bounds, copies, and releases the loan. Drop
            // also releases unread replies on every failure path above.
            let payload = reply.copy_payload().map_err(|e| e.to_string())?;
            let (values, shape) =
                analysis::decode(&payload.bytes, descriptor.dtype, &descriptor.shape_zyx)?;
            drop(payload);
            self.status(
                ctx,
                format!("Blurring frame {}/{} (channel {})", i + 1, total, channel),
            );
            let blurred = analysis::blur(values, shape, extents, cancelled)?;
            peaks.push(analysis::peak(&blurred, shape));
        }
        // A swap after the last read must not silently attach old geometry to
        // a new dataset. The protocol has no atomic generation condition on
        // ContributeRois; the final check narrows, but cannot close, that race.
        let current = view_state(ctx, Duration::from_secs(30))?;
        if current.open_generation != view.open_generation {
            return Err("Dataset changed before ROI contribution".into());
        }
        if cancelled() {
            return Err("Analysis cancelled".into());
        }
        ctx.request(Request::ContributeRois(pb::ContributeRois {
            rois: vec![roi::ellipsoid(&peaks, extents, all)],
            replace_all: false,
        }))
        .map_err(|e| e.to_string())?;
        Ok(format!(
            "Done: peak {:?}; {} timepoint(s), channel {}",
            peaks[0], total, channel
        ))
    }
}

fn view_state(ctx: &Context, timeout: Duration) -> analysis::Result<pb::ViewState> {
    let reply = ctx
        .request_timeout(Request::GetViewState(pb::GetViewState {}), timeout)
        .map_err(|e| format!("No dataset or view unavailable: {e}"))?;
    let Host::ViewState(view) = &reply.message else {
        return Err("Expected ViewState".into());
    };
    if view.shape_tczyx.len() != 5 || view.shape_tczyx.contains(&0) {
        return Err("No dataset is open".into());
    }
    let channel = view.visible_channels.first().copied().unwrap_or(0);
    if view.t >= view.shape_tczyx[0] || channel >= view.shape_tczyx[1] {
        return Err("View indices are outside the dataset".into());
    }
    Ok(view.clone())
}

fn declare(app: &Arc<App>) -> Result<DeclaredPlugin, String> {
    let mut plugin = declaration::builder();
    let [z, y, x, mode_spec, run_spec] = declaration::commands();
    let [z, y, x] = [(0, z), (1, y), (2, x)].map(|(axis, spec)| {
        let app = Arc::clone(app);
        plugin.command(spec, move |ctx, command| {
            let Some(Kind::S(text)) = command.args.get("value").and_then(|v| v.kind.as_ref())
            else {
                return Ok(done(false, "Expected line-edit text in value"));
            };
            let parsed = text.trim().parse::<f64>().ok();
            if parsed.is_none_or(|extent| analysis::validate_extents([extent; 3]).is_err()) {
                let message = format!(
                    "Invalid {} extent {:?}: expected a finite, strictly positive number with Gaussian radius at most 100000",
                    ["Z", "Y", "X"][axis], text.chars().take(80).collect::<String>()
                );
                app.status(ctx, message.clone());
                app.restore(ctx)?;
                return Ok(done(false, message));
            }
            app.model.lock().unwrap().extents[axis] = text.clone();
            Ok(done(true, ""))
        })
    });
    let mode = Arc::clone(app);
    let run = Arc::clone(app);
    let panel = Arc::clone(app);
    let shutdown = Arc::clone(app);
    let mode = plugin.command(mode_spec, move |_, command| {
        let Some(Kind::S(text)) = command.args.get("value").and_then(|v| v.kind.as_ref()) else {
            return Ok(done(false, "Expected timepoint mode in value"));
        };
        let Some(index) = MODES.iter().position(|&mode| mode == text) else {
            return Ok(done(false, "Unknown timepoint mode"));
        };
        mode.model.lock().unwrap().all = index == 1;
        Ok(done(true, ""))
    });
    let run = plugin.command(run_spec, move |ctx, _| Ok(run.run(ctx)));
    plugin
        .on_panel_opened(move |ctx, opened| {
            if opened.panel_id == "peak" {
                panel.panel_open.store(true, Ordering::Release);
                panel.restore(ctx)?;
            }
            Ok(())
        })
        .on_shutdown(move |_, _| {
            shutdown.stop.store(true, Ordering::Relaxed);
            Ok(())
        });
    declaration::panels(&mut plugin, [z, y, x, mode, run]);
    plugin.build()
}

#[tokio::main]
async fn main() -> std::process::ExitCode {
    vizi::declare::report_exit(run().await)
}

async fn run() -> Result<(), Box<dyn std::error::Error>> {
    let app = Arc::new(App::default());
    let Some(plugin) = ManagementCommand::prepare(
        declaration::ID,
        env!("CARGO_PKG_VERSION"),
        std::env::args_os().skip(1),
        || declare(&app),
    )?
    else {
        return Ok(());
    };
    // The launcher transfers exclusive ownership of the inherited doorbell fd.
    let runtime = unsafe { plugin.into_runtime_from_env() }?;
    let result = runtime.serve_env().await;
    app.stop.store(true, Ordering::Relaxed);
    // Session close wakes SDK requests; do not join inside the shutdown hook.
    if let Some(worker) = app.worker.lock().unwrap().take() {
        let _ = worker.join();
    }
    result?;
    Ok(())
}

#[cfg(test)]
mod declaration_tests {
    use super::*;
    #[test]
    fn manifest_matches_development_snapshot() {
        let plugin = declare(&Arc::new(App::default())).unwrap();
        let checked = vizi::manifest::Manifest::load(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/vizi-plugin.json"
        ))
        .unwrap();
        let mut expected = checked.value().clone();
        let mut actual = plugin.manifest().value().clone();
        expected.as_object_mut().unwrap().remove("entry");
        actual.as_object_mut().unwrap().remove("entry");
        assert_eq!(actual, expected);
    }
}
