use crate::{
    calibration,
    format::{self, error, Calibration, Crate, Header, Writer},
    offline,
};
use pb::{error::Kind, host_msg::Msg as Host, plugin_msg::Msg as Request};
use std::{
    fs::{self, File},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex, Weak,
    },
    thread::JoinHandle,
};
use vizi::{
    control::{Context, Error, Result},
    plugin::{Exporter, Reader, Volume},
    proto as pb,
};

struct Model {
    head: String,
    status: String,
    vox: String,
    cal: String,
}
pub struct App {
    model: Mutex<Model>,
    verify: AtomicBool,
    panel_open: AtomicBool,
    busy: AtomicBool,
    stopping: AtomicBool,
    active: Mutex<Weak<BridgeReader>>,
    exports: Mutex<Vec<Weak<BridgeExport>>>,
    workers: Mutex<Vec<JoinHandle<()>>>,
    override_calibration: Mutex<Option<(PathBuf, Calibration)>>,
}
fn done(ok: bool, message: impl Into<String>) -> pb::CommandDone {
    pb::CommandDone {
        ok,
        message: message.into(),
        ..Default::default()
    }
}
fn patch(id: &str, kind: pb::value::Kind) -> pb::PatchOp {
    pb::PatchOp {
        row_key: String::new(),
        widget_id: id.into(),
        op: Some(pb::patch_op::Op::SetValue(pb::Value { kind: Some(kind) })),
    }
}
impl App {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            model: Mutex::new(Model {
                head: "No crate open".into(),
                status: "Ready".into(),
                vox: "(unknown)".into(),
                cal: "(not provisioned)".into(),
            }),
            verify: AtomicBool::new(true),
            panel_open: AtomicBool::new(false),
            busy: AtomicBool::new(false),
            stopping: AtomicBool::new(false),
            active: Mutex::new(Weak::new()),
            exports: Mutex::new(Vec::new()),
            workers: Mutex::new(Vec::new()),
            override_calibration: Mutex::new(None),
        })
    }
    fn update(&self, ctx: &Context, change: impl FnOnce(&mut Model)) {
        let mut model = self.model.lock().unwrap();
        change(&mut model);
        if !self.panel_open.load(Ordering::Acquire) {
            return;
        }
        let mut ops: Vec<_> = [
            ("head", &model.head),
            ("status", &model.status),
            ("vox", &model.vox),
            ("cal", &model.cal),
        ]
        .into_iter()
        .map(|(id, s)| patch(id, pb::value::Kind::S(s.clone())))
        .collect();
        ops.push(patch(
            "verify",
            pb::value::Kind::B(self.verify.load(Ordering::Relaxed)),
        ));
        if let Err(e) = ctx.patch("crate", ops) {
            if !ctx.is_shutting_down() {
                eprintln!("panel patch: {e}");
            }
        }
    }
    fn calibration(&self, ctx: &Context) -> Option<Calibration> {
        if let Some((path, cal)) = self.override_calibration.lock().unwrap().as_ref() {
            self.update(ctx, |m| {
                m.cal = format!(
                    "override: {}",
                    path.file_name().unwrap_or_default().to_string_lossy()
                )
            });
            return Some(cal.clone());
        }
        let cal = ctx
            .asset("calibration")
            .ok()
            .and_then(|path| match Calibration::load(&path) {
                Ok(cal) => Some(cal),
                Err(e) => {
                    eprintln!("{e}");
                    None
                }
            });
        self.update(ctx, |m| {
            m.cal = if cal.is_some() {
                "manifest asset"
            } else {
                "(not provisioned)"
            }
            .into()
        });
        cal
    }
    pub fn declare(self: &Arc<Self>) -> std::result::Result<vizi::declare::DeclaredPlugin, String> {
        let mut plugin = crate::declaration::builder();
        let [verify_spec, describe_spec, pick_spec, check_spec] = crate::declaration::commands();
        let [reader_spec] = crate::declaration::readers();
        let [exporter_spec] = crate::declaration::exporters();
        let a = self.clone();
        let b = self.clone();
        let c = self.clone();
        let d = self.clone();
        let e = self.clone();
        let f = self.clone();
        let g = self.clone();
        let h = self.clone();
        plugin
            .reader(reader_spec, move |ctx, req| a.open_reader(ctx, req))
            .exporter(exporter_spec, move |ctx, req| b.open_export(ctx, req));
        let verify = plugin.command(verify_spec, move |ctx, req| {
            let Some(pb::value::Kind::B(value)) =
                req.args.get("value").and_then(|v| v.kind.as_ref())
            else {
                return Ok(done(false, "verify requires a boolean value"));
            };
            c.verify.store(*value, Ordering::Relaxed);
            c.update(ctx, |m| {
                m.status = format!(
                    "CRC-32 verification {} for volume reads; plane reads do not verify",
                    if *value { "enabled" } else { "disabled" }
                )
            });
            Ok(done(true, ""))
        });
        let describe = plugin.command(describe_spec, move |ctx, _| Ok(d.start_describe(ctx)));
        let pick = plugin.command(pick_spec, move |ctx, _| g.start_picker(ctx, true));
        let check = plugin.command(check_spec, move |ctx, _| h.start_picker(ctx, false));
        plugin
            .on_panel_opened(move |ctx, panel| {
                if panel.panel_id == "crate" {
                    e.panel_open.store(true, Ordering::Release);
                    e.calibration(ctx);
                }
                Ok(())
            })
            .on_shutdown(move |_, _| {
                f.shutdown();
                Ok(())
            });
        crate::declaration::panels(&mut plugin, [verify, describe, pick, check]);
        plugin.build()
    }
    fn open_reader(
        self: &Arc<Self>,
        ctx: &Context,
        req: &pb::OpenReader,
    ) -> Result<Arc<dyn Reader>> {
        let cal = self.calibration(ctx);
        let result = (|| {
            let file = Crate::open(Path::new(&req.uri))?;
            file.require_calibration(cal.as_ref())?;
            let header = file.header.clone();
            let invalid = file.invalid_utf8;
            let malformed_names = file.malformed_names;
            let reader = Arc::new(BridgeReader {
                file: Mutex::new(Some(file)),
                header: header.clone(),
                cal,
                app: Arc::downgrade(self),
                ctx: ctx.clone(),
            });
            *self.active.lock().unwrap() = Arc::downgrade(&reader);
            self.update(ctx, |m| {
                m.head = format!(
                    "{} — {:?}",
                    Path::new(&req.uri)
                        .file_name()
                        .unwrap_or_default()
                        .to_string_lossy(),
                    header.shape
                );
                m.vox = header.label();
                m.status = if invalid {
                    "Opened; metadata is not UTF-8 and will be carried as no document".into()
                } else if header.malformed_spacing {
                    "Opened; malformed header spacing treated as unknown".into()
                } else {
                    "Opened crate".into()
                };
                if malformed_names {
                    m.status
                        .push_str("; malformed channel names; none declared");
                }
            });
            Ok(reader as Arc<dyn Reader>)
        })();
        if let Err(e) = &result {
            self.update(ctx, |m| m.status = format!("Open failed: {e}"));
        }
        result
    }
    fn open_export(
        self: &Arc<Self>,
        ctx: &Context,
        req: &pb::OpenExport,
    ) -> Result<Arc<dyn Exporter>> {
        let doc = req.source_metadata.clone().unwrap_or_default();
        let resolved = calibration::resolve(
            [
                req.spacing_z,
                req.spacing_y,
                req.spacing_x,
                req.time_increment,
            ],
            &req.spacing_unit,
            &req.time_unit,
        );
        let shape = req
            .shape_tczyx
            .clone()
            .try_into()
            .map_err(|_| error(Kind::InvalidArgument, "export needs five axes"))?;
        let header = Header {
            dtype: req
                .dtype
                .try_into()
                .map_err(|_| Error::unsupported(format!("dtype {}", req.dtype)))?,
            flags: crate::format::DERIVED,
            shape,
            calibration: resolved.values,
            malformed_spacing: false,
        };
        header.validate()?;
        let path = PathBuf::from(&req.locator);
        let output = File::create(&path)
            .map_err(|e| error(Kind::IoError, format!("{}: {e}", path.display())))?;
        let names: Vec<_> = req
            .channels
            .iter()
            .map(|ch| {
                if ch.declared() {
                    ch.name.clone()
                } else {
                    String::new()
                }
            })
            .collect();
        let writer = match Writer::begin_sections(
            output,
            header,
            &doc.form,
            &doc.text,
            req.provenance.as_bytes(),
            &names,
        ) {
            Ok(w) => w,
            Err(e) => {
                fs::remove_file(&path).map_err(|cleanup| {
                    error(
                        Kind::IoError,
                        format!("{}: {e}; cleanup: {cleanup}", path.display()),
                    )
                })?;
                return Err(e);
            }
        };
        let exporter = Arc::new(BridgeExport {
            state: Mutex::new(ExportState {
                writer: Some(writer),
                times: Vec::new(),
                channels: Vec::new(),
                completed: false,
            }),
            path,
            little_endian: req.little_endian,
            resolved,
            app: Arc::downgrade(self),
            ctx: ctx.clone(),
        });
        let mut exports = self.exports.lock().unwrap();
        exports.retain(|e| e.strong_count() > 0);
        exports.push(Arc::downgrade(&exporter));
        if let Some(sources) = exporter.resolved.source_diagnostic() {
            eprintln!("{}: calibration {sources}", exporter.path.display());
        }
        self.update(ctx, |m| {
            m.status = format!("Writing {}", exporter.path.display())
        });
        Ok(exporter)
    }
    fn start_describe(self: &Arc<Self>, ctx: &Context) -> pb::CommandDone {
        if self.busy.swap(true, Ordering::SeqCst) {
            self.update(ctx, |m| m.status = "Busy — a job is already running".into());
            return done(false, "Busy — a job is already running");
        }
        let app = self.clone();
        let ctx = ctx.clone();
        let job = std::thread::Builder::new()
            .name("crate-describe".into())
            .spawn(move || {
                let result =
                    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| app.describe(&ctx)));
                let result =
                    result.unwrap_or_else(|_| Err(Error::internal("description worker panicked")));
                if let Err(e) = result {
                    app.update(&ctx, |m| m.status = format!("Describe failed: {e}"));
                }
                app.busy.store(false, Ordering::SeqCst);
            });
        match job {
            Ok(job) => {
                let mut jobs = self.workers.lock().unwrap();
                jobs.retain(|j| !j.is_finished());
                jobs.push(job);
                done(true, "Description accepted")
            }
            Err(e) => {
                self.busy.store(false, Ordering::SeqCst);
                done(false, e.to_string())
            }
        }
    }
    fn start_picker(self: &Arc<Self>, ctx: &Context, calibration: bool) -> Result<pb::CommandDone> {
        if self.busy.swap(true, Ordering::SeqCst) {
            return Ok(done(false, "Busy — a job is already running"));
        }
        let guard = BusyGuard(self.clone());
        ctx.after_reply(move |ctx| {
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                guard.0.pick(&ctx, calibration)
            }))
            .unwrap_or_else(|_| Err(Error::internal("file-picker worker panicked")));
            if let Err(e) = result {
                guard
                    .0
                    .update(&ctx, |m| m.status = format!("File operation failed: {e}"));
            }
            Ok(())
        })?;
        Ok(done(
            true,
            if calibration {
                "Choose a calibration table"
            } else {
                "Choose a crate to check"
            },
        ))
    }
    fn pick(&self, ctx: &Context, calibration: bool) -> Result<()> {
        let slot = if calibration {
            "calibration_override"
        } else {
            "crate_check_file"
        };
        let reply = ctx.request_timeout(
            Request::RequestAsset(pb::RequestAsset {
                slot: slot.into(),
                force_reprompt: true,
            }),
            std::time::Duration::from_secs(30 * 60),
        )?;
        let Host::AssetResult(asset) = &reply.message else {
            return Err(Error::protocol("expected AssetResult"));
        };
        if asset.declined {
            return Ok(());
        }
        if asset.path.is_empty() {
            return Err(Error::protocol("asset response has no path"));
        }
        let path = PathBuf::from(&asset.path);
        if calibration {
            let table = Calibration::load(&path)?;
            *self.override_calibration.lock().unwrap() = Some((path, table));
            self.calibration(ctx);
            self.update(ctx, |m| {
                m.status = "Calibration override accepted; used on the next calibrated open".into()
            });
        } else {
            let (_, message) = offline::check(&path)?;
            self.update(ctx, |m| m.status = message);
        }
        Ok(())
    }
    fn describe(&self, ctx: &Context) -> Result<()> {
        let reply = ctx.request(Request::GetViewState(pb::GetViewState {}))?;
        let Host::ViewState(view) = &reply.message else {
            return Err(Error::protocol("expected ViewState"));
        };
        let channel = view.visible_channels.first().copied().unwrap_or(0);
        let read = ctx.request(Request::ReadDataVolume(pb::ReadDataVolume {
            t: view.t,
            c: channel,
            level: 0,
            ..Default::default()
        }));
        let (dtype, bytes, path, vox) = match read {
            Ok(mut data) => {
                let Host::DataVolume(volume) = &data.message else {
                    return Err(Error::protocol("expected DataVolume"));
                };
                if volume.t != view.t
                    || volume.c != channel
                    || volume.level != 0
                    || volume.open_generation != view.open_generation
                {
                    return Err(Error::protocol("dataset changed during description"));
                }
                let dtype = volume
                    .segment
                    .as_ref()
                    .ok_or_else(|| Error::protocol("missing volume descriptor"))?
                    .dtype;
                let bytes = data.copy_payload()?.bytes;
                (
                    dtype as u16,
                    bytes,
                    "read via host data access",
                    "unknown — the host publishes no voxel size to a plugin without a tool".into(),
                )
            }
            Err(e) if e.kind == Kind::Unsupported => {
                let reader = self.active.lock().unwrap().upgrade().ok_or_else(|| {
                    error(
                        Kind::NotFound,
                        "host data access unavailable and no crate is open",
                    )
                })?;
                let bytes = reader
                    .read_volume(&pb::ReadVolume {
                        t: view.t,
                        c: channel,
                        ..Default::default()
                    })?
                    .bytes;
                (reader.header.dtype,bytes,"read directly — host data access is unavailable for a dataset this plugin serves",reader.header.label())
            }
            Err(e) => return Err(e),
        };
        let values = format::values(dtype, &bytes)?;
        let min = values.iter().copied().fold(f64::INFINITY, f64::min);
        let max = values.iter().copied().fold(f64::NEG_INFINITY, f64::max);
        let mean = values.iter().sum::<f64>() / values.len() as f64;
        if self.stopping.load(Ordering::SeqCst) {
            return Ok(());
        }
        self.update(ctx, |m| {
            m.status = format!("{path}; min={min}, max={max}, mean={mean}");
            m.vox = vox;
        });
        Ok(())
    }
    fn shutdown(&self) {
        self.stopping.store(true, Ordering::SeqCst);
        let exports = self
            .exports
            .lock()
            .unwrap()
            .iter()
            .filter_map(Weak::upgrade)
            .collect::<Vec<_>>();
        for exporter in exports {
            if let Err(e) = exporter.abort() {
                eprintln!("shutdown: {e}");
            }
        }
    }
    pub fn join(&self) {
        for job in self.workers.lock().unwrap().drain(..) {
            let _ = job.join();
        }
    }
}
struct BusyGuard(Arc<App>);
impl Drop for BusyGuard {
    fn drop(&mut self) {
        self.0.busy.store(false, Ordering::SeqCst);
    }
}

struct BridgeReader {
    file: Mutex<Option<Crate<File>>>,
    header: Header,
    cal: Option<Calibration>,
    app: Weak<App>,
    ctx: Context,
}
impl Reader for BridgeReader {
    fn info(&self) -> Result<pb::ReaderOpened> {
        let [z, y, x, t] = self.header.calibration;
        Ok(pb::ReaderOpened {
            shape_tczyx: self.header.shape.to_vec(),
            dtype: self.header.dtype as i32,
            little_endian: true,
            axis_inference: "crate".into(),
            channel_names: self
                .file
                .lock()
                .unwrap()
                .as_ref()
                .ok_or_else(|| error(Kind::NotFound, "crate file is closed"))?
                .names
                .clone(),
            spacing_z: z,
            spacing_y: y,
            spacing_x: x,
            spacing_unit: if [z, y, x].iter().any(|v| *v > 0.) {
                "um"
            } else {
                ""
            }
            .into(),
            time_increment: t,
            time_unit: if t > 0. { "s" } else { "" }.into(),
            ..Default::default()
        })
    }
    fn read_volume(&self, req: &pb::ReadVolume) -> Result<Volume> {
        let mut guard = self.file.lock().unwrap();
        let file = guard
            .as_mut()
            .ok_or_else(|| error(Kind::NotFound, "crate file is closed"))?;
        let verify = self
            .app
            .upgrade()
            .is_none_or(|a| a.verify.load(Ordering::Relaxed));
        let bytes = file.volume(req.t, req.c, verify, self.cal.as_ref())?;
        Ok(Volume {
            bytes,
            shape_zyx: self.header.shape[2..].try_into().unwrap(),
            dtype: pb::DType::try_from(self.header.dtype as i32).unwrap(),
        })
    }
    fn read_plane(&self, req: &pb::ReadPlane) -> Result<Volume> {
        let mut guard = self.file.lock().unwrap();
        let file = guard
            .as_mut()
            .ok_or_else(|| error(Kind::NotFound, "crate file is closed"))?;
        let bytes = file.plane(req.t, req.c, req.z, self.cal.as_ref())?;
        Ok(Volume {
            bytes,
            shape_zyx: [1, self.header.shape[3], self.header.shape[4]],
            dtype: pb::DType::try_from(self.header.dtype as i32).unwrap(),
        })
    }
    fn metadata(&self) -> Result<pb::MetadataResult> {
        let guard = self.file.lock().unwrap();
        let file = guard
            .as_ref()
            .ok_or_else(|| error(Kind::NotFound, "crate file is closed"))?;
        if file.header.flags & crate::format::DERIVED != 0 {
            return Ok(pb::MetadataResult::default());
        }
        Ok(pb::MetadataResult {
            form: file.form.clone(),
            text: file.text.clone(),
        })
    }
    fn close(&self) -> Result<()> {
        self.file.lock().unwrap().take();
        if let Some(app) = self.app.upgrade() {
            let mut active = app.active.lock().unwrap();
            if std::ptr::eq(active.as_ptr(), self) {
                *active = Weak::new();
                app.update(&self.ctx, |m| {
                    m.head = "No crate open".into();
                    m.vox = "(unknown)".into();
                });
            }
        }
        Ok(())
    }
}
struct ExportState {
    writer: Option<Writer<File>>,
    times: Vec<u32>,
    channels: Vec<u32>,
    completed: bool,
}
struct BridgeExport {
    state: Mutex<ExportState>,
    path: PathBuf,
    little_endian: bool,
    resolved: calibration::Resolved,
    app: Weak<App>,
    ctx: Context,
}
impl BridgeExport {
    fn annotate(&self, e: Error) -> Error {
        error(e.kind, format!("{}: {}", self.path.display(), e.message))
    }
    fn remove(&self) -> Result<()> {
        match fs::remove_file(&self.path) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(self.annotate(error(Kind::IoError, e.to_string()))),
        }
    }
}
impl Exporter for BridgeExport {
    fn write(&self, t: u32, channels: &[u32], bytes: &[u8]) -> Result<u64> {
        let mut state = self.state.lock().unwrap();
        let writer = state
            .writer
            .as_ref()
            .ok_or_else(|| self.annotate(error(Kind::IoError, "export file is closed")))?;
        let shape = writer.header.shape;
        let size = writer.header.block_bytes()? as usize;
        let mut times = state.times.clone();
        let mut order = state.channels.clone();
        if !times.contains(&t) {
            times.push(t);
        }
        for c in channels {
            if !order.contains(c) {
                order.push(*c);
            }
        }
        if times.len() > shape[0] as usize || order.len() > shape[1] as usize {
            return Err(self.annotate(error(
                Kind::OutOfBounds,
                format!(
                    "source t={t}, channels={channels:?}: too many distinct {}",
                    if times.len() > shape[0] as usize {
                        "timepoints"
                    } else {
                        "channels"
                    }
                ),
            )));
        }
        if channels.is_empty() || size.checked_mul(channels.len()) != Some(bytes.len()) {
            return Err(self.annotate(error(
                Kind::InvalidArgument,
                "chunk length disagrees with geometry",
            )));
        }
        let ta = times.iter().position(|v| *v == t).unwrap() as u32;
        for (&c, b) in channels.iter().zip(bytes.chunks_exact(size)) {
            let ca = order.iter().position(|v| *v == c).unwrap() as u32;
            state
                .writer
                .as_mut()
                .unwrap()
                .block(ta, ca, b, self.little_endian)
                .map_err(|e| self.annotate(e))?;
        }
        state.times = times;
        state.channels = order;
        Ok(bytes.len() as u64)
    }
    fn finish(&self) -> Result<pb::ExportDone> {
        let mut state = self.state.lock().unwrap();
        let writer = state
            .writer
            .take()
            .ok_or_else(|| self.annotate(error(Kind::IoError, "export file is closed")))?;
        let result = writer
            .finish()
            .map(|file| {
                drop(file);
                pb::ExportDone::default()
            })
            .map_err(|e| self.annotate(e));
        if result.is_ok() {
            state.completed = true;
        } else {
            self.remove()?;
        }
        if let Some(app) = self.app.upgrade() {
            app.update(&self.ctx, |m| {
                m.status = match &result {
                    Ok(_) => format!(
                        "Exported {}{}",
                        self.path.file_name().unwrap_or_default().to_string_lossy(),
                        if self.resolved.values == [0.; 4] {
                            " without voxel size — the host supplied no spacing."
                        } else {
                            ""
                        }
                    ),
                    Err(e) => format!("Export failed: {e}"),
                };
                m.vox = format::spacing_label(self.resolved.values);
            });
        }
        result
    }
    fn abort(&self) -> Result<()> {
        let mut state = self.state.lock().unwrap();
        if state.completed {
            return Ok(());
        }
        state.writer.take();
        self.remove()?;
        if let Some(app) = self.app.upgrade() {
            app.update(&self.ctx, |m| {
                m.status = format!("Export canceled: {}", self.path.display())
            });
        }
        Ok(())
    }
}
impl Drop for BridgeExport {
    fn drop(&mut self) {
        let state = self.state.get_mut().unwrap();
        if !state.completed {
            state.writer.take();
            if let Err(e) = self.remove() {
                eprintln!("{e}");
            }
        }
    }
}

#[cfg(all(test, target_os = "linux"))]
mod lifecycle_tests {
    use super::*;
    use vizi::{control::Runtime, manifest::Manifest, plugin::Registry, Doorbell};

    #[test]
    fn replaced_reader_close_preserves_successor_panel_and_current_close_clears_it() {
        let manifest =
            Manifest::load(Path::new(env!("CARGO_MANIFEST_DIR")).join("vizi-plugin.json")).unwrap();
        let (fd, _peer) = vizi::socket_pair().unwrap();
        let (bell, listener) = Doorbell::new(fd, Default::default()).unwrap();
        let runtime = Runtime::new(manifest, Arc::new(Registry::new()), bell, listener).unwrap();
        let ctx = runtime.context();
        let app = App::new();
        let open = |name: &str| {
            app.open_reader(
                &ctx,
                &pb::OpenReader {
                    uri: Path::new(env!("CARGO_MANIFEST_DIR"))
                        .join("../fixtures")
                        .join(name)
                        .to_str()
                        .unwrap()
                        .into(),
                    reader_id: "crate".into(),
                    ..Default::default()
                },
            )
            .unwrap()
        };
        let old = open("sample.crate");
        let next = open("nospacing.crate");
        let expected = {
            let model = app.model.lock().unwrap();
            (model.head.clone(), model.vox.clone())
        };
        old.close().unwrap();
        {
            let model = app.model.lock().unwrap();
            assert_eq!((&model.head, &model.vox), (&expected.0, &expected.1));
        }
        assert!(app.active.lock().unwrap().upgrade().is_some());
        next.close().unwrap();
        assert_eq!(app.model.lock().unwrap().head, "No crate open");
        assert_eq!(app.model.lock().unwrap().vox, "(unknown)");
        assert!(app.active.lock().unwrap().upgrade().is_none());
        runtime.stop();
    }
}

#[cfg(all(test, target_os = "linux"))]
mod provenance_tests {
    use super::*;
    use vizi::{control::Runtime, manifest::Manifest, plugin::Registry, Doorbell};

    #[test]
    fn export_preserves_source_sections_and_reader_serves_only_artifact_names() {
        let manifest =
            Manifest::load(Path::new(env!("CARGO_MANIFEST_DIR")).join("vizi-plugin.json")).unwrap();
        let (fd, _peer) = vizi::socket_pair().unwrap();
        let (bell, listener) = Doorbell::new(fd, Default::default()).unwrap();
        let runtime = Runtime::new(manifest, Arc::new(Registry::new()), bell, listener).unwrap();
        let ctx = runtime.context();
        let app = App::new();
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("derived.crate");
        let doc = "<Pixels PhysicalSizeX='99' TimeIncrement='99'/>";
        let exporter = app
            .open_export(
                &ctx,
                &pb::OpenExport {
                    locator: path.to_str().unwrap().into(),
                    shape_tczyx: vec![1, 6, 1, 1, 1],
                    dtype: 1,
                    source_metadata: Some(pb::MetadataDoc {
                        form: "ome-xml".into(),
                        text: doc.into(),
                    }),
                    provenance: "{\"source\":\"image\"}".into(),
                    channels: [
                        (4, "DNA", 1),
                        (5, "Channel 6", 2),
                        (6, "RNA", 3),
                        (7, "future", 99),
                        (8, "bad\0name", 1),
                        (9, "unspecified", 0),
                    ]
                    .into_iter()
                    .map(|(index, name, source)| pb::ChannelInfo {
                        index,
                        name: name.into(),
                        source,
                    })
                    .collect(),
                    spacing_y: 2.,
                    spacing_unit: "um".into(),
                    little_endian: true,
                    ..Default::default()
                },
            )
            .unwrap();
        exporter
            .write(0, &[4, 5, 6, 7, 8, 9], &[1, 2, 3, 4, 5, 6])
            .unwrap();
        exporter.finish().unwrap();
        let mut file = Crate::open(&path).unwrap();
        assert_eq!(file.header.flags, crate::format::DERIVED);
        assert_eq!((&*file.form, &*file.text), ("ome-xml", doc));
        assert_eq!(file.provenance, b"{\"source\":\"image\"}");
        assert_eq!(file.names, ["DNA", "", "RNA"]);
        assert_eq!(file.header.calibration, [0., 2., 0., 0.]);
        for c in 0..6 {
            assert_eq!(file.volume(0, c, true, None).unwrap(), [c as u8 + 1]);
        }
        let req = pb::OpenReader {
            uri: path.to_str().unwrap().into(),
            ..Default::default()
        };
        let reader = app.open_reader(&ctx, &req).unwrap();
        assert_eq!(reader.info().unwrap().channel_names, ["DNA", "", "RNA"]);
        assert_eq!(reader.metadata().unwrap(), pb::MetadataResult::default());
        reader.close().unwrap();

        let original = app
            .open_reader(
                &ctx,
                &pb::OpenReader {
                    uri: Path::new(env!("CARGO_MANIFEST_DIR"))
                        .join("../fixtures/sample.crate")
                        .to_str()
                        .unwrap()
                        .into(),
                    ..Default::default()
                },
            )
            .unwrap();
        assert_eq!(original.metadata().unwrap().form, "ome-xml");
        original.close().unwrap();
        // Invalid UTF-8 names are diagnosed when opened but do not hide voxels.
        let mut bytes = std::fs::read(&path).unwrap();
        bytes[96 + 7 + doc.len() + b"{\"source\":\"image\"}".len()] = 0xff;
        std::fs::write(&path, bytes).unwrap();
        let reader = app.open_reader(&ctx, &req).unwrap();
        assert!(reader.info().unwrap().channel_names.is_empty());
        assert!(app
            .model
            .lock()
            .unwrap()
            .status
            .contains("malformed channel names"));
        assert_eq!(
            reader
                .read_volume(&pb::ReadVolume::default())
                .unwrap()
                .bytes,
            [1]
        );
        reader.close().unwrap();
        runtime.stop();
    }
}
