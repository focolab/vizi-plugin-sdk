//! A processing-stage plugin. Build with --release --example processing_stage;
//! --manifest DIR and --install use the same declaration as the running stage.
use std::sync::Arc;
use vizi_doorbell::{
    control::{Context, Error, Result},
    declare::{ManagementCommand, PluginBuilder, StageSpec},
    proto as pb,
    stage::{Outcome, ParamSpec, ParamType, ParamValue, Params, Slot, Stage},
};

struct Invert;
impl Stage for Invert {
    fn plan(
        &self,
        _: &Context,
        input: &pb::StageShape,
        params: &Params,
    ) -> Result<pb::StagePlanned> {
        if input.dtype != pb::DType::DtypeU8 as i32 {
            return Err(Error::unsupported("invert accepts u8 volumes"));
        }
        let enabled = params["enabled"] == ParamValue::Bool(true);
        Ok(pb::StagePlanned {
            output: Some(input.clone()),
            modifies: input
                .channels
                .iter()
                .filter(|_| enabled)
                .map(|&channel| pb::StageChannelNeed {
                    channel,
                    needs: vec![channel],
                })
                .collect(),
            adds: Vec::new(),
            reads: Vec::new(),
        })
    }
    fn process(&self, _: &Context, _: &pb::StageArm, slot: &mut Slot) -> Result<Outcome> {
        if slot.layout.dtype != pb::DType::DtypeU8 {
            return Err(Error::protocol("current slot is not u8"));
        }
        if slot.params()["enabled"] == ParamValue::Bool(false) {
            return Ok(Outcome::Processed);
        }
        let [_, y, x] = slot.layout.allocation;
        let plane_bytes = slot.layout.plane_bytes()?;
        // Bounded row-sized scratch, preserving the current packed layout.
        // Work only on the valid extent; placement and pose stay unchanged.
        for plane in 0..slot.layout.channels.len() {
            for z in slot.extent[0][0]..slot.extent[0][1] {
                for row in slot.extent[1][0]..slot.extent[1][1] {
                    let offset = plane as u64 * plane_bytes
                        + (u64::from(z) * u64::from(y) + u64::from(row)) * u64::from(x)
                        + u64::from(slot.extent[2][0]);
                    let mut bytes =
                        slot.read_bytes(offset, u64::from(slot.extent[2][1] - slot.extent[2][0]))?;
                    for byte in &mut bytes {
                        *byte = 255 - *byte;
                    }
                    slot.write_bytes(offset, &bytes)?;
                }
            }
        }
        Ok(Outcome::Processed)
    }
}
#[tokio::main]
async fn main() -> std::process::ExitCode {
    vizi_doorbell::declare::report_exit(run().await)
}

async fn run() -> std::result::Result<(), Box<dyn std::error::Error>> {
    let Some(plugin) = ManagementCommand::prepare(
        "org.vizi.rust_invert",
        "0.1.0",
        std::env::args_os().skip(1),
        || {
            let mut plugin =
                PluginBuilder::new("org.vizi.rust_invert", "Rust invert stage", "0.1.0");
            let mut spec = StageSpec::new("invert");
            spec.params
                .push(ParamSpec::new("enabled", ParamType::Bool { value: true }));
            plugin.stage(spec, Arc::new(Invert));
            plugin.build()
        },
    )?
    else {
        return Ok(());
    };
    // Vizi transfers exclusive ownership of the inherited launch fd.
    let runtime = unsafe { plugin.into_runtime_from_env() }?;
    runtime.serve_env().await?;
    Ok(())
}
