use crate::analysis::{self, Labels};
use vizi::{control::Context, memory::Loan, proto as pb, PaneEpoch};

pub fn transform(origin: [usize; 3]) -> pb::OverlayTransform {
    pb::OverlayTransform {
        frame: "voxel".into(),
        position_zyx: origin.map(|v| v as f64).to_vec(),
        ..Default::default()
    }
}

pub fn trail(path: &[[f64; 3]], z: usize, revision: u64) -> pb::OverlayUpdate {
    let indices = analysis::preview_indices(path.len());
    let mut bytes = Vec::with_capacity(indices.len() * 24);
    for &i in &indices {
        for v in [z as f64, path[i][1], path[i][2]] {
            bytes.extend_from_slice(&v.to_le_bytes());
        }
    }
    pb::OverlayUpdate {
        overlay_id: "seg.trail".into(),
        revision,
        items: vec![pb::OverlayItem {
            item_id: "trail".into(),
            transform: Some(transform([0; 3])),
            geometry_version: revision,
            style: Some(pb::OverlayStyle {
                color_rgba: vec![255, 255, 255, 220],
                ..Default::default()
            }),
            geometry: Some(pb::overlay_item::Geometry::Marker(pb::OverlayMarker {
                positions_zyx: Some(pb::GeomBuffer {
                    source: Some(pb::geom_buffer::Source::Inline(bytes)),
                    shape: vec![indices.len() as u32, 3],
                    dtype: pb::DType::DtypeF64 as i32,
                }),
                size_px: 5.0,
                shape: "disc".into(),
            })),
        }],
    }
}

pub fn publish_trail(
    ctx: &Context,
    update: &pb::OverlayUpdate,
    pane: PaneEpoch,
) -> Result<(), String> {
    ctx.set_overlay_live(update, Some(pane))
        .map(|_| ())
        .map_err(|e| e.to_string())
}

pub struct ReservedItem {
    pub item: pb::OverlayItem,
    pub loans: Vec<Loan>,
}

pub fn label_item(
    ctx: &Context,
    labels: &Labels,
    t: u32,
    version: u64,
) -> Result<ReservedItem, String> {
    let segment = ctx.segment("O", "seg.labels").map_err(|e| e.to_string())?;
    let mut loans = Vec::new();
    let buffers = [
        (&labels.bytes, labels.shape.map(|v| v as u32).to_vec()),
        (&analysis::lut(), vec![256, 4]),
    ]
    .into_iter()
    .map(|(bytes, shape)| {
        let loan = ctx
            .memory()
            .reserve(segment.segment_id, bytes)
            .map_err(|e| e.to_string())?;
        let descriptor = pb::SegmentRef {
            segment_id: loan.segment_id,
            offset: loan.offset,
            len: loan.len,
            shape_zyx: shape.clone(),
            dtype: pb::DType::DtypeU8 as i32,
            uses: 1,
        };
        loans.push(loan);
        Ok(pb::GeomBuffer {
            source: Some(pb::geom_buffer::Source::Segment(descriptor)),
            shape,
            dtype: pb::DType::DtypeU8 as i32,
        })
    })
    .collect::<Result<Vec<_>, String>>();
    let mut buffers = match buffers {
        Ok(v) => v,
        Err(e) => {
            for loan in loans {
                let _ = ctx.memory().cancel_unsent(loan);
            }
            return Err(e);
        }
    };
    let colormap = buffers.pop().unwrap();
    let data = buffers.pop().unwrap();
    let names = labels
        .bytes
        .iter()
        .copied()
        .filter(|&v| v != 0)
        .map(|id| (u32::from(id), format!("Region {id}")))
        .collect();
    Ok(ReservedItem {
        loans,
        item: pb::OverlayItem {
            item_id: "labels".into(),
            transform: Some(transform(labels.origin)),
            style: None,
            geometry_version: version,
            geometry: Some(pb::overlay_item::Geometry::LabelVolume(
                pb::OverlayLabelVolume {
                    labels: Some(data),
                    colormap: Some(colormap),
                    t: Some(t),
                    label_names: names,
                    label_visible: Default::default(),
                },
            )),
        },
    })
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;
    use std::{
        fs::File,
        os::{fd::FromRawFd, unix::fs::FileExt},
        sync::Arc,
    };
    use vizi::{
        control::Runtime, manifest::Manifest, plugin::Registry, Doorbell, MemoryGrant, Permissions,
    };

    #[test]
    fn failed_lut_reservation_reclaims_labels_without_a_host_release() {
        let manifest = Manifest::parse(r#"{"manifest_version":1,"id":"test.labels","name":"Labels","version":"1","engines":{"vizi":"1.0"},"entry":{"command":["test"]},"contributions":{"commands":[{"id":"run","title":"Run"}],"overlays":[{"id":"seg.labels"}]}}"#).unwrap();
        let (fd, _peer) = vizi::socket_pair().unwrap();
        let (bell, listener) = Doorbell::new(fd, Default::default()).unwrap();
        let runtime = Runtime::new(manifest, Arc::new(Registry::new()), bell, listener).unwrap();
        let ctx = runtime.context();
        let fd = unsafe {
            libc::memfd_create(
                c"label-builder-test".as_ptr(),
                libc::MFD_CLOEXEC | libc::MFD_ALLOW_SEALING,
            )
        };
        assert!(fd >= 0);
        let file = unsafe { File::from_raw_fd(fd) };
        file.set_len(1088).unwrap();
        let mut preamble = [0; 32];
        preamble[..8].copy_from_slice(&1u64.to_le_bytes());
        let crc = vizi::crc32(&preamble[..28]);
        preamble[28..].copy_from_slice(&crc.to_le_bytes());
        file.write_all_at(&preamble, 0).unwrap();
        assert_eq!(
            unsafe {
                libc::fcntl(
                    fd,
                    libc::F_ADD_SEALS,
                    libc::F_SEAL_GROW | libc::F_SEAL_SHRINK,
                )
            },
            0
        );
        let grant = MemoryGrant {
            segment_id: 1,
            size: 1088,
            permissions: Permissions::new(15).unwrap(),
        };
        ctx.memory()
            .grant(grant.clone(), grant.encode(1).unwrap(), file.into())
            .unwrap();
        ctx.memory()
            .install(pb::InstallSegment {
                segment_id: 1,
                purpose: "O".into(),
                subject: "seg.labels".into(),
                regions: vec![pb::Region {
                    index: 0,
                    offset: 64,
                    extent: 1024,
                    discipline: 4,
                    producer: 2,
                    ..Default::default()
                }],
                ..Default::default()
            })
            .unwrap();
        assert_eq!(ctx.memory().query(1).state, 3);
        let labels = Labels {
            bytes: vec![1; 64],
            shape: [1, 1, 64],
            ..Default::default()
        };
        for _ in 0..3 {
            let error = label_item(&ctx, &labels, 0, 1)
                .err()
                .expect("LUT must not fit after labels");
            assert!(error.contains("arena full"));
            // No host was sent a descriptor or release. Only cancel_unsent can
            // make the entire arena available again after the builder fails.
            let whole = ctx.memory().reserve(1, &[0; 1024]).unwrap();
            assert_eq!(whole.offset, 64);
            ctx.memory().cancel_unsent(whole).unwrap();
        }
        let reports = ctx.memory().drain_events().unwrap();
        assert!(
            matches!(reports.as_slice(), [pb::plugin_msg::Msg::GrantAck(_), pb::plugin_msg::Msg::SegmentShortfall(s)] if s.dropped == 3)
        );
        runtime.stop();
    }
}
