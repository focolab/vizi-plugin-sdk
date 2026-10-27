use vizi::proto::{self as pb, pois_math::v1 as roi};

/// Four stored controls; eighteen compiled display positions; twelve conic arcs.
pub fn ellipsoid(peaks: &[[usize; 3]], extents: [f64; 3], all: bool) -> pb::RoiContribution {
    let center = peaks[0].map(|p| p as f64 + 0.5);
    let vertices = (0..4)
        .map(|i| {
            let mut p = center;
            if i > 0 {
                p[i - 1] += extents[i - 1];
            }
            roi::Vertex {
                position: Some(roi::Vec3 {
                    z: p[0],
                    y: p[1],
                    x: p[2],
                    ..Default::default()
                }),
                ..Default::default()
            }
        })
        .collect();
    let mut rows = Vec::new();
    for axis in 0..3 {
        for sign in [1.0, -1.0] {
            let mut row = [0.0; 3];
            row[axis] = sign;
            rows.push(row);
        }
    }
    let mut edges = Vec::new();
    for (a, b) in [(0, 1), (0, 2), (1, 2)] {
        for (sa, sb) in [(1.0, 1.0), (-1.0, 1.0), (-1.0, -1.0), (1.0, -1.0)] {
            let mut row = [0.0; 3];
            row[a] = sa;
            row[b] = sb;
            edges.push(roi::Edge {
                source: (2 * a + usize::from(sa < 0.0)) as i32,
                target: (2 * b + usize::from(sb < 0.0)) as i32,
                control: rows.len() as i32,
                weight: std::f64::consts::FRAC_1_SQRT_2,
                ..Default::default()
            });
            rows.push(row);
        }
    }
    let mut triplets = Vec::new();
    for (i, &[a, b, c]) in rows.iter().enumerate() {
        for (column, coeff) in [1.0 - a - b - c, a, b, c].into_iter().enumerate() {
            if coeff != 0.0 {
                triplets.push(roi::CompileTriplet {
                    display_vertex: i as i32,
                    control: column as i32,
                    coeff,
                    ..Default::default()
                });
            }
        }
    }
    let mut geometry = roi::Roi {
        type_name: "ellipsoid".into(),
        vertices,
        edges,
        compile: Some(roi::CompileOperator {
            triplets,
            num_display: 18,
            controls: vec![0, 1, 2, 3],
            ..Default::default()
        }),
        ..Default::default()
    };
    if all {
        geometry.t_start = Some(0);
        geometry.t_end = Some((peaks.len() - 1) as i32);
        geometry.out_of_span = roi::OutOfSpan::Hold as i32;
        geometry.pose_translation = peaks
            .iter()
            .flat_map(|p| (0..3).map(move |axis| p[axis] as f64 - peaks[0][axis] as f64))
            .collect();
    }
    pb::RoiContribution {
        contrib_id: "peak".into(),
        name: "Peak".into(),
        roi: Some(geometry),
        style: Some(pb::RoiStyle {
            color: "#FFCC33".into(),
            ..Default::default()
        }),
    }
}
