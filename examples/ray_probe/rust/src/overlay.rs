use crate::geometry::Point;
use pb::overlay_item::Geometry;
use vizi::proto as pb;
fn positions(points: &[Point]) -> pb::GeomBuffer {
    pb::GeomBuffer {
        source: Some(pb::geom_buffer::Source::Inline(
            points
                .iter()
                .flatten()
                .flat_map(|v| v.to_le_bytes())
                .collect(),
        )),
        shape: vec![points.len() as u32, 3],
        dtype: pb::DType::DtypeF64 as i32,
    }
}
fn item(id: String, revision: u64, geometry: Geometry) -> pb::OverlayItem {
    pb::OverlayItem {
        item_id: id,
        geometry_version: revision,
        transform: Some(pb::OverlayTransform {
            frame: "voxel".into(),
            ..Default::default()
        }),
        style: Some(pb::OverlayStyle {
            color_rgba: vec![255, 204, 0, 255],
            ..Default::default()
        }),
        geometry: Some(geometry),
    }
}
pub fn aim(points: [Point; 2], revision: u64) -> pb::OverlayUpdate {
    pb::OverlayUpdate {
        overlay_id: "probe.aim".into(),
        revision,
        items: vec![item(
            "aim".into(),
            revision,
            Geometry::LineSet(pb::OverlayLineSet {
                positions_zyx: Some(positions(&points)),
                topology: "segments".into(),
                ..Default::default()
            }),
        )],
    }
}
pub fn picks(points: &[Point], revision: u64) -> pb::OverlayUpdate {
    let mut items = Vec::new();
    if !points.is_empty() {
        items.push(item(
            "picks".into(),
            revision,
            Geometry::Marker(pb::OverlayMarker {
                positions_zyx: Some(positions(points)),
                size_px: 8.,
                shape: "disc".into(),
            }),
        ));
        for (i, p) in points.iter().enumerate() {
            items.push(item(
                format!("label-{}", i + 1),
                revision,
                Geometry::Text(pb::OverlayText {
                    text: (i + 1).to_string(),
                    anchor_zyx: p.to_vec(),
                    size_px: 0.,
                }),
            ));
        }
    }
    pb::OverlayUpdate {
        overlay_id: "probe.picks".into(),
        revision,
        items,
    }
}
