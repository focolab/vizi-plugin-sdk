//! Geometry and closed-form shaping from tetra_shape/SPEC.md §§2–3.
use vizi::{proto as pb, proto::pois_math::v1 as roi, shape::Shape};

type Point = [f64; 3];
const POINTS: [Point; 5] = [
    [10., 10., 10.],
    [10., 30., 12.],
    [28., 18., 14.],
    [16., 20., 32.],
    [16., 19.5, 17.],
];
fn add(a: Point, b: Point) -> Point {
    std::array::from_fn(|i| a[i] + b[i])
}
fn sub(a: Point, b: Point) -> Point {
    std::array::from_fn(|i| a[i] - b[i])
}
fn scale(a: Point, k: f64) -> Point {
    a.map(|x| x * k)
}
fn dot(a: Point, b: Point) -> f64 {
    a.into_iter().zip(b).map(|(a, b)| a * b).sum()
}
fn cross(a: Point, b: Point) -> Point {
    [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]
}
fn norm(a: Point) -> f64 {
    dot(a, a).sqrt()
}
fn wire([z, y, x]: Point) -> roi::Vec3 {
    roi::Vec3 { z, y, x }
}
fn point(v: roi::Vec3) -> Point {
    [v.z, v.y, v.x]
}

fn bindings(p: &[Point; 5]) -> Vec<roi::LinearBinding> {
    let mut result = Vec::with_capacity(12);
    for i in 0..4 {
        let ray = sub(p[i], p[4]);
        let radius = norm(ray);
        let unit = scale(ray, 1. / radius);
        for j in 0..4 {
            if i == j {
                continue;
            }
            let moved = sub(p[j], p[4]);
            let m = (0..9)
                .map(|n| moved[n / 3] * unit[n % 3] / radius)
                .collect();
            result.push(roi::LinearBinding {
                target: i as i32,
                moved: j as i32,
                m,
            });
        }
    }
    result
}

pub fn contributions() -> Vec<pb::RoiContribution> {
    [
        ("free", "Free corners", 0.),
        ("scale", "Scale", 40.),
        ("volume", "Keeps its volume", 80.),
        ("unshaped", "No shaper", 120.),
    ]
    .into_iter()
    .map(|(id, name, x)| {
        let points = POINTS.map(|p| add(p, [0., 0., x]));
        let radial = matches!(id, "scale" | "volume");
        let vertices = points
            .into_iter()
            .enumerate()
            .map(|(i, p)| roi::Vertex {
                position: Some(wire(p)),
                enclosing: i < 4,
                handle: (i < 4).then(|| roi::HandleSpec {
                    frame: vec![1., 0., 0., 0., 1., 0., 0., 0., 1.],
                    mode: if radial {
                        roi::DofMode::Primary
                    } else {
                        roi::DofMode::All
                    } as i32,
                    dof_mask: if radial { 0 } else { 7 },
                    axis_source: if radial {
                        roi::AxisSource::AxisRadial
                    } else {
                        roi::AxisSource::AxisFrame
                    } as i32,
                    extent_anchor: if radial { 4 } else { 0 },
                    min_extent: (id == "scale").then_some(1.),
                    ..Default::default()
                }),
                ..Default::default()
            })
            .collect();
        pb::RoiContribution {
            contrib_id: id.into(),
            name: name.into(),
            style: None,
            roi: Some(roi::Roi {
                type_name: format!("tetra.{id}"),
                vertices,
                root: 4,
                auto_surface: if id == "scale" {
                    roi::AutoSurface::Star
                } else {
                    roi::AutoSurface::Hull
                } as i32,
                bindings: if id == "scale" {
                    bindings(&points)
                } else {
                    vec![]
                },
                ..Default::default()
            }),
        }
    })
    .collect()
}

/// Each edit is independent, including commit-only edits and copied regions.
pub fn conserve_volume(edit: &pb::ShapeEdit) -> Shape {
    let Some(movement) = &edit.r#move else {
        return Shape::rejected("missing move");
    };
    let h = movement.handle;
    if !(0..4).contains(&h) {
        return Shape::rejected(format!("vertex {h} is not a corner"));
    }
    let h = h as usize;
    let Some(roi) = &movement.roi else {
        return Shape::rejected("missing rest shape");
    };
    let Some(p1) = movement.p1.map(point) else {
        return Shape::rejected("missing projected point");
    };
    let Some(p) = roi
        .vertices
        .iter()
        .map(|v| v.position.map(point))
        .collect::<Option<Vec<_>>>()
    else {
        return Shape::rejected("missing vertex position");
    };
    if p.len() != 5
        || !p
            .iter()
            .chain(std::iter::once(&p1))
            .flatten()
            .all(|x| x.is_finite())
    {
        return Shape::rejected("expected five finite rest positions and a finite projected point");
    }
    let face: Vec<_> = (0..4).filter(|&j| j != h).collect();
    let (a, b, d) = (p[face[0]], p[face[1]], p[face[2]]);
    let g = scale(add(add(a, b), d), 1. / 3.);
    let normal = cross(sub(b, a), sub(d, a));
    let length = norm(normal);
    if length == 0. || !length.is_finite() {
        return Shape::rejected("opposite face is degenerate");
    }
    let n = scale(normal, 1. / length);
    let s0 = dot(n, sub(p[h], g));
    let s1 = dot(n, sub(p1, g));
    if !s0.is_finite() || !s1.is_finite() || s0 * s1 <= 0. || s1.abs() < s0.abs() / 16. {
        return Shape::rejected("the corner would come too close to the opposite face");
    }
    let k = (s0.abs() / s1.abs()).sqrt();
    let mut result = p;
    result[h] = p1;
    for j in face {
        result[j] = add(g, scale(sub(result[j], g), k));
    }
    Shape::ok(result.into_iter().map(wire).collect())
}

#[cfg(test)]
mod tests;
