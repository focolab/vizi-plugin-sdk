use super::*;

fn close(a: f64, b: f64) {
    assert!((a - b).abs() <= 1e-9, "{a} != {b}");
}
fn close_point(a: Point, b: Point) {
    for k in 0..3 {
        close(a[k], b[k]);
    }
}
fn volume(p: &[Point]) -> f64 {
    dot(sub(p[1], p[0]), cross(sub(p[2], p[0]), sub(p[3], p[0]))).abs() / 6.
}
fn edit(p: &[Point], h: i32, p1: Point) -> pb::ShapeEdit {
    pb::ShapeEdit {
        type_name: "tetra.volume".into(),
        r#move: Some(roi::ConstrainMoveRequest {
            roi: Some(roi::Roi {
                vertices: p
                    .iter()
                    .map(|&p| roi::Vertex {
                        position: Some(wire(p)),
                        ..Default::default()
                    })
                    .collect(),
                ..Default::default()
            }),
            handle: h,
            p1: Some(wire(p1)),
            ..Default::default()
        }),
        ..Default::default()
    }
}
fn face(p: &[Point], h: usize) -> (Point, Point) {
    let f: Vec<_> = (0..4).filter(|&i| i != h).map(|i| p[i]).collect();
    let g = scale(add(add(f[0], f[1]), f[2]), 1. / 3.);
    let n = cross(sub(f[1], f[0]), sub(f[2], f[0]));
    (g, scale(n, 1. / norm(n)))
}
fn at_height(p: &[Point], h: usize, ratio: f64) -> Point {
    let (g, n) = face(p, h);
    let ray = sub(p[h], p[4]);
    let t = (ratio * dot(n, sub(p[h], g)) - dot(n, sub(p[4], g))) / dot(n, ray);
    add(p[4], scale(ray, t))
}

#[test]
fn twelve_ordered_bindings_match_the_formula_and_uniform_scale() {
    let c = POINTS[4];
    let bindings = bindings(&POINTS);
    assert_eq!(bindings.len(), 12);
    let expected: Vec<_> = (0..4)
        .flat_map(|i| (0..4).filter(move |&j| j != i).map(move |j| (i, j)))
        .collect();
    assert_eq!(
        bindings
            .iter()
            .map(|b| (b.target, b.moved))
            .collect::<Vec<_>>(),
        expected
    );
    for b in &bindings {
        let vi = sub(POINTS[b.target as usize], c);
        let vj = sub(POINTS[b.moved as usize], c);
        for (row, &moved_component) in vj.iter().enumerate() {
            for (col, &target_component) in vi.iter().enumerate() {
                close(
                    b.m[3 * row + col],
                    moved_component * target_component / dot(vi, vi),
                );
            }
        }
        for fraction in [-0.5, 0.25, 1.5] {
            let delta = scale(vi, fraction);
            let moved: Point =
                std::array::from_fn(|r| (0..3).map(|c| b.m[3 * r + c] * delta[c]).sum());
            close_point(
                add(POINTS[b.moved as usize], moved),
                add(c, scale(vj, 1. + fraction)),
            );
        }
    }
    // World offsets cancel from the matrix.
    assert_eq!(
        bindings,
        super::bindings(&POINTS.map(|p| add(p, [0., 0., 40.])))
    );
}

#[test]
fn four_point_clouds_have_only_the_specified_fields() {
    let rois = contributions();
    assert_eq!(
        rois.iter()
            .map(|c| c.contrib_id.as_str())
            .collect::<Vec<_>>(),
        ["free", "scale", "volume", "unshaped"]
    );
    for (i, c) in rois.iter().enumerate() {
        assert!(c.style.is_none());
        let r = c.roi.as_ref().unwrap();
        assert_eq!(r.root, 4);
        assert!(
            r.edges.is_empty()
                && r.facets.is_empty()
                && r.patches.is_empty()
                && r.inclusion.is_none()
        );
        assert!(r.vertices[4].handle.is_none() && !r.vertices[4].enclosing);
        assert_eq!(r.vertices.len(), 5);
        for (j, v) in r.vertices.iter().enumerate() {
            close_point(
                point(v.position.unwrap()),
                add(POINTS[j], [0., 0., 40. * i as f64]),
            );
            if j < 4 {
                assert!(v.enclosing);
                let h = v.handle.as_ref().unwrap();
                assert_eq!(h.frame, [1., 0., 0., 0., 1., 0., 0., 0., 1.]);
                assert_eq!(h.min_extent, (i == 1).then_some(1.));
                if i == 1 || i == 2 {
                    assert_eq!(
                        (h.mode, h.axis_source, h.extent_anchor, h.dof_mask),
                        (0, 1, 4, 0)
                    );
                } else {
                    assert_eq!((h.mode, h.dof_mask), (2, 7));
                }
            }
        }
        assert_eq!(r.auto_surface, if i == 1 { 1 } else { 0 });
        assert_eq!(r.bindings.len(), if i == 1 { 12 } else { 0 });
        // Fields other than these must remain protobuf defaults.
        let mut cleared = r.clone();
        cleared.type_name.clear();
        cleared.vertices.clear();
        cleared.root = 0;
        cleared.bindings.clear();
        cleared.auto_surface = 0;
        assert_eq!(cleared, roi::Roi::default());
    }
}

fn check_volume_case(ratio: f64) {
    let p = POINTS.map(|p| add(p, [0., 0., 80.]));
    for h in 0..4 {
        let p1 = at_height(&p, h, ratio);
        let mut edit = edit(&p, h as i32, p1);
        let answer = conserve_volume(&edit);
        assert_eq!(answer.resolution, roi::Resolution::Ok);
        let after: Vec<_> = answer.positions.iter().copied().map(point).collect();
        assert_eq!(after.len(), 5);
        assert!(after.iter().flatten().all(|x| x.is_finite()));
        close_point(after[h], p1);
        close_point(after[4], p[4]);
        assert!((volume(&after) - volume(&p)).abs() <= 1e-9 * volume(&p));
        let (g, _) = face(&p, h);
        for j in (0..4).filter(|&j| j != h) {
            close_point(after[j], add(g, scale(sub(p[j], g), 1. / ratio.sqrt())));
        }
        edit.seq = 12;
        edit.drag_id = 987;
        edit.contrib_id = "arbitrary or copied".into();
        assert_eq!(conserve_volume(&edit).positions, answer.positions);
    }
}
#[test]
fn pulled_out_corner_preserves_volume() {
    check_volume_case(1.5);
}
#[test]
fn pushed_in_corner_within_limit_preserves_volume() {
    check_volume_case(0.5);
}
#[test]
fn too_close_and_crossing_face_reject_without_positions() {
    for h in 0..4 {
        for ratio in [1. / 32., -0.5] {
            let s = conserve_volume(&edit(&POINTS, h as i32, at_height(&POINTS, h, ratio)));
            assert_eq!(s.resolution, roi::Resolution::Rejected);
            assert!(s.positions.is_empty() && !s.message.is_empty());
        }
    }
}
#[test]
fn threshold_is_strict_and_no_history_or_fixed_rest_shape_is_used() {
    // Axis-aligned face makes the threshold exactly representable.
    let p = [
        [16., 0., 0.],
        [0., 0., 0.],
        [0., 4., 0.],
        [0., 0., 4.],
        [4., 1., 1.],
    ];
    let s = conserve_volume(&edit(&p, 0, [1., 0., 0.]));
    assert_eq!(s.resolution, roi::Resolution::Ok);
    let after: Vec<_> = s.positions.into_iter().map(point).collect();
    close(volume(&after), volume(&p));
}
#[test]
fn invalid_handle_or_nonfinite_input_is_rejected() {
    for h in [-1, 4, 5] {
        let s = conserve_volume(&edit(&POINTS, h, POINTS[0]));
        assert_eq!(s.message, format!("vertex {h} is not a corner"));
        assert!(s.positions.is_empty());
    }
    let s = conserve_volume(&edit(&POINTS, 0, [f64::NAN, 0., 0.]));
    assert_eq!(s.resolution, roi::Resolution::Rejected);
    assert!(s.positions.is_empty());
}
