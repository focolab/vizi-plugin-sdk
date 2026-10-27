use vizi::{
    plugin::{Chunk, LabelTable, Volume},
    proto as pb,
};

pub const NAMES: [&str; 3] = ["dim", "mid", "bright"];
pub fn label(value: f64, threshold: f64) -> u8 {
    if value.is_nan() || value < threshold {
        0
    } else {
        (1.0 + ((value - threshold) / 200.0).floor()).min(3.0) as u8
    }
}

/// ZYX row-major crop; internal zero labels are retained.
pub fn tight(labels: &[u8], shape: [u32; 3]) -> Option<Chunk> {
    let [depth, height, width] = shape.map(|n| n as usize);
    assert_eq!(labels.len(), depth * height * width);
    let mut lo = shape;
    let mut hi = [0; 3];
    for (i, &value) in labels.iter().enumerate() {
        if value == 0 {
            continue;
        }
        let position = [
            (i / (height * width)) as u32,
            ((i / width) % height) as u32,
            (i % width) as u32,
        ];
        for axis in 0..3 {
            lo[axis] = lo[axis].min(position[axis]);
            hi[axis] = hi[axis].max(position[axis] + 1);
        }
    }
    if hi[0] == 0 {
        return None;
    }
    let cropped_shape = std::array::from_fn(|a| hi[a] - lo[a]);
    let mut bytes = Vec::new();
    for z in lo[0]..hi[0] {
        for y in lo[1]..hi[1] {
            let row = (z as usize * height + y as usize) * width;
            bytes.extend_from_slice(&labels[row + lo[2] as usize..row + hi[2] as usize]);
        }
    }
    Some(Chunk {
        labels: Volume {
            bytes,
            shape_zyx: cropped_shape,
            dtype: pb::DType::DtypeU8,
        },
        position_zyx: lo,
    })
}

pub fn union_into(mask: &mut [u8], values: &[f32], threshold: f64) {
    assert_eq!(mask.len(), values.len());
    for (target, &value) in mask.iter_mut().zip(values) {
        *target |= u8::from(f64::from(value) >= threshold);
    }
}

pub fn bands_table(visible: [bool; 3]) -> LabelTable {
    let mut colormap = [[0; 4]; 256];
    colormap[1] = [60, 120, 255, 255];
    colormap[2] = [255, 200, 0, 255];
    colormap[3] = [255, 60, 60, 255];
    for (row, shown) in colormap[1..4].iter_mut().zip(visible) {
        if !shown {
            row[3] = 0;
        }
    }
    LabelTable {
        colormap,
        label_names: NAMES
            .iter()
            .enumerate()
            .map(|(i, name)| (i as u32 + 1, (*name).into()))
            .collect(),
        label_visible: visible
            .into_iter()
            .enumerate()
            .map(|(i, v)| (i as u32 + 1, v))
            .collect(),
        style: pb::OverlayStyle {
            opacity: 0.6,
            draw_priority: 1,
            ..Default::default()
        },
    }
}
pub fn footprint_table() -> LabelTable {
    let mut colormap = [[0; 4]; 256];
    colormap[1] = [200, 200, 200, 255];
    LabelTable {
        colormap,
        label_names: [(1, "ever".into())].into(),
        label_visible: Default::default(),
        style: pb::OverlayStyle {
            opacity: 0.3,
            ..Default::default()
        },
    }
}

pub fn threshold_text(value: f64) -> String {
    // Display uses a shortest round-tripping decimal and no trailing .0.
    if value == 0.0 {
        "0".into()
    } else {
        value.to_string()
    }
}
