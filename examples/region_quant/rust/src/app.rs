use crate::{
    table::{self, Measure, Row},
    trace::Traces,
};
use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use vizi::{
    control::{Context, Error, Follow, Result, ViewMove, VoxelData},
    declare::DeclaredPlugin,
    proto as pb,
};

#[derive(Clone, Default)]
pub struct Model {
    t: u32,
    c: u32,
    saw_view: bool,
    generation: Option<u64>,
    frames: u32,
    channels: u32,
    float: bool,
    selected: String,
    rows: BTreeMap<String, Row>,
    colormap: Option<Vec<u8>>,
    offered_count: Option<usize>,
    last_toured: Option<(String, String)>,
}

pub(crate) fn measure(ctx: &Context, id: &str, t: u32, c: u32, generation: u64) -> Result<Measure> {
    let frame =
        i32::try_from(t).map_err(|_| Error::protocol("frame does not fit an inclusion request"))?;
    let inclusion = ctx.read_roi_inclusion(id, frame)?;
    if inclusion.open_generation != generation {
        return Err(Error::new(
            pb::error::Kind::NotFound,
            "dataset changed during measurement",
        ));
    }
    let Some(mask) = inclusion.volume else {
        return Ok(Measure::default());
    };
    let mut hi = mask.origin_zyx;
    for (end, size) in hi.iter_mut().zip(mask.shape_zyx) {
        *end = end
            .checked_add(size)
            .ok_or_else(|| Error::protocol("inclusion box overflows"))?;
    }
    let volume = ctx.read_volume(t, c, 0, Some((mask.origin_zyx, hi)))?;
    if volume.open_generation != generation {
        return Err(Error::new(
            pb::error::Kind::NotFound,
            "dataset changed during measurement",
        ));
    }
    if volume.shape_zyx != mask.shape_zyx {
        return Err(Error::protocol(
            "inclusion and intensity data refer to different boxes",
        ));
    }
    match volume.data {
        VoxelData::U8(values) => masked_sum(&mask.data, values.into_iter().map(u64::from)),
        VoxelData::U16(values) => masked_sum(&mask.data, values.into_iter().map(u64::from)),
        VoxelData::F32(_) => Err(Error::unsupported(
            "region-quant requires integer intensity data",
        )),
    }
}
fn masked_sum(mask: &[u8], values: impl ExactSizeIterator<Item = u64>) -> Result<Measure> {
    if mask.len() != values.len() {
        return Err(Error::protocol("mask and intensity payload sizes differ"));
    }
    let mut result = Measure::default();
    for (&included, value) in mask.iter().zip(values) {
        if included == 1 {
            result.count += 1;
            result.sum = result
                .sum
                .checked_add(value)
                .ok_or_else(|| Error::protocol("intensity sum overflows u64"))?;
        }
    }
    Ok(result)
}
const FLOAT_STATUS: &str = "Region Quant measures integer data; this dataset is f32";
const MAX_ROWS: usize = 1024;

impl Model {
    fn tour_rows(&self, resumed: bool) -> Vec<String> {
        table::ordered(self.rows.values())
            .into_iter()
            .filter(|row| {
                !resumed
                    || self.last_toured.as_ref().is_none_or(|last| {
                        (row.display_name(), row.id.as_str()) > (last.0.as_str(), last.1.as_str())
                    })
            })
            .map(|row| row.id.clone())
            .collect::<Vec<_>>()
    }

    fn refresh(&mut self, ctx: &Context) -> Result<()> {
        let (t, c, generation) = (self.t, self.c, self.generation.unwrap_or(0));
        if self.float {
            for row in self.rows.values_mut() {
                row.measure = Measure::default();
            }
        } else if let Err(error) = self.refresh_with(|id| measure(ctx, id, t, c, generation)) {
            status(ctx, format!("Measuring failed: {}", error.message))?;
        }
        Ok(())
    }
    fn refresh_with(&mut self, mut measure: impl FnMut(&str) -> Result<Measure>) -> Result<()> {
        let mut failure = None;
        for id in self.rows.keys().cloned().collect::<Vec<_>>() {
            match measure(&id) {
                Ok(value) => self.rows.get_mut(&id).unwrap().measure = value,
                Err(error) if error.kind == pb::error::Kind::NotFound => {
                    self.rows.remove(&id);
                }
                Err(error) => failure = Some(error),
            }
        }
        failure.map_or(Ok(()), Err)
    }
    fn dataset(&mut self, ctx: &Context, generation: u64, traces: &Traces) -> Result<bool> {
        if self.generation == Some(generation) {
            return Ok(false);
        }
        let view = ctx.view_state()?;
        self.generation = Some(view.open_generation);
        self.frames = view.shape_tczyx.first().copied().unwrap_or(0);
        self.channels = view.shape_tczyx.get(1).copied().unwrap_or(0);
        self.float = view.dtype == pb::DType::DtypeF32 as i32;
        self.t = view.t;
        self.selected = view.selected_region_id;
        if self.c >= self.channels {
            self.c = 0;
            ctx.panel("quant").set_value("channel", 0_i64)?;
        }
        traces.dataset(ctx, view.open_generation, self.frames, self.c, self.float)?;
        status(ctx, if self.float { FLOAT_STATUS } else { "Ready" })?;
        Ok(true)
    }
    fn publish(&mut self, ctx: &Context) -> Result<()> {
        if self.colormap.is_none() {
            self.colormap = Some(ctx.get_colormap("viridis")?.rgba);
        }
        let panel = ctx.panel("quant");
        let count = self.rows.len();
        if self.offered_count != Some(count) {
            if count == 0 {
                panel.withdraw_follow()?;
            } else {
                panel.offer_follow(format!(
                    "Tour {count} {}",
                    if count == 1 { "region" } else { "regions" }
                ))?;
            }
            self.offered_count = Some(count);
        }
        let mut rows = table::ordered(self.rows.values());
        rows.truncate(MAX_ROWS);
        panel.set_rows("regions", rows.iter().map(|r| r.id.as_str()))?;
        let mut labels = Vec::new();
        let mut images = Vec::new();
        for row in &rows {
            for (widget, value) in [
                ("name", row.display_name().to_owned()),
                (
                    "voxels",
                    if self.float {
                        String::new()
                    } else {
                        row.measure.count.to_string()
                    },
                ),
                (
                    "mean",
                    if self.float {
                        String::new()
                    } else {
                        row.measure.mean()
                    },
                ),
            ] {
                labels.push(pb::PatchOp {
                    widget_id: widget.into(),
                    row_key: row.id.clone(),
                    op: Some(pb::patch_op::Op::SetValue(value.into())),
                });
            }
            let start = if self.float {
                0
            } else {
                row.measure.swatch_index() * 4
            };
            let table = self.colormap.as_ref().unwrap();
            images.push((
                row.id.clone(),
                table::swatch(table[start..start + 3].try_into().unwrap()),
            ));
        }
        ctx.patch("quant", labels)?;
        ctx.push_image_rows("quant", "swatch", &images)?;
        panel.set_value(
            "regions",
            if rows.iter().any(|r| r.id == self.selected) {
                self.selected.as_str()
            } else {
                ""
            },
        )?;
        if self.float {
            status(ctx, FLOAT_STATUS)?;
        } else if count > MAX_ROWS {
            status(ctx, format!("Showing 1024 of {count} regions"))?;
        }
        Ok(())
    }
    fn view(&mut self, ctx: &Context, view: pb::ViewChanged, traces: &Traces) -> Result<()> {
        let mut next = self.clone();
        let opened = next.dataset(ctx, view.open_generation, traces)?;
        next.saw_view = true;
        next.selected = view.selected_region_id;
        if opened || next.t != view.t {
            next.t = view.t;
            next.refresh(ctx)?;
        }
        next.publish(ctx)?;
        *self = next;
        Ok(())
    }
    fn regions(
        &mut self,
        ctx: &Context,
        change: pb::RegionsChanged,
        traces: &Traces,
    ) -> Result<()> {
        let mut next = self.clone();
        if change.reset {
            next.rows.clear();
        }
        let opened = next.dataset(ctx, change.open_generation, traces)?;
        if !next.saw_view {
            let view = ctx.view_state()?;
            next.t = view.t;
            next.selected = view.selected_region_id;
        }
        if opened {
            next.refresh(ctx)?;
        }
        let mut ids = Vec::new();
        for change in change.changes {
            if change.deleted {
                next.rows.remove(&change.region_id);
            } else {
                ids.push(change.region_id);
            }
        }
        // Empty IDs means ALL regions to the host, so never send an empty delta.
        if !ids.is_empty() {
            for record in ctx.get_rois_selected(&ids, None, None)? {
                let measure = if next.float {
                    Measure::default()
                } else {
                    match measure(
                        ctx,
                        &record.region_id,
                        next.t,
                        next.c,
                        next.generation.unwrap_or(0),
                    ) {
                        Ok(value) => value,
                        Err(error) if error.kind == pb::error::Kind::NotFound => {
                            next.rows.remove(&record.region_id);
                            continue;
                        }
                        Err(error) => {
                            status(ctx, format!("Measuring failed: {}", error.message))?;
                            next.rows
                                .get(&record.region_id)
                                .map_or(Measure::default(), |r| r.measure)
                        }
                    }
                };
                let row = Row {
                    id: record.region_id,
                    name: record.name,
                    measure,
                };
                next.rows.insert(row.id.clone(), row);
            }
        }
        next.publish(ctx)?;
        *self = next;
        Ok(())
    }
    fn channel(&mut self, ctx: &Context, c: u32) -> Result<()> {
        if self.c == c {
            return Ok(());
        }
        let mut next = self.clone();
        next.c = c;
        next.refresh(ctx)?;
        next.publish(ctx)?;
        *self = next;
        Ok(())
    }
}
fn done() -> pb::CommandDone {
    pb::CommandDone {
        ok: true,
        ..Default::default()
    }
}
fn status(ctx: &Context, text: impl Into<String>) -> Result<()> {
    ctx.panel("quant").set_value("status", text.into())
}
fn did_not_move(result: &pb::ViewRequestResult) -> String {
    let name = pb::ViewRefusal::try_from(result.reason)
        .map(|r| {
            r.as_str_name()
                .trim_start_matches("VIEW_REFUSAL_")
                .to_owned()
        })
        .unwrap_or_else(|_| result.reason.to_string());
    format!("The view did not move: {name}")
}
// A subscription may hold the model during host reads. Every navigation cause
// still gets an answer within its one-second budget; an unavailable row declines.
fn wait_model(model: &Mutex<Model>, budget: Duration) -> Option<std::sync::MutexGuard<'_, Model>> {
    let start = Instant::now();
    loop {
        if let Ok(model) = model.try_lock() {
            return Some(model);
        }
        let remaining = budget.checked_sub(start.elapsed())?;
        std::thread::sleep(remaining.min(Duration::from_millis(2)));
    }
}
fn pick_row(model: &Mutex<Model>, command: &pb::InvokeCommand, budget: Duration) -> Option<Row> {
    let model = wait_model(model, budget)?;
    command.args.get("row").and_then(|v| match &v.kind {
        Some(pb::value::Kind::S(id)) => model.rows.get(id).cloned(),
        _ => None,
    })
}

fn pick(
    ctx: &Context,
    command: &pb::InvokeCommand,
    model: &Mutex<Model>,
) -> Result<pb::CommandDone> {
    if command.cause == 0 {
        status(ctx, "No dataset is open")?;
        return Ok(done());
    }
    let navigation = ctx.command_view(command)?;
    let row = pick_row(model, command, Duration::from_millis(900));
    if let Some(row) = row {
        let result = navigation.move_view(ViewMove {
            select_region: Some(row.id.clone()),
            ..Default::default()
        })?;
        status(
            ctx,
            if result.applied() {
                format!("Selected {}", row.display_name())
            } else {
                did_not_move(&result)
            },
        )?;
    } else {
        navigation.decline_view()?;
    }
    Ok(done())
}
fn brightest(
    ctx: &Context,
    command: &pb::InvokeCommand,
    traces: &Traces,
    model: &Mutex<Model>,
) -> Result<pb::CommandDone> {
    if command.cause == 0 {
        status(ctx, "No dataset is open")?;
        return Ok(done());
    }
    let navigation = ctx.command_view(command)?;
    let Some(model) = wait_model(model, Duration::from_millis(900)) else {
        navigation.decline_view()?;
        return Ok(done());
    };
    if model.float {
        drop(model);
        navigation.decline_view()?;
        status(ctx, FLOAT_STATUS)?;
        return Ok(done());
    }
    let row = model
        .rows
        .contains_key(&model.selected)
        .then(|| traces.selection())
        .flatten();
    drop(model);
    let Some(row) = row else {
        navigation.decline_view()?;
        status(ctx, "Select a region first")?;
        return Ok(done());
    };
    if let Some((k, total)) = row.progress {
        navigation.decline_view()?;
        status(ctx, format!("Measuring… {k} of {total} frames"))?;
    } else if let Some(t) = row.best {
        let result = navigation.move_view(ViewMove {
            t: Some(t),
            ..Default::default()
        })?;
        status(
            ctx,
            if result.applied() {
                format!("Brightest frame for {}: t={t}", row.name)
            } else {
                did_not_move(&result)
            },
        )?;
    } else {
        navigation.decline_view()?;
        status(ctx, format!("{} covers no voxel", row.name))?;
    }
    Ok(done())
}
fn export(ctx: &Context, model: &Mutex<Model>) -> Result<()> {
    let outcome = (|| -> Result<String> {
        let answer = ctx.request_save_path(
            "regions.csv",
            "csv",
            "Export region means",
            Duration::from_secs(60 * 60),
        )?;
        if answer.declined {
            return Ok("Export cancelled".into());
        }
        let (csv, count) = {
            let model = model.lock().unwrap();
            (
                table::csv(
                    &table::ordered(model.rows.values()),
                    model.t,
                    model.c,
                    !model.float,
                ),
                model.rows.len(),
            )
        };
        std::fs::write(&answer.path, csv)?;
        Ok(format!("Exported {count} regions to {}", answer.path))
    })();
    status(
        ctx,
        outcome.unwrap_or_else(|e| format!("Export failed: {}", e.message)),
    )
}
fn tour(ctx: &Context, follow: &Follow, model: &Mutex<Model>) -> Result<()> {
    let rows = model.lock().unwrap().tour_rows(follow.resumed());
    for id in rows {
        if !follow.active() {
            break;
        }
        {
            let mut model = model.lock().unwrap();
            let current = table::ordered(model.rows.values());
            let Some(index) = current.iter().position(|row| row.id == id) else {
                continue;
            };
            let row = current[index];
            if !follow.move_view(ViewMove {
                frame_region: Some(id.clone()),
                ..Default::default()
            })? {
                break;
            }
            let key = (row.display_name().to_owned(), id);
            status(
                ctx,
                format!("Touring {} ({} of {})", key.0, index + 1, current.len()),
            )?;
            model.last_toured = Some(key);
        }
        if !follow.wait(Duration::from_millis(500)) {
            break;
        }
    }
    if follow.active() {
        status(ctx, "Tour finished")?;
        follow.end()?;
    } else if let Some(end) = follow.ended() {
        let reason = pb::FollowEndReason::try_from(end.reason)
            .map(|r| r.as_str_name().trim_start_matches("FOLLOW_END_").to_owned())
            .unwrap_or_else(|_| end.reason.to_string());
        status(ctx, format!("Tour stopped: {reason}"))?;
    }
    Ok(())
}

pub fn declare(
    model: Arc<Mutex<Model>>,
    traces: Arc<Traces>,
) -> std::result::Result<DeclaredPlugin, String> {
    let mut plugin = crate::declaration::builder();
    let [channel_spec, pick_spec, brightest_spec, export_spec] = crate::declaration::commands();
    let view_traces = traces.clone();
    let region_traces = traces.clone();
    let channel_traces = traces.clone();
    let shutdown_traces = traces.clone();
    let view = model.clone();
    let regions = model.clone();
    let channel = model.clone();
    let picking = model.clone();
    let bright = traces;
    let bright_model = model.clone();
    let touring = model.clone();
    plugin
        .on_shutdown(move |_, _| shutdown_traces.stop_and_join())
        .follow("quant", move |ctx, follow| tour(ctx, follow, &touring))
        .on_view_changed(move |ctx, change| {
            let mut model = view.lock().unwrap();
            view_traces.select(&change.selected_region_id);
            model.view(ctx, change, &view_traces)?;
            view_traces.sync(ctx, model.rows.values(), &model.selected)
        })
        .on_regions_changed(move |ctx, change| {
            let mut model = regions.lock().unwrap();
            region_traces.invalidate(ctx, &change)?;
            model.regions(ctx, change, &region_traces)?;
            region_traces.sync(ctx, model.rows.values(), &model.selected)
        });
    let channel = plugin.command(channel_spec, move |ctx, cmd| {
        let c = match cmd.args.get("value").and_then(|v| v.kind.as_ref()) {
            Some(pb::value::Kind::I(n)) => u32::try_from(*n).ok(),
            _ => None,
        }
        .ok_or_else(|| Error::protocol("channel must be a nonnegative integer"))?;
        let mut model = channel.lock().unwrap();
        if model.c == c {
            return Ok(done());
        }
        channel_traces.channel(ctx, c)?;
        model.channel(ctx, c)?;
        channel_traces.sync(ctx, model.rows.values(), &model.selected)?;
        Ok(done())
    });
    let pick = plugin.command(pick_spec, move |ctx, cmd| pick(ctx, cmd, &picking));
    let brightest = plugin.command(brightest_spec, move |ctx, cmd| {
        brightest(ctx, cmd, &bright, &bright_model)
    });
    let export = plugin.command(export_spec, move |ctx, _| {
        let model = model.clone();
        ctx.after_reply(move |ctx| export(&ctx, &model))?;
        Ok(done())
    });
    crate::declaration::panels(&mut plugin, [channel, pick, brightest, export]);
    plugin.build()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn resumed_tour_uses_order_key_even_after_deletion_and_equal_names() {
        let mut model = Model::default();
        for (id, name) in [("a", "same"), ("b", "same"), ("c", "later"), ("z", "")] {
            model.rows.insert(
                id.into(),
                Row {
                    id: id.into(),
                    name: name.into(),
                    measure: Measure::default(),
                },
            );
        }
        assert_eq!(model.tour_rows(true), ["c", "a", "b", "z"]);
        model.last_toured = Some(("same".into(), "a".into()));
        model.rows.remove("a");
        assert_eq!(model.tour_rows(true), ["b", "z"]);
        assert_eq!(model.tour_rows(false), ["c", "b", "z"]);
        model.last_toured = Some(("z".into(), "z".into()));
        assert!(model.tour_rows(true).is_empty());
    }
    #[test]
    fn deleted_measurement_removes_only_that_row_and_keeps_later_updates() {
        let mut model = Model::default();
        for id in ["a", "b", "c"] {
            model.rows.insert(
                id.into(),
                Row {
                    id: id.into(),
                    name: id.into(),
                    measure: Measure::default(),
                },
            );
        }
        let mut visited = Vec::new();
        model
            .refresh_with(|id| {
                visited.push(id.to_owned());
                if id == "b" {
                    Err(Error::new(
                        pb::error::Kind::NotFound,
                        "deleted during measure",
                    ))
                } else {
                    Ok(Measure { sum: 10, count: 2 })
                }
            })
            .unwrap();
        assert_eq!(visited, ["a", "b", "c"]);
        assert!(!model.rows.contains_key("b"));
        assert_eq!(model.rows["a"].measure, Measure { sum: 10, count: 2 });
        assert_eq!(model.rows["c"].measure, Measure { sum: 10, count: 2 });
        assert!(model
            .refresh_with(|_| Err(Error::internal("read failed")))
            .is_err());
    }

    #[test]
    fn failed_measure_preserves_its_numbers_and_commits_later_rows() {
        let mut model = Model::default();
        for id in ["a", "b", "c"] {
            model.rows.insert(
                id.into(),
                Row {
                    id: id.into(),
                    name: id.into(),
                    measure: Measure { count: 2, sum: 8 },
                },
            );
        }
        let error = model
            .refresh_with(|id| {
                if id == "b" {
                    Err(Error::internal("temporary"))
                } else {
                    Ok(Measure { count: 1, sum: 9 })
                }
            })
            .unwrap_err();
        assert_eq!(error.message, "temporary");
        assert_eq!(model.rows["b"].measure, Measure { count: 2, sum: 8 });
        assert_eq!(model.rows["c"].measure, Measure { count: 1, sum: 9 });
    }

    #[test]
    fn picking_has_a_bounded_wait_while_the_model_is_busy() {
        let model = Mutex::new(Model::default());
        let command = pb::InvokeCommand {
            args: [("row".into(), "a".into())].into_iter().collect(),
            ..Default::default()
        };
        let guard = model.lock().unwrap();
        let start = Instant::now();
        assert!(pick_row(&model, &command, Duration::from_millis(20)).is_none());
        assert!(start.elapsed() < Duration::from_secs(1));
        drop(guard);
        model.lock().unwrap().rows.insert(
            "a".into(),
            Row {
                id: "a".into(),
                name: "A".into(),
                measure: Measure::default(),
            },
        );
        assert_eq!(
            pick_row(&model, &command, Duration::ZERO).unwrap().name,
            "A"
        );
    }

    #[test]
    fn membership_selects_exact_voxels_and_checks_payload_size() {
        assert_eq!(
            masked_sum(&[0, 1, 0, 1], [100, 17, 300, 9].into_iter()).unwrap(),
            Measure { sum: 26, count: 2 }
        );
        assert_eq!(masked_sum(&[], [].into_iter()).unwrap(), Measure::default());
        assert!(masked_sum(&[1], [].into_iter()).is_err());
        assert!(masked_sum(&[1, 1], [u64::MAX, 1].into_iter()).is_err());
    }
}
