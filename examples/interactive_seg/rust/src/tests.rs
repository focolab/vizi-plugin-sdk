use crate::{
    analysis::{self, Selection},
    overlay, plot,
};
use prost::Message;
use std::sync::atomic::AtomicBool;
use vizi::{manifest::Manifest, proto as pb};

fn path(points: &[[f64; 2]]) -> Vec<[f64; 3]> {
    points.iter().map(|p| [0., p[0], p[1]]).collect()
}
#[test]
fn polygons_and_boundary_convention() {
    let square = path(&[[0., 0.], [0., 4.], [4., 4.], [4., 0.]]);
    assert!(analysis::inside(&square, 2., 2.));
    assert!(analysis::inside(&square, 2., 0.));
    assert!(!analysis::inside(&square, 2., 4.));
    assert!(!analysis::inside(&square, 5., 2.));
    let elbow = path(&[[0., 0.], [0., 3.], [1., 3.], [1., 1.], [3., 1.], [3., 0.]]);
    assert!(analysis::inside(&elbow, 0.5, 2.5));
    assert!(analysis::inside(&elbow, 2.5, 0.5));
    assert!(!analysis::inside(&elbow, 2., 2.));
    let bow = path(&[[0., 0.], [4., 4.], [0., 4.], [4., 0.]]);
    assert!(analysis::inside(&bow, 1., 0.5));
    assert!(analysis::inside(&bow, 1., 3.5));
    assert!(!analysis::inside(&bow, 1., 2.));
}
#[test]
fn selection_clips_off_volume_and_refuses_click() {
    let s = analysis::select(
        &path(&[[-10., -10.], [-10., 2.], [2., 2.], [2., -10.]]),
        [3, 5, 5],
    )
    .unwrap();
    assert_eq!(s.selected.iter().filter(|&&v| v).count(), 4);
    assert_eq!(s.origin, [0, 0, 0]);
    assert_eq!(s.shape, [3, 5, 5]);
    assert!(analysis::select(&[[0., 2., 2.]; 2], [3, 5, 5]).is_err());
}
#[test]
fn search_window_margin_and_ties_even() {
    let s = analysis::select(
        &path(&[[20., 20.], [20., 70.], [56., 70.], [56., 20.]]),
        [12, 100, 100],
    )
    .unwrap();
    assert_eq!(s.origin, [0, 11, 8]);
    assert_eq!(s.shape, [12, 54, 74]);
}
fn selection(shape: [usize; 3], selected: Vec<bool>) -> Selection {
    Selection {
        origin: [0; 3],
        shape,
        selected,
        volume_shape: shape,
    }
}
#[test]
fn six_connectivity_separates_edge_and_corner_contacts() {
    let mut w = vec![0.; 27];
    w[0] = 5.;
    w[4] = 5.;
    w[13] = 5.;
    let s = selection([3, 3, 3], vec![true; 9]);
    // 4 and 13 share a face, but 0 touches 4 only along an edge.
    let c = analysis::grow(&w, &s, 0, 5., true, 1, &AtomicBool::new(false)).unwrap();
    assert_eq!(c.voxels, vec![[0, 1, 1], [1, 1, 1]]);
    w[4] = 0.;
    let c = analysis::grow(&w, &s, 0, 5., true, 1, &AtomicBool::new(false)).unwrap();
    assert_eq!(c.voxels, vec![[0, 0, 0]]);
}
#[test]
fn largest_anchored_component_not_brightest_seed_and_ties_raster() {
    let w = [100., 0., 5., 5., 5., 0., 0., 0., 0., 0.];
    let s = selection([1, 2, 5], vec![true; 10]);
    let c = analysis::grow(&w, &s, 0, 5., true, 1, &AtomicBool::new(false)).unwrap();
    assert_eq!(c.voxels, vec![[0, 0, 2], [0, 0, 3], [0, 0, 4]]);
    let s = selection([1, 1, 5], vec![true; 5]);
    let c = analysis::grow(
        &[5., 5., 0., 5., 5.],
        &s,
        0,
        5.,
        true,
        1,
        &AtomicBool::new(false),
    )
    .unwrap();
    assert_eq!(c.voxels, vec![[0, 0, 0], [0, 0, 1]]);
}
#[test]
fn planar_growth_anchor_refusal_and_diagnostic_tie() {
    let w = [10., 0., 0., 0., 0., 0., 0., 10.];
    let s = selection([2, 2, 2], vec![true; 4]);
    let c = analysis::grow(&w, &s, 1, 10., false, 1, &AtomicBool::new(false)).unwrap();
    assert_eq!(c.voxels, vec![[1, 1, 1]]);
    let error = analysis::grow(&w, &s, 1, 11., true, 1, &AtomicBool::new(false))
        .err()
        .unwrap();
    assert!(error.contains("plane 1") && error.contains("z=0"));
    let error = analysis::grow(&w, &s, 0, 10., false, 2, &AtomicBool::new(false))
        .err()
        .unwrap();
    assert_eq!(error, "No component of at least 2 voxels.");
}
#[test]
fn lasso_anchors_without_clipping_component() {
    let s = selection([1, 1, 5], vec![false, false, true, false, false]);
    let c = analysis::grow(&[10.; 5], &s, 0, 5., false, 1, &AtomicBool::new(false)).unwrap();
    assert_eq!(c.voxels.len(), 5);
    assert!(!c.clipped);
    let mut clipped = s;
    clipped.volume_shape = [1, 1, 10];
    assert!(
        analysis::grow(
            &[10.; 5],
            &clipped,
            0,
            5.,
            false,
            1,
            &AtomicBool::new(false)
        )
        .unwrap()
        .clipped
    );
}
#[test]
fn accumulation_bbox_overlap_replace_and_cap() {
    let (one, id) = analysis::Labels::default()
        .add(&[[2, 4, 6], [2, 4, 7]], false)
        .unwrap();
    assert_eq!(id, 1);
    assert_eq!(one.origin, [2, 4, 6]);
    assert_eq!(one.shape, [1, 1, 2]);
    assert_eq!(one.bytes, [1, 1]);
    let (two, id) = one.add(&[[2, 4, 7], [3, 4, 7]], false).unwrap();
    assert_eq!(id, 2);
    assert_eq!(two.shape, [2, 1, 2]);
    assert_eq!(two.bytes, [1, 2, 0, 2]);
    let (replacement, id) = two.add(&[[9, 8, 7]], true).unwrap();
    assert_eq!(id, 1);
    assert_eq!(replacement.origin, [9, 8, 7]);
    assert_eq!(replacement.bytes, [1]);
    let full = analysis::Labels {
        next: 256,
        ..replacement
    };
    assert!(full.add(&[[1, 1, 1]], false).is_err());
    let lut = analysis::lut();
    assert_eq!(&lut[..4], &[0, 0, 0, 0]);
    assert_eq!(&lut[4..8], &[230, 25, 75, 180]);
    assert_eq!(&lut[36..40], &[230, 25, 75, 180]);
}
#[test]
fn preview_uniform_decimation_and_encoding() {
    assert_eq!(analysis::preview_indices(0), Vec::<usize>::new());
    assert_eq!(analysis::preview_indices(1), [0]);
    let indices = analysis::preview_indices(10_001);
    assert_eq!(indices.len(), 512);
    assert_eq!(indices[0], 0);
    assert_eq!(indices[511], 10_000);
    assert!(indices.windows(2).all(|w| w[0] < w[1]));
    let path: Vec<_> = (0..10_001)
        .map(|i| [99., i as f64 / 1000., i as f64 / 2000.])
        .collect();
    let update = overlay::trail(&path, 6, 42);
    let bytes = update.encode_to_vec();
    let decoded = pb::OverlayUpdate::decode(bytes.as_slice()).unwrap();
    assert_eq!(decoded, update);
    let Some(pb::overlay_item::Geometry::Marker(marker)) = &decoded.items[0].geometry else {
        panic!("not marker")
    };
    let geometry = marker.positions_zyx.as_ref().unwrap();
    assert_eq!(geometry.shape, [512, 3]);
    let Some(pb::geom_buffer::Source::Inline(raw)) = &geometry.source else {
        panic!("not inline")
    };
    assert!(raw
        .as_chunks::<24>()
        .0
        .iter()
        .all(|point| f64::from_le_bytes(point[..8].try_into().unwrap()) == 6.));
    eprintln!(
        "10,001-point lasso -> 512-point preview: {} encoded bytes (grant checked during publish)",
        bytes.len()
    );
}
#[test]
fn manifest_and_png_structure() {
    Manifest::parse(include_str!("../vizi-plugin.json")).unwrap();
    let png = plot::render(&[1., 2., 3.]);
    assert_eq!(&png[..8], b"\x89PNG\r\n\x1a\n");
    let mut at = 8;
    let mut types = vec![];
    while at < png.len() {
        let len = u32::from_be_bytes(png[at..at + 4].try_into().unwrap()) as usize;
        let body = &png[at + 4..at + 8 + len];
        let crc = u32::from_be_bytes(png[at + 8 + len..at + 12 + len].try_into().unwrap());
        assert_eq!(crc32fast::hash(body), crc);
        types.push(body[..4].to_vec());
        at += len + 12;
    }
    assert_eq!(
        types,
        vec![b"IHDR".to_vec(), b"IDAT".to_vec(), b"IEND".to_vec()]
    );
}
