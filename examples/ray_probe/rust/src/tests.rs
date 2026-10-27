use crate::geometry::*;
use std::collections::BTreeSet;
// Independent oracle: exhaustively intersect each closed cube with the segment.
fn slab(p: Point, q: Point, c: Cell) -> Option<f64> {
    let (mut entry, mut exit) = (0.0_f64, 1.0_f64);
    for a in 0..3 {
        let d = q[a] - p[a];
        let (lo, hi) = (c[a] as f64 - 0.5, c[a] as f64 + 0.5);
        if d == 0. {
            if p[a] < lo || p[a] > hi {
                return None;
            }
        } else {
            let (t, u) = ((lo - p[a]) / d, (hi - p[a]) / d);
            entry = entry.max(t.min(u));
            exit = exit.min(t.max(u));
        }
    }
    (entry <= exit).then_some(entry)
}
fn check_slab(p: Point, q: Point) {
    let actual = traverse([p, q]);
    let mut expected = BTreeSet::new();
    for z in -4..=4 {
        for y in -4..=4 {
            for x in -4..=4 {
                let c = [z, y, x];
                if slab(p, q, c).is_some() {
                    expected.insert(c);
                }
            }
        }
    }
    assert_eq!(
        actual.iter().copied().collect::<BTreeSet<_>>(),
        expected,
        "{p:?} -> {q:?}"
    );
    assert_eq!(actual.len(), expected.len(), "no duplicates");
    let entries: Vec<_> = actual.iter().map(|&c| slab(p, q, c).unwrap()).collect();
    assert!(
        entries.windows(2).all(|w| w[0] <= w[1] + 1e-14),
        "{entries:?}"
    );
}
#[test]
fn edge_corner_face_and_endpoint_membership_both_directions() {
    for (p, q) in [
        ([0., 0., 0.], [1., 1., 0.]),
        ([0., 0., 0.], [2., 2., 2.]),
        ([0.5, 0.5, 0.5], [-2., -2., -2.]),
        ([-1., -1., -1.], [0.5, 0.5, 0.5]),
        ([0.5, 0.5, -2.], [0.5, 0.5, 2.]),
        ([0.5, -2., 0.], [0.5, 2., 0.]),
        ([0.5, 0.5, 0.5], [0.5, 0.5, 0.5]),
        ([-3., 2., 1.], [2., -3., -2.]),
    ] {
        check_slab(p, q);
        check_slab(q, p);
    }
}
#[test]
fn axis_single_cell_and_oblique() {
    assert_eq!(
        traverse([[0., 0., 0.], [0., 0., 3.]]),
        vec![[0, 0, 0], [0, 0, 1], [0, 0, 2], [0, 0, 3]]
    );
    assert_eq!(
        traverse([[0.1, 0.2, -0.1], [0.4, 0.3, -0.2]]),
        vec![[0, 0, 0]]
    );
    let p = [-2.13, -1.07, 0.03];
    let q = [2.21, 3.13, 2.72];
    check_slab(p, q);
    let sampled: BTreeSet<_> = (0..=100_000)
        .map(|i| {
            std::array::from_fn(|a| {
                (p[a] + (q[a] - p[a]) * i as f64 / 100_000. + 0.5).floor() as i64
            })
        })
        .collect();
    assert_eq!(
        traverse([p, q]).into_iter().collect::<BTreeSet<_>>(),
        sampled
    );
}
#[test]
fn arbitrary_scale_and_crop_order() {
    assert_eq!(level0(2., 3.), 7.);
    assert_eq!(
        crop([[24.5, 1519.5]; 3], [2.; 3], [1000; 3]),
        Some(([12; 3], [759; 3]))
    );
    assert_eq!(
        crop([[2.5, 14.5]; 3], [3.; 3], [10; 3]),
        Some(([1; 3], [4; 3]))
    );
    assert_eq!(
        crop([[-0.5, 2.5]; 3], [2.; 3], [1; 3]),
        Some(([0; 3], [0; 3]))
    ); // the factor freezes at 2
}
#[test]
fn cropped_level_ray_and_column() {
    let bounds = crop([[3.5, 11.5], [-0.5, 7.5], [-0.5, 7.5]], [2.; 3], [8; 3]).unwrap();
    let p = vizi::Position::Ray(vizi::Ray {
        camera: [-1., 1., 1.],
        far: [7., 1., 1.],
        entry_frac: 0.,
        roll: 0.,
    });
    assert_eq!(
        cells(&p, bounds).unwrap(),
        vec![[2, 1, 1], [3, 1, 1], [4, 1, 1], [5, 1, 1]]
    );
    let p = vizi::Position::ColumnZx {
        z: 3.,
        x: 1.,
        y_min: 0.,
        y_max: 3.,
    };
    assert_eq!(
        cells(&p, bounds).unwrap(),
        vec![[3, 0, 1], [3, 1, 1], [3, 2, 1]]
    );
}
#[test]
fn maxima_ties_nan_and_infinity() {
    assert_eq!(argmax(&[f32::NAN, f32::NEG_INFINITY]), Some(1));
    assert_eq!(argmax(&[1., 5., 5., f32::NAN]), Some(1));
    assert_eq!(argmax(&[f32::INFINITY, f32::INFINITY]), Some(0));
    assert_eq!(argmax(&[f32::NAN]), None);
    // C-contiguous block iteration is z, then y, then x.
    let values = [1., 9., 9., 0., 9., 0., 0., 0.];
    assert_eq!(argmax(&values), Some(1)); // (0,0,1) before (0,1,0), (1,0,0)
}
#[test]
fn block_bounds_and_budget() {
    assert_eq!(
        block([2, 3, 4], [2.; 3], ([0; 3], [30; 3])),
        ([4, 6, 8], [6, 8, 10])
    );
    assert_eq!(
        block([1, 2, 3], [4.; 3], ([0; 3], [7, 10, 15])),
        ([4, 8, 12], [7, 10, 15])
    );
    assert!(budget(([0; 3], [512; 3])).is_err());
    assert_eq!(aabb(&[[1, 4, 2], [3, 2, 2]]), Some(([1, 2, 2], [4, 5, 3])));
}
#[test]
fn distance_uses_anisotropic_steps() {
    assert_eq!(
        distances(&[[0, 0, 0], [1, 0, 0], [1, 1, 0]], [2., 3., 1.]),
        vec![0., 2., 5.]
    );
}
#[test]
fn manifest_and_svg_icons() {
    vizi::manifest::Manifest::load("vizi-plugin.json").unwrap();
    for icon in ["probe", "undo", "clear"] {
        let s = std::fs::read_to_string(format!("icons/{icon}.svg")).unwrap();
        assert!(s.starts_with("<svg ") && s.contains("</svg>"));
    }
}

#[test]
fn column_aim_ends_at_last_included_centre() {
    let column = vizi::Position::ColumnYx {
        y: 22.,
        x: 40.,
        z_min: 0.,
        z_max: 32.,
    };
    assert_eq!(
        aim_segment(&column),
        Some([[0., 22., 40.], [31., 22., 40.]])
    );
    let result = cells(&column, ([0; 3], [63; 3])).unwrap();
    assert_eq!(result.len(), 32);
    assert_eq!(result.last(), Some(&[31, 22, 40]));
}
#[test]
fn ellipsoid_compiles_around_exact_fractional_pick() {
    let c = [17.5, 21.5, 41.5];
    let r = crate::roi::ellipsoid(c, 2., 3);
    assert_eq!(r.contrib_id, "pick-3");
    assert_eq!(r.style.unwrap().color, "#ffcc00");
    let r = r.roi.unwrap();
    assert_eq!(r.vertices.len(), 4);
    assert_eq!(r.root, 0);
    assert!(r.t_start.is_none() && r.t_end.is_none() && r.pose_translation.is_empty());
    let controls: Vec<_> = r
        .vertices
        .iter()
        .map(|v| {
            let p = v.position.as_ref().unwrap();
            [p.z, p.y, p.x]
        })
        .collect();
    assert_eq!(
        controls,
        vec![
            c,
            [19.5, 21.5, 41.5],
            [17.5, 23.5, 41.5],
            [17.5, 21.5, 43.5]
        ]
    );
    let compile = r.compile.unwrap();
    assert_eq!(compile.num_display, 18);
    assert_eq!(compile.controls, vec![0, 1, 2, 3]);
    let mut display = vec![[0.; 3]; 18];
    for t in compile.triplets {
        for (a, v) in display[t.display_vertex as usize].iter_mut().enumerate() {
            *v += t.coeff * controls[compile.controls[t.control as usize] as usize][a];
        }
    }
    assert_eq!(r.edges.len(), 12);
    for edge in r.edges {
        assert_eq!(
            edge.role,
            vizi::proto::pois_math::v1::ElementRole::ElementDrawn as i32
        );
        let (p, q, w) = (
            display[edge.source as usize],
            display[edge.target as usize],
            display[edge.control as usize],
        );
        // Rational quadratic midpoint lies on the analytic ellipsoid.
        let midpoint: Point = std::array::from_fn(|a| {
            (0.25 * p[a] + 0.5 * edge.weight * w[a] + 0.25 * q[a]) / (0.5 + 0.5 * edge.weight)
        });
        let norm = (0..3)
            .map(|a| ((midpoint[a] - c[a]) / 2.).powi(2))
            .sum::<f64>();
        assert!((norm - 1.).abs() < 1e-12);
    }
}

#[test]
fn thin_axis_uses_frozen_declared_factor_and_centred_ray_edges() {
    // Shape 3 x 100 x 100 at L=2: Z stopped folding at factor 2.
    let scale = [2., 4., 4.];
    let bounds = crop(
        [[-0.5, 2.5], [-0.5, 99.5], [-0.5, 99.5]],
        scale,
        [1, 25, 25],
    )
    .unwrap();
    assert_eq!(bounds, ([0, 0, 0], [0, 24, 24]));
    assert_eq!(level0(0., scale[0]), 0.5); // covers [0,2), not whole axis [0,3)
    assert_eq!(
        block([0, 2, 3], scale, ([0; 3], [3, 100, 100])),
        ([0, 8, 12], [2, 12, 16])
    );
    let position = vizi::Position::Ray(vizi::Ray {
        camera: [0., 2., -0.5],
        far: [0., 2., 24.5],
        entry_frac: 0.,
        roll: 0.,
    });
    let ray = cells(&position, bounds).unwrap();
    assert_eq!(ray.len(), 25);
    assert_eq!(ray[0], [0, 2, 0]);
    assert_eq!(ray[24], [0, 2, 24]);
    assert_eq!(level0(24.5, 4.), 99.5);
}

#[test]
fn activation_describes_only_assigned_controls_and_includes_mouse_hold() {
    use vizi::proto as pb;
    let mut binding = pb::ToolBinding {
        triggers: vec![
            pb::Trigger {
                key: "P".into(),
                hold: pb::TriggerHold::Toggle as i32,
                ..Default::default()
            },
            pb::Trigger {
                button: 3,
                hold: pb::TriggerHold::Momentary as i32,
                ..Default::default()
            },
        ],
        actions: vec![
            pb::ToolAction {
                action: 1,
                action_id: "undo".into(),
                key: "U".into(),
            },
            pb::ToolAction {
                action: 2,
                action_id: "clear".into(),
                key: String::new(),
            },
        ],
        ..Default::default()
    };
    let text = crate::app::activation_status(&binding);
    assert!(
        text.contains("P toggles")
            && text.contains("middle button holds")
            && text.contains("undo: U")
    );
    assert!(!text.contains("clear:"));
    binding.triggers.remove(0);
    assert!(!crate::app::activation_status(&binding).contains("P toggles"));
}
