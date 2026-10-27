mod arithmetic;
mod declaration;
#[cfg(test)]
mod tests;

use arithmetic::{Drift, Plane};
use std::sync::{Arc, Mutex};
use vizi::{
    control::{Context, Error, Result},
    declare::{DeclaredPlugin, ManagementCommand},
    proto as pb,
    stage::{Outcome, ParamValue, Params, Refusal, Slot, Stage},
};

#[derive(Clone, Copy)]
enum Kind {
    Gain,
    Diff,
    Widen,
    Drift,
}

fn int(params: &Params, key: &str) -> u32 {
    match params[key] {
        ParamValue::Int(value) => value as u32,
        _ => unreachable!("the SDK checks parameter kinds and bounds"),
    }
}

impl Kind {
    fn plan(self, input: &pb::StageShape, params: &Params) -> Result<pb::StagePlanned> {
        let channel = |key: &str| -> Result<u32> {
            let channel = int(params, key);
            if !input.channels.contains(&channel) {
                return Err(Error::unsupported(format!("channel {channel} is absent")));
            }
            Ok(channel)
        };
        let mut plan = pb::StagePlanned {
            output: Some(input.clone()),
            ..Default::default()
        };
        match self {
            Self::Gain => {
                let channel = channel("channel")?;
                plan.modifies.push(pb::StageChannelNeed {
                    channel,
                    needs: vec![channel],
                });
            }
            Self::Diff => plan.adds.push(pb::StageNewChannel {
                key: "absdiff".into(),
                name: "Absolute difference".into(),
                needs: vec![channel("a")?, channel("b")?],
            }),
            Self::Widen => {
                let crop = int(params, "crop");
                if input.y <= 2 * crop || input.x <= 2 * crop {
                    return Err(Error::unsupported("crop leaves no output voxels"));
                }
                let output = plan.output.as_mut().unwrap();
                output.y -= 2 * crop;
                output.x -= 2 * crop;
                output.dtype = pb::DType::DtypeF32 as i32;
                plan.modifies = input
                    .channels
                    .iter()
                    .map(|&channel| pb::StageChannelNeed {
                        channel,
                        needs: vec![channel],
                    })
                    .collect();
            }
            Self::Drift => {
                let channel = channel("channel")?;
                if !matches!(
                    pb::DType::try_from(input.dtype),
                    Ok(pb::DType::DtypeU8 | pb::DType::DtypeU16)
                ) {
                    return Err(Error::unsupported("drift requires u8 or u16"));
                }
                plan.reads.push(channel);
            }
        }
        Ok(plan)
    }
}

struct Processor {
    kind: Kind,
    drift: Mutex<Drift>,
}
impl Stage for Processor {
    fn plan(
        &self,
        _: &Context,
        input: &pb::StageShape,
        params: &Params,
    ) -> Result<pb::StagePlanned> {
        self.kind.plan(input, params)
    }
    fn process(&self, _: &Context, arm: &pb::StageArm, slot: &mut Slot) -> Result<Outcome> {
        match self.kind {
            Kind::Gain => {
                let stall = int(slot.params(), "stall_ms");
                std::thread::sleep(std::time::Duration::from_millis(stall.into()));
                let ParamValue::Float(factor) = slot.params()["factor"] else {
                    unreachable!("SDK checked float parameter")
                };
                let channel = int(slot.params(), "channel") as u16;
                let (offset, mut plane) = read_plane(slot, channel)?;
                plane.gain(slot.extent, factor);
                // Only write rows inside the extent, leaving other bytes untouched.
                for z in slot.extent[0][0]..slot.extent[0][1] {
                    for y in slot.extent[1][0]..slot.extent[1][1] {
                        let start = plane.offset([z, y, slot.extent[2][0]]);
                        let end = start
                            + (slot.extent[2][1] - slot.extent[2][0]) as usize * plane.width();
                        slot.write_bytes(offset + start as u64, &plane.bytes[start..end])?;
                    }
                }
            }
            Kind::Diff => {
                let (_, a) = read_plane(slot, int(slot.params(), "a") as u16)?;
                let (_, b) = read_plane(slot, int(slot.params(), "b") as u16)?;
                let result = a.diff(&b, slot.extent);
                let offset = slot.layout.channels.len() as u64 * slot.layout.plane_bytes()?;
                slot.write_bytes(offset, &result.bytes)?;
                slot.layout
                    .channels
                    .push(arm.new_channels["absdiff"] as u16);
            }
            Kind::Widen => {
                let crop = int(slot.params(), "crop");
                // An actual slot may be smaller than the planned maximum.
                if slot.layout.allocation[1] <= 2 * crop || slot.layout.allocation[2] <= 2 * crop {
                    return Ok(Outcome::Refused(Refusal::Other));
                }
                // Snapshot all planes before writing: wider elements overlap the old data.
                let planes = slot
                    .layout
                    .channels
                    .iter()
                    .map(|&channel| read_plane(slot, channel).map(|(_, plane)| plane.widen(crop)))
                    .collect::<Result<Vec<_>>>()?;
                let [z, y, x] = slot.layout.allocation;
                let allocation = [z, y - 2 * crop, x - 2 * crop];
                let mut offset = 0;
                for plane in planes {
                    slot.write_bytes(offset, &plane.bytes)?;
                    offset += plane.bytes.len() as u64;
                }
                (slot.extent, slot.origin) =
                    arithmetic::placement(slot.extent, slot.origin, allocation, crop);
                slot.layout.allocation = allocation;
                slot.layout.dtype = pb::DType::DtypeF32;
            }
            Kind::Drift => {
                let key = (arm.pipeline, arm.position);
                let mut drift = self.drift.lock().unwrap();
                if !drift.accept_time(key, slot.t()) {
                    return Ok(Outcome::Refused(Refusal::NeedsEarlierState));
                }
                let (_, plane) = read_plane(slot, int(slot.params(), "channel") as u16)?;
                let centroid = plane.centroid(slot.extent, slot.origin);
                drift.apply(key, slot.t(), centroid, &mut slot.pose);
            }
        }
        Ok(Outcome::Processed)
    }
}

fn read_plane(slot: &Slot, channel: u16) -> Result<(u64, Plane)> {
    let index = slot
        .layout
        .channels
        .iter()
        .position(|&c| c == channel)
        .ok_or_else(|| Error::internal("SDK-checked input channel absent"))?;
    let length = slot.layout.plane_bytes()?;
    let offset = index as u64 * length;
    Ok((
        offset,
        Plane {
            shape: slot.layout.allocation,
            dtype: slot.layout.dtype,
            bytes: slot.read_bytes(offset, length)?,
        },
    ))
}

fn declare() -> std::result::Result<DeclaredPlugin, String> {
    let mut plugin = declaration::builder();
    for (spec, kind) in
        declaration::stages()
            .into_iter()
            .zip([Kind::Gain, Kind::Diff, Kind::Widen, Kind::Drift])
    {
        plugin.stage(
            spec,
            Arc::new(Processor {
                kind,
                drift: Mutex::new(Drift::default()),
            }),
        );
    }
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
        declare,
    )?
    else {
        return Ok(());
    };
    // The host transfers exclusive ownership of the inherited doorbell fd.
    let runtime = unsafe { plugin.into_runtime_from_env() }?;
    runtime.serve_env().await?;
    Ok(())
}

#[cfg(test)]
mod declaration_tests {
    use super::*;
    #[test]
    fn manifest_matches_development_snapshot() {
        let plugin = declare().unwrap();
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
