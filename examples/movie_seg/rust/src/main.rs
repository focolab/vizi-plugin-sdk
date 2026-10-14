mod bands;
mod declaration;
#[cfg(test)]
mod tests;
use std::sync::{Arc, Mutex};
use vizi::{
    control::{Context, Error, Result},
    declare::{DeclaredPlugin, ManagementCommand},
    plugin::Chunk,
    proto::{self as pb, value::Kind},
};

struct Model {
    threshold: f64,
    visible: [bool; 3],
    tables_sent: [bool; 2],
}
impl Default for Model {
    fn default() -> Self {
        Self {
            threshold: 400.0,
            visible: [true; 3],
            tables_sent: [false; 2],
        }
    }
}
impl Model {
    fn table_once(&mut self, ctx: &Context, footprint: bool) -> Result<()> {
        let index = usize::from(footprint);
        if !self.tables_sent[index] {
            if footprint {
                ctx.set_label_table("footprint", &bands::footprint_table())?;
            } else {
                ctx.set_label_table("bands", &bands::bands_table(self.visible))?;
            }
            self.tables_sent[index] = true;
        }
        Ok(())
    }
}

fn source(
    ctx: &Context,
    want: &pb::ChunkWant,
    model: &Mutex<Model>,
    footprint: bool,
) -> Result<Option<Chunk>> {
    let threshold = {
        let mut model = model.lock().unwrap();
        model.table_once(ctx, footprint)?;
        model.threshold
    };
    if !footprint {
        let t = want
            .t
            .ok_or_else(|| Error::protocol("bands want requires t"))?;
        let volume = ctx.read_volume(t, 0, want.level, None)?;
        let labels: Vec<_> = volume
            .data
            .into_f32()
            .into_iter()
            .map(|v| bands::label(f64::from(v), threshold))
            .collect();
        return Ok(bands::tight(&labels, volume.shape_zyx));
    }
    if want.t.is_some() {
        return Err(Error::protocol("footprint want must have no t"));
    }
    let view = ctx.view_state()?;
    let frames = view
        .shape_tczyx
        .first()
        .copied()
        .filter(|&n| n > 0)
        .ok_or_else(|| Error::protocol("dataset has no timepoints"))?;
    let mut mask = Vec::new();
    let mut shape = [0; 3];
    for t in 0..frames {
        let volume = ctx.read_volume(t, 0, want.level, None)?;
        if volume.open_generation != view.open_generation {
            return Err(Error::protocol("dataset changed during footprint"));
        }
        let values = volume.data.into_f32();
        if t == 0 {
            shape = volume.shape_zyx;
            mask.resize(values.len(), 0);
        } else if shape != volume.shape_zyx {
            return Err(Error::protocol("dataset shape changed during footprint"));
        }
        bands::union_into(&mut mask, &values, threshold);
    }
    Ok(bands::tight(&mask, shape))
}

fn threshold_arg(command: &pb::InvokeCommand) -> Option<f64> {
    match command.args.get("value")?.kind.as_ref()? {
        Kind::I(n) if *n >= 0 => Some(*n as f64),
        Kind::F(n) if n.is_finite() && *n >= 0.0 => Some(*n),
        Kind::S(s) => s
            .trim()
            .parse::<f64>()
            .ok()
            .filter(|n| n.is_finite() && *n >= 0.0),
        _ => None,
    }
}
fn visibility_args(command: &pb::InvokeCommand) -> Option<(usize, bool)> {
    match (
        command.args.get("band")?.kind.as_ref()?,
        command.args.get("visible")?.kind.as_ref()?,
    ) {
        (Kind::I(n @ 1..=3), Kind::B(visible)) => Some((*n as usize - 1, *visible)),
        _ => None,
    }
}
fn reply(ok: bool, message: impl Into<String>) -> pb::CommandDone {
    pb::CommandDone {
        ok,
        message: message.into(),
        ..Default::default()
    }
}
fn threshold_reply(threshold: f64, wanted: usize) -> String {
    format!(
        "threshold {}; {wanted} wanted volume(s) to serve again",
        bands::threshold_text(threshold)
    )
}
fn visibility_reply(band: usize, visible: bool) -> String {
    format!(
        "band {} ({}) {}",
        band + 1,
        bands::NAMES[band],
        if visible { "visible" } else { "hidden" }
    )
}
fn set_visibility(
    ctx: &Context,
    model: &Mutex<Model>,
    band: usize,
    visible: bool,
) -> Result<pb::CommandDone> {
    let mut model = model.lock().unwrap();
    model.visible[band] = visible;
    if model.tables_sent[0] {
        ctx.set_label_table("bands", &bands::bands_table(model.visible))?;
    }
    ctx.panel("movie_seg")
        .set_value(&format!("band_{}", band + 1), visible)?;
    Ok(reply(true, visibility_reply(band, visible)))
}
fn declare(model: Arc<Mutex<Model>>) -> std::result::Result<DeclaredPlugin, String> {
    let mut plugin = declaration::builder();
    let [bands, footprint] = declaration::overlays();
    let [threshold, visible, band_1, band_2, band_3] = declaration::commands();
    let band_source = model.clone();
    let footprint_source = model.clone();
    let threshold_model = model.clone();
    plugin
        .chunked_overlay(bands, move |ctx, want| {
            source(ctx, want, &band_source, false)
        })
        .chunked_overlay(footprint, move |ctx, want| {
            source(ctx, want, &footprint_source, true)
        });
    let threshold = plugin.command(threshold, move |ctx, command| {
        let Some(threshold) = threshold_arg(command) else {
            return Ok(reply(false, "value must be a finite number >= 0"));
        };
        threshold_model.lock().unwrap().threshold = threshold;
        ctx.panel("movie_seg")
            .set_value("threshold", bands::threshold_text(threshold))?;
        let mut wanted = 0;
        for overlay in ["bands", "footprint"] {
            wanted += ctx.wants(overlay)?.len();
            ctx.invalidate_chunks(overlay)?;
        }
        Ok(reply(true, threshold_reply(threshold, wanted)))
    });
    let visible_model = model.clone();
    plugin.command(visible, move |ctx, command| {
        let Some((band, visible)) = visibility_args(command) else {
            return Ok(reply(
                false,
                "band must be an integer in 1..3 and visible must be a bool",
            ));
        };
        set_visibility(ctx, &visible_model, band, visible)
    });
    let bands = [(0, band_1), (1, band_2), (2, band_3)].map(|(band, spec)| {
        let model = model.clone();
        plugin.command(spec, move |ctx, command| {
            let Some(Kind::B(visible)) = command.args.get("value").and_then(|v| v.kind.as_ref())
            else {
                return Ok(reply(false, "value must be a bool"));
            };
            set_visibility(ctx, &model, band, *visible)
        })
    });
    declaration::panel(&mut plugin, threshold, bands);
    plugin.build()
}

#[tokio::main]
async fn main() -> std::process::ExitCode {
    vizi::declare::report_exit(run().await)
}

async fn run() -> std::result::Result<(), Box<dyn std::error::Error>> {
    let Some(plugin) = ManagementCommand::prepare(
        declaration::ID,
        env!("CARGO_PKG_VERSION"),
        std::env::args_os().skip(1),
        || declare(Arc::new(Mutex::new(Model::default()))),
    )?
    else {
        return Ok(());
    };
    // The launcher transfers exclusive ownership of the inherited doorbell fd.
    let runtime = unsafe { plugin.into_runtime_from_env() }?;
    runtime.serve_env().await?;
    Ok(())
}

#[cfg(test)]
mod declaration_tests {
    use super::*;
    #[test]
    fn manifest_matches_development_snapshot() {
        let plugin = declare(Arc::new(Mutex::new(Model::default()))).unwrap();
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
