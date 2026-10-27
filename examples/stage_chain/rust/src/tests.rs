use super::*;
use arithmetic::placement;
use vizi::manifest::Manifest;
use vizi::stage::{Pose, Voxel};

fn plane(shape: [u32; 3], values: &[Voxel]) -> Plane {
    let dtype = match values[0] {
        Voxel::U8(_) => pb::DType::DtypeU8,
        Voxel::U16(_) => pb::DType::DtypeU16,
        Voxel::F32(_) => pb::DType::DtypeF32,
        Voxel::F64(_) => pb::DType::DtypeF64,
    };
    let mut plane = Plane {
        shape,
        dtype,
        bytes: vec![],
    };
    assert_eq!(shape.iter().product::<u32>() as usize, values.len());
    plane.bytes.resize(values.len() * plane.width(), 0);
    for (i, &v) in values.iter().enumerate() {
        let i = i as u32;
        plane.set(
            [
                i / (shape[1] * shape[2]),
                i / shape[2] % shape[1],
                i % shape[2],
            ],
            v,
        );
    }
    plane
}

fn row(values: &[Voxel]) -> Plane {
    plane([1, 1, values.len() as u32], values)
}
fn all(p: &Plane) -> [[u32; 2]; 3] {
    p.shape.map(|n| [0, n])
}
fn values(p: &Plane) -> Vec<Voxel> {
    (0..p.shape[0])
        .flat_map(|z| {
            (0..p.shape[1]).flat_map(move |y| (0..p.shape[2]).map(move |x| p.voxel([z, y, x])))
        })
        .collect()
}
fn identity() -> Pose {
    Pose {
        rotation: [1., 0., 0., 0., 1., 0., 0., 0., 1.],
        translation: [0.; 3],
    }
}

#[test]
fn gain_rounds_both_tie_directions_and_clamps() {
    for (factor, expected) in [(1.5, [2, 4, 8, 10]), (0.5, [0, 2, 2, 4])] {
        let mut p = row(&[1, 3, 5, 7].map(Voxel::U16));
        p.gain(all(&p), factor);
        assert_eq!(values(&p), expected.map(Voxel::U16));
        let mut p = row(&[1, 3, 5, 7].map(Voxel::U8));
        p.gain(all(&p), factor);
        assert_eq!(values(&p), expected.map(|v| Voxel::U8(v as u8)));
    }
    let mut p = row(&[Voxel::U16(40000), Voxel::U16(65535)]);
    p.gain(all(&p), 2.);
    assert_eq!(values(&p), [Voxel::U16(65535); 2]);
    let mut p = row(&[Voxel::U8(200), Voxel::U8(255)]);
    p.gain(all(&p), 2.);
    assert_eq!(values(&p), [Voxel::U8(255); 2]);
}

#[test]
fn gain_float_and_extent_preservation() {
    let mut p = row(&[Voxel::F32(-2.), Voxel::F32(1.25), Voxel::F32(5.)]);
    p.gain([[0, 1], [0, 1], [1, 2]], 1.5);
    assert_eq!(
        values(&p),
        [Voxel::F32(-2.), Voxel::F32(1.875), Voxel::F32(5.)]
    );
    let mut p = row(&[Voxel::F64(-2.5)]);
    p.gain(all(&p), 1.5);
    assert_eq!(values(&p), [Voxel::F64(-3.75)]);
}

#[test]
fn diff_signed_difference_equal_inputs_and_zero_padding() {
    let a = row(&[65535, 65535, 0, 42].map(Voxel::U16));
    let b = row(&[0, 0, 65535, 42].map(Voxel::U16));
    let extent = [[0, 1], [0, 1], [1, 3]];
    assert_eq!(
        values(&a.diff(&b, extent)),
        [0, 65535, 65535, 0].map(Voxel::U16)
    );
    assert_eq!(values(&a.diff(&a, all(&a))), [Voxel::U16(0); 4]);
    assert_eq!(values(&a), [65535, 65535, 0, 42].map(Voxel::U16));
    let a = row(&[Voxel::F32(16777216.), Voxel::F32(-1.25)]);
    let b = row(&[Voxel::F32(-1.), Voxel::F32(2.5)]);
    assert_eq!(
        values(&a.diff(&b, all(&a))),
        [Voxel::F32(16777216.), Voxel::F32(3.75)]
    );
    let a = row(&[Voxel::U8(0), Voxel::U8(255)]);
    let b = row(&[Voxel::U8(255), Voxel::U8(0)]);
    assert_eq!(values(&a.diff(&b, all(&a))), [Voxel::U8(255); 2]);
    let a = row(&[Voxel::F64(-1.25)]);
    let b = row(&[Voxel::F64(2.5)]);
    assert_eq!(values(&a.diff(&b, all(&a))), [Voxel::F64(3.75)]);
}

#[test]
fn widen_copies_window_and_rounds_f64() {
    let p = plane([2, 4, 5], &(0..40).map(Voxel::U16).collect::<Vec<_>>());
    let result = p.widen(1);
    assert_eq!(result.shape, [2, 2, 3]);
    assert_eq!(
        values(&result),
        [6, 7, 8, 11, 12, 13, 26, 27, 28, 31, 32, 33].map(|v| Voxel::F32(v as f32))
    );
    let p = row(&[
        Voxel::F64(1. + 2f64.powi(-24)),
        Voxel::F64(1. + 3. * 2f64.powi(-24)),
    ]);
    assert_eq!(
        values(&p.widen(0)),
        [Voxel::F32(1.), Voxel::F32(1. + 2f32.powi(-22))]
    );
}

#[test]
fn crop_placement_handles_nonzero_and_cut_extents() {
    assert_eq!(
        placement([[1, 3], [3, 7], [4, 8]], [10., 20., 30.], [4, 6, 6], 2),
        ([[1, 3], [1, 5], [2, 6]], [10., 20., 30.])
    );
    assert_eq!(
        placement([[1, 3], [1, 9], [0, 7]], [10., 20., 30.], [4, 6, 6], 2),
        ([[1, 3], [0, 6], [0, 5]], [10., 21., 32.])
    );
    assert_eq!(
        placement([[0, 4], [8, 10], [0, 1]], [0.; 3], [4, 6, 6], 2),
        ([[0, 4], [6, 6], [0, 0]], [0., 0., 2.])
    );
}

#[test]
fn centroid_uses_slot_indices_and_extent_origin_with_exact_sums() {
    let mut p = plane([3, 5, 5], &[Voxel::U16(0); 75]);
    p.set([0, 0, 0], Voxel::U16(65535)); // outside the extent
    p.set([1, 2, 3], Voxel::U16(3));
    p.set([2, 4, 4], Voxel::U16(1));
    assert_eq!(
        p.centroid([[1, 3], [2, 5], [3, 5]], [10., 20., 30.]),
        Some([10.25, 20.5, 30.25])
    );
    assert_eq!(p.centroid([[0, 0], [0, 5], [0, 5]], [0.; 3]), None);
    let p = row(&[Voxel::U8(0); 3]);
    assert_eq!(p.centroid(all(&p), [0.; 3]), None);
}

#[test]
fn drift_gap_forgets_and_arms_are_independent() {
    let mut drift = Drift::default();
    for key in [(1, 1), (1, 2), (2, 1)] {
        assert!(drift.accept_time(key, 0));
        drift.apply(
            key,
            0,
            Some([key.0 as f64, key.1 as f64, 3.]),
            &mut identity(),
        );
    }
    assert!(!drift.accept_time((1, 1), 2));
    let mut pose = identity();
    assert!(drift.accept_time((1, 1), 3));
    drift.apply((1, 1), 3, Some([9.; 3]), &mut pose);
    assert_eq!(pose, identity());
    for key in [(1, 2), (2, 1)] {
        let mut pose = identity();
        assert!(drift.accept_time(key, 1));
        drift.apply(key, 1, Some([4., 5., 6.]), &mut pose);
        assert_eq!(
            pose.translation,
            [key.0 as f64 - 4., key.1 as f64 - 5., -3.]
        );
    }
}

#[test]
fn drift_empty_reference_stays_empty_and_pose_composes_with_old_rotation() {
    let mut drift = Drift::default();
    let key = (1, 1);
    drift.apply(key, 0, None, &mut identity());
    let mut pose = identity();
    assert!(drift.accept_time(key, 1));
    drift.apply(key, 1, Some([1., 2., 3.]), &mut pose);
    assert_eq!(pose, identity());
    assert!(!drift.accept_time(key, 3));
    assert!(drift.accept_time(key, 4));
    drift.apply(key, 4, Some([1., 2., 3.]), &mut pose);
    let mut pose = Pose {
        rotation: [0., -1., 0., 1., 0., 0., 0., 0., 1.],
        translation: [10., 20., 30.],
    };
    assert!(drift.accept_time(key, 5));
    let rotation = pose.rotation;
    drift.apply(key, 5, Some([2., 4., 6.]), &mut pose);
    assert_eq!(pose.translation, [12., 19., 27.]);
    assert_eq!(pose.rotation, rotation);
    let before = pose.clone();
    assert!(drift.accept_time(key, 6));
    drift.apply(key, 6, None, &mut pose);
    assert_eq!(pose, before);
}

#[test]
fn manifest_validates_and_equal_diff_inputs_are_planned() {
    let manifest = Manifest::parse(include_str!("../vizi-plugin.json")).unwrap();
    let json = manifest.value();
    assert!(json["permissions"].as_array().unwrap().is_empty());
    let contributions = json["contributions"].as_object().unwrap();
    assert_eq!(contributions.len(), 1);
    assert_eq!(
        contributions["stages"]
            .as_array()
            .unwrap()
            .iter()
            .map(|s| s["id"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["gain", "diff", "widen", "drift"]
    );
    let shape = pb::StageShape {
        channels: vec![7, 2],
        z: 2,
        y: 5,
        x: 5,
        dtype: pb::DType::DtypeU16 as i32,
    };
    let params = [
        ("a".into(), ParamValue::Int(7)),
        ("b".into(), ParamValue::Int(7)),
    ]
    .into_iter()
    .collect();
    let plan = Kind::Diff.plan(&shape, &params).unwrap();
    assert_eq!(plan.adds[0].needs, [7, 7]);
}
