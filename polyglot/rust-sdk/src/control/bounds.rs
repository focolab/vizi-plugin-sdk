use super::*;
use prost_reflect::{DescriptorPool, DynamicMessage, MapKey, ReflectMessage, Value};
use std::sync::OnceLock;
fn pool() -> &'static DescriptorPool {
    static POOL: OnceLock<DescriptorPool> = OnceLock::new();
    POOL.get_or_init(|| {
        DescriptorPool::decode(include_bytes!(concat!(env!("OUT_DIR"), "/vizi.bin")).as_slice())
            .expect("generated descriptor set")
    })
}
fn walk(message: &DynamicMessage, loans: &mut Vec<pb::SegmentRef>) -> Result<()> {
    match message.descriptor().full_name() {
        "vizi.plugin.v1.SegmentRef" => loans.push(message.transcode_to().unwrap()),
        "vizi.plugin.v1.ImageRef" => {
            let image: pb::ImageRef = message.transcode_to().unwrap();
            loans.push(pb::SegmentRef {
                segment_id: image.segment_id,
                offset: image.offset,
                len: image.len,
                ..Default::default()
            });
        }
        _ => {}
    }
    for (field, v) in message.fields() {
        let name = field.full_name();
        let cap = if name.ends_with("TimeMapping.t")
            || name.ends_with("Roi.pose_translation")
            || name.ends_with("Roi.pose_rotation")
            || name.ends_with("Roi.pose_stretch")
            || name.ends_with("RoiPoseExtension.pose_translation")
            || name.ends_with("RoiPoseExtension.pose_rotation")
            || name.ends_with("RoiPoseExtension.pose_stretch")
        {
            1_048_576
        } else if name.ends_with("Geometry.levels") {
            64
        } else if name.ends_with("SeriesList.entries") {
            MAX_SERIES
        } else {
            65_536
        };
        match v {
            Value::List(v) if v.len() > cap => {
                return Err(Error::protocol(format!("repeated field overflow: {name}")))
            }
            Value::Map(v) if v.len() > cap => {
                return Err(Error::protocol(format!("map field overflow: {name}")))
            }
            _ => {}
        }
        walk_value(v, name, loans)?;
    }
    Ok(())
}
fn inline_size(len: usize, name: &str) -> Result<()> {
    if len > MAX_INLINE_BYTES {
        return Err(Error::protocol(format!(
            "inline field exceeds 8 MiB: {name}"
        )));
    }
    Ok(())
}
fn walk_value(v: &Value, name: &str, loans: &mut Vec<pb::SegmentRef>) -> Result<()> {
    match v {
        Value::Bytes(v) => inline_size(v.len(), name)?,
        Value::String(v) => inline_size(v.len(), name)?,
        Value::Message(m) => walk(m, loans)?,
        Value::List(v) => {
            for v in v {
                walk_value(v, name, loans)?;
            }
        }
        Value::Map(v) => {
            for (key, v) in v {
                if let MapKey::String(key) = key {
                    inline_size(key.len(), name)?;
                }
                walk_value(v, name, loans)?;
            }
        }
        _ => {}
    }
    Ok(())
}
fn check<M: Message>(message: &M, name: &str) -> Result<Vec<pb::SegmentRef>> {
    if message.encoded_len() > MAX_MESSAGE_BYTES {
        return Err(Error::protocol("message exceeds 32 MiB"));
    }
    let d = pool().get_message_by_name(name).unwrap();
    let dynamic = DynamicMessage::decode(d, message.encode_to_vec().as_slice())
        .map_err(|e| Error::protocol(e.to_string()))?;
    let mut loans = Vec::new();
    walk(&dynamic, &mut loans)?;
    Ok(loans)
}
pub(super) fn host(message: &pb::HostMsg) -> Result<()> {
    check(message, "vizi.plugin.v1.HostMsg").map(|_| ())
}
pub(super) fn plugin(message: &pb::PluginMsg) -> Result<Vec<pb::SegmentRef>> {
    check(message, "vizi.plugin.v1.PluginMsg")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn series_lists_exceed_repeated_limit_but_trim_to_message_budget() {
        use crate::plugin::{SeriesChoice, SeriesEntry};
        for count in [60_000, 65_537] {
            let entries = (0..count)
                .map(|i| SeriesEntry {
                    id: format!("{i:020}"),
                    label: "x".repeat(20),
                    ..Default::default()
                })
                .collect();
            let list = SeriesChoice(entries).into_list().unwrap();
            assert_eq!(list.entries.len(), count);
            assert!(!list.truncated);
            plugin(&pb::PluginMsg {
                msg: Some(Plugin::SeriesList(list)),
                ..Default::default()
            })
            .unwrap();
        }
        let entries: Vec<_> = (0..40_000)
            .map(|i| SeriesEntry {
                id: i.to_string(),
                label: "x".repeat(1000),
                ..Default::default()
            })
            .collect();
        let next_cost = |entry: &SeriesEntry| {
            entry.encoded_len() + 1 + prost::length_delimiter_len(entry.encoded_len())
        };
        let list = SeriesChoice(entries.clone()).into_list().unwrap();
        let count = list.entries.len();
        assert!(list.truncated);
        assert_eq!(list.entries, entries[..count]);
        let used: usize = list.entries.iter().map(next_cost).sum();
        assert!(used <= MAX_MESSAGE_BYTES - 64);
        assert!(used + next_cost(&entries[count]) > MAX_MESSAGE_BYTES - 64);
        let message = pb::PluginMsg {
            id: "open".into(),
            msg: Some(Plugin::SeriesList(list)),
        };
        plugin(&message).unwrap();
        assert_eq!(
            pb::PluginMsg::decode(message.encode_to_vec().as_slice()).unwrap(),
            message
        );
    }

    #[test]
    fn chunk_and_table_descriptors_are_walked_as_loans() {
        let descriptor = pb::SegmentRef {
            segment_id: 3,
            offset: 64,
            len: 1024,
            uses: 1,
            ..Default::default()
        };
        let buffer = pb::GeomBuffer {
            source: Some(pb::geom_buffer::Source::Segment(descriptor.clone())),
            ..Default::default()
        };
        for msg in [
            Plugin::OverlayChunk(pb::OverlayChunk {
                labels: Some(buffer.clone()),
                ..Default::default()
            }),
            Plugin::OverlayLabelTable(pb::OverlayLabelTable {
                colormap: Some(buffer),
                ..Default::default()
            }),
        ] {
            assert_eq!(
                plugin(&pb::PluginMsg {
                    msg: Some(msg),
                    ..Default::default()
                })
                .unwrap(),
                vec![descriptor.clone()]
            );
        }
    }
    #[test]
    fn bounds_allow_time_axis_exception_but_refuse_other_overflows() {
        let mut m = pb::HostMsg {
            id: String::new(),
            msg: Some(Host::TimeMapping(pb::TimeMapping {
                gen: 1,
                revision: 1,
                t: vec![0.; 65537],
            })),
        };
        assert!(host(&m).is_ok());
        m.msg = Some(Host::EpochChange(pb::EpochChange {
            panes: vec![pb::PaneEpoch::default(); 65537],
            datasets: vec![],
        }));
        assert!(host(&m).is_err());
        m.msg = Some(Host::ExportChunk(pb::ExportChunk {
            source: Some(pb::export_chunk::Source::Inline(vec![
                0;
                MAX_INLINE_BYTES + 1
            ])),
            ..Default::default()
        }));
        assert!(host(&m).is_err());
    }
    #[test]
    fn long_roi_poses_use_the_time_axis_limit() {
        let roi = pb::pois_math::v1::Roi {
            pose_stretch: vec![0.; 65537],
            ..Default::default()
        };
        let m = pb::HostMsg {
            id: "q1".into(),
            msg: Some(Host::Rois(pb::RoiList {
                rois: vec![pb::RoiRecord {
                    roi: Some(roi),
                    ..Default::default()
                }],
            })),
        };
        assert!(host(&m).is_ok());
    }

    #[test]
    fn pose_extensions_use_the_pose_limit_for_every_component() {
        for field in ["pose_translation", "pose_rotation", "pose_stretch"] {
            for len in [65_537, 1_048_576, 1_048_577] {
                let mut extension = pb::RoiPoseExtension::default();
                *match field {
                    "pose_translation" => &mut extension.pose_translation,
                    "pose_rotation" => &mut extension.pose_rotation,
                    _ => &mut extension.pose_stretch,
                } = vec![0.; len];
                let message = pb::PluginMsg {
                    msg: Some(Plugin::ExtendRoiPoses(pb::ExtendRoiPoses {
                        extensions: vec![extension],
                    })),
                    ..Default::default()
                };
                assert_eq!(plugin(&message).is_ok(), len <= 1_048_576, "{field}: {len}");
            }
        }
    }

    #[test]
    fn strings_obey_the_utf8_byte_limit_in_both_directions() {
        for extra in [false, true] {
            let mut text = "é".repeat(MAX_INLINE_BYTES / 2);
            if extra {
                text.push('x');
            }
            let incoming = pb::HostMsg {
                msg: Some(Host::OpenExport(pb::OpenExport {
                    source_metadata: Some(pb::MetadataDoc {
                        text: text.clone(),
                        ..Default::default()
                    }),
                    ..Default::default()
                })),
                ..Default::default()
            };
            let outgoing = pb::PluginMsg {
                msg: Some(Plugin::Metadata(pb::MetadataResult {
                    text,
                    ..Default::default()
                })),
                ..Default::default()
            };
            for result in [host(&incoming), plugin(&outgoing).map(|_| ())] {
                if extra {
                    assert_eq!(result.unwrap_err().kind, pb::error::Kind::Protocol);
                } else {
                    result.unwrap();
                }
            }
        }
    }

    #[test]
    fn non_inline_named_bytes_and_label_name_map_values_are_bounded() {
        let bytes = pb::PluginMsg {
            msg: Some(Plugin::DoorbellFault(pb::DoorbellFault {
                capture: Some(vec![0; MAX_INLINE_BYTES + 1]),
                ..Default::default()
            })),
            ..Default::default()
        };
        assert_eq!(plugin(&bytes).unwrap_err().kind, pb::error::Kind::Protocol);
        for len in [MAX_INLINE_BYTES, MAX_INLINE_BYTES + 1] {
            let message = pb::PluginMsg {
                msg: Some(Plugin::OverlayUpdate(pb::OverlayUpdate {
                    items: vec![pb::OverlayItem {
                        geometry: Some(pb::overlay_item::Geometry::LabelVolume(
                            pb::OverlayLabelVolume {
                                label_names: [(256, "x".repeat(len))].into_iter().collect(),
                                ..Default::default()
                            },
                        )),
                        ..Default::default()
                    }],
                    ..Default::default()
                })),
                ..Default::default()
            };
            assert_eq!(plugin(&message).is_ok(), len == MAX_INLINE_BYTES);
        }
    }

    #[test]
    fn scalar_values_inside_lists_and_maps_and_string_keys_are_bounded() {
        for value in [
            Value::String("x".repeat(MAX_INLINE_BYTES + 1)),
            Value::Bytes(vec![0; MAX_INLINE_BYTES + 1].into()),
        ] {
            for container in [
                Value::List(vec![value.clone()]),
                Value::Map([(MapKey::U32(1), value.clone())].into_iter().collect()),
            ] {
                assert_eq!(
                    walk_value(&container, "fixture", &mut Vec::new())
                        .unwrap_err()
                        .kind,
                    pb::error::Kind::Protocol
                );
            }
        }
        let keys = Value::Map(
            [(
                MapKey::String("x".repeat(MAX_INLINE_BYTES + 1)),
                Value::U32(1),
            )]
            .into_iter()
            .collect(),
        );
        assert!(walk_value(&keys, "fixture", &mut Vec::new()).is_err());
    }
}
