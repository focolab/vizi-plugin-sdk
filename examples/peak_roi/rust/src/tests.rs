use super::*;
use vizi::manifest::Manifest;

#[test]
fn manifest_is_valid_and_declares_the_registered_contributions() {
    let manifest = Manifest::parse(include_str!("../vizi-plugin.json")).unwrap();
    assert_eq!(manifest.contribution_ids("commands").len(), 5);
    assert_eq!(
        manifest.contribution_ids("panels"),
        ["peak".to_owned()].into()
    );
}

#[test]
fn decodes_little_endian_volumes_and_rejects_bad_payloads() {
    for (dtype, bytes, expected) in [
        (pb::DType::DtypeU8, vec![0, 255], vec![0.0, 255.0]),
        (
            pb::DType::DtypeU16,
            vec![0x34, 0x12, 0xff, 0xff],
            vec![4660.0, 65535.0],
        ),
        (
            pb::DType::DtypeF32,
            [(-1.5f32).to_le_bytes(), 8.25f32.to_le_bytes()].concat(),
            vec![-1.5, 8.25],
        ),
        (
            pb::DType::DtypeF64,
            [(-2.25f64).to_le_bytes(), 1e100f64.to_le_bytes()].concat(),
            vec![-2.25, 1e100],
        ),
    ] {
        let (values, shape) = analysis::decode(&bytes, dtype as i32, &[1, 1, 2]).unwrap();
        assert_eq!(values, expected);
        assert_eq!(shape, [1, 1, 2]);
    }
    for (bytes, shape) in [
        (vec![0], vec![1, 1, 2]),
        (vec![], vec![0, 1, 1]),
        (vec![0], vec![1, 1]),
    ] {
        assert!(analysis::decode(&bytes, pb::DType::DtypeU8 as i32, &shape).is_err());
    }
    let nonfinite = [f32::NAN, f32::NEG_INFINITY, f32::INFINITY];
    let bytes: Vec<_> = nonfinite.into_iter().flat_map(f32::to_le_bytes).collect();
    let (decoded, _) = analysis::decode(&bytes, pb::DType::DtypeF32 as i32, &[1, 1, 3]).unwrap();
    assert!(decoded[0].is_nan());
    assert_eq!(&decoded[1..], &[f64::NEG_INFINITY, f64::INFINITY]);
    assert!(analysis::decode(&[0], 999, &[1, 1, 1]).is_err());
}

#[test]
fn clamped_gaussian_matches_independent_direct_3d_convolution() {
    let shape = [3, 4, 5];
    let input: Vec<_> = (0..60).map(|i| ((i * 17 + 3) % 31) as f64).collect();
    let extents = [1.0, 2.0, 0.5];
    let actual = analysis::blur(input.clone(), shape, extents, || false).unwrap();
    // Independent full 3D convolution; no separable helper or builder constants.
    let radii = [2isize, 3, 1];
    for z in 0..3isize {
        for y in 0..4isize {
            for x in 0..5isize {
                let mut expected = 0.0;
                let mut normalization = 0.0;
                for dz in -radii[0]..=radii[0] {
                    for dy in -radii[1]..=radii[1] {
                        for dx in -radii[2]..=radii[2] {
                            let exponent = -2.0
                                * ((dz as f64 / extents[0]).powi(2)
                                    + (dy as f64 / extents[1]).powi(2)
                                    + (dx as f64 / extents[2]).powi(2));
                            let w = exponent.exp();
                            let index = ((z + dz).clamp(0, 2) * 20
                                + (y + dy).clamp(0, 3) * 5
                                + (x + dx).clamp(0, 4))
                                as usize;
                            expected += input[index] * w;
                            normalization += w;
                        }
                    }
                }
                assert!(
                    (actual[(z * 20 + y * 5 + x) as usize] - expected / normalization).abs()
                        < 1e-12
                );
            }
        }
    }
}

#[test]
fn known_blob_and_first_raster_tie() {
    let shape = [13, 15, 17];
    let mut blob = Vec::new();
    for z in 0..13i32 {
        for y in 0..15i32 {
            for x in 0..17i32 {
                blob.push((-((z - 6).pow(2) + (y - 7).pow(2) + (x - 8).pow(2)) as f64 / 4.0).exp());
            }
        }
    }
    let blurred = analysis::blur(blob, shape, [2.0, 3.0, 4.0], || false).unwrap();
    assert_eq!(analysis::peak(&blurred, shape), [6, 7, 8]);
    let ties = vec![-3.0, -1.0, -1.0, -5.0, -1.0, -1.0];
    assert_eq!(analysis::peak(&ties, [1, 2, 3]), [0, 0, 1]);
    assert_eq!(
        analysis::blur(ties.clone(), [1, 2, 3], [0.0; 3], || false).unwrap(),
        ties
    );
    assert_eq!(
        analysis::peak(
            &analysis::blur(vec![5.0; 24], [2, 3, 4], [1.0; 3], || false).unwrap(),
            [2, 3, 4]
        ),
        [0, 0, 0]
    );
}

#[test]
fn zero_tiny_invalid_extents_and_cancellation() {
    assert_eq!(
        analysis::blur(vec![7.0], [1; 3], [1e-300; 3], || false).unwrap(),
        [7.0]
    );
    for extent in [
        0.0,
        -0.0,
        -1.0,
        f64::INFINITY,
        f64::NEG_INFINITY,
        f64::NAN,
        1e100,
    ] {
        for axis in 0..3 {
            let mut extents = [1.0; 3];
            extents[axis] = extent;
            assert!(analysis::validate_extents(extents).is_err());
        }
    }
    assert!(analysis::validate_extents([1e-300; 3]).is_ok());
    assert!(analysis::blur(vec![1.0; 8], [2; 3], [1.0; 3], || true).is_err());
    assert!(analysis::blur(vec![1.0], [2; 3], [1.0; 3], || false).is_err());
}

#[test]
fn nan_and_negative_infinity_use_the_minimum_finite_voxel_before_blurring() {
    let expected = analysis::blur(
        vec![-4.0, -4.0, 12.0, 3.0, -1.0],
        [1, 1, 5],
        [1.0; 3],
        || false,
    )
    .unwrap();
    for bad in [f64::NAN, f64::NEG_INFINITY] {
        let actual = analysis::blur(
            vec![-4.0, bad, 12.0, 3.0, -1.0],
            [1, 1, 5],
            [1.0; 3],
            || false,
        )
        .unwrap();
        assert_eq!(actual, expected);
        assert_eq!(analysis::peak(&actual, [1, 1, 5]), [0, 0, 2]);
    }
    // Even the zero-sigma kernel sanitizes the input, while retaining +Inf.
    assert_eq!(
        analysis::blur(
            vec![f64::NAN, -7.0, f64::NEG_INFINITY, f64::INFINITY],
            [1, 2, 2],
            [0.0; 3],
            || false
        )
        .unwrap(),
        [-7.0, -7.0, -7.0, f64::INFINITY]
    );
}

#[test]
fn positive_infinity_wins_without_zero_taps_contaminating_other_voxels() {
    let input = vec![0.0, 1.0, f64::INFINITY, 2.0, 0.0];
    let tiny = analysis::blur(input.clone(), [1, 1, 5], [1e-300; 3], || false).unwrap();
    assert_eq!(tiny, input);
    assert_eq!(analysis::peak(&tiny, [1, 1, 5]), [0, 0, 2]);
    let blurred = analysis::blur(input, [1, 1, 5], [0.5; 3], || false).unwrap();
    assert!(blurred.iter().all(|v| !v.is_nan()));
    assert!(blurred[0].is_finite() && blurred[4].is_finite());
    assert_eq!(&blurred[1..4], &[f64::INFINITY; 3]);
    assert_eq!(analysis::peak(&blurred, [1, 1, 5]), [0, 0, 1]);
}

#[test]
fn all_nonfinite_volumes_fail_with_a_message() {
    for values in [
        vec![f64::NAN; 3],
        vec![f64::NEG_INFINITY; 3],
        vec![f64::INFINITY; 3],
        vec![f64::NAN, f64::NEG_INFINITY, f64::INFINITY],
    ] {
        let error = analysis::blur(values, [1, 1, 3], [1.0; 3], || false).unwrap_err();
        assert!(error.contains("no finite voxel"), "{error}");
    }
}

#[test]
fn ellipsoid_compile_operator_and_conic_geometry() {
    let contribution = roi::ellipsoid(&[[2, 3, 4]], [2.0, 3.0, 4.0], false);
    assert_eq!(contribution.contrib_id, "peak");
    let geometry = contribution.roi.unwrap();
    assert_eq!(geometry.type_name, "ellipsoid");
    assert_eq!(geometry.root, 0);
    let controls: Vec<[f64; 3]> = geometry
        .vertices
        .iter()
        .map(|v| {
            let p = v.position.as_ref().unwrap();
            [p.z, p.y, p.x]
        })
        .collect();
    assert_eq!(
        controls,
        [
            [2.5, 3.5, 4.5],
            [4.5, 3.5, 4.5],
            [2.5, 6.5, 4.5],
            [2.5, 3.5, 8.5]
        ]
    );
    let compile = geometry.compile.unwrap();
    assert_eq!(compile.controls, [0, 1, 2, 3]);
    assert_eq!(compile.num_display, 18);
    let mut display = [[0.0; 3]; 18];
    for triplet in compile.triplets {
        let control = controls[compile.controls[triplet.control as usize] as usize];
        for (axis, coordinate) in control.into_iter().enumerate() {
            display[triplet.display_vertex as usize][axis] += triplet.coeff * coordinate;
        }
    }
    // Absolute expectations independent of the builder's row/edge generation.
    assert_eq!(
        display,
        [
            [4.5, 3.5, 4.5],
            [0.5, 3.5, 4.5],
            [2.5, 6.5, 4.5],
            [2.5, 0.5, 4.5],
            [2.5, 3.5, 8.5],
            [2.5, 3.5, 0.5],
            [4.5, 6.5, 4.5],
            [0.5, 6.5, 4.5],
            [0.5, 0.5, 4.5],
            [4.5, 0.5, 4.5],
            [4.5, 3.5, 8.5],
            [0.5, 3.5, 8.5],
            [0.5, 3.5, 0.5],
            [4.5, 3.5, 0.5],
            [2.5, 6.5, 8.5],
            [2.5, 0.5, 8.5],
            [2.5, 0.5, 0.5],
            [2.5, 6.5, 0.5],
        ]
    );
    assert_eq!(geometry.edges.len(), 12);
    let mut pairs = std::collections::BTreeSet::new();
    for edge in &geometry.edges {
        assert!((0..6).contains(&edge.source) && (0..6).contains(&edge.target));
        assert!((6..18).contains(&edge.control));
        assert_ne!(edge.source / 2, edge.target / 2);
        assert!(pairs.insert((edge.source, edge.target)));
        assert!((edge.weight - 0.5f64.sqrt()).abs() < 1e-15);
        let mut norm = 0.0;
        for axis in 0..3 {
            let midpoint = (display[edge.source as usize][axis]
                + 2.0 * edge.weight * display[edge.control as usize][axis]
                + display[edge.target as usize][axis])
                / (2.0 + 2.0 * edge.weight);
            norm += ((midpoint - [2.5, 3.5, 4.5][axis]) / [2.0, 3.0, 4.0][axis]).powi(2);
        }
        assert!((norm - 1.0).abs() < 1e-14);
    }
    assert!(geometry.t_start.is_none() && geometry.t_end.is_none());
    assert!(geometry.pose_translation.is_empty());
    assert!(
        geometry.direction_specs.is_empty()
            && geometry.views.is_empty()
            && geometry.inclusion.is_none()
    );
}

#[test]
fn dense_pose_has_every_frame_and_signed_offsets() {
    let geometry = roi::ellipsoid(&[[5, 7, 9], [2, 8, 10], [4, 3, 8]], [1.0; 3], true)
        .roi
        .unwrap();
    assert_eq!(geometry.t_start, Some(0));
    assert_eq!(geometry.t_end, Some(2));
    assert_eq!(geometry.out_of_span, 0);
    assert_eq!(
        geometry.pose_translation,
        [0.0, 0.0, 0.0, -3.0, 1.0, 1.0, -1.0, -4.0, -1.0]
    );
    let single = roi::ellipsoid(&[[0; 3]], [1.0; 3], true).roi.unwrap();
    assert_eq!(single.t_end, Some(0));
    assert_eq!(single.pose_translation, [0.0; 3]);
}
