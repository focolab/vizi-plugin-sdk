//! C-order, little-endian volumes and the SPEC.md separable Gaussian.
use vizi::proto::DType;

pub type Result<T> = std::result::Result<T, String>;
// Bound a user-supplied kernel before allocating it (the spec sets no limit).
const MAX_RADIUS: f64 = 100_000.0;

pub fn validate_extents(extents: [f64; 3]) -> Result<()> {
    if extents.iter().any(|e| !e.is_finite() || *e <= 0.0) {
        return Err("Extents must be finite, strictly positive numbers".into());
    }
    validate_kernel_extents(extents)
}

// The Gaussian definition permits zero sigma even though an ROI semi-axis
// entered through the panel must be strictly positive.
fn validate_kernel_extents(extents: [f64; 3]) -> Result<()> {
    if extents.iter().any(|e| !e.is_finite() || *e < 0.0) {
        return Err("Blur extents must be finite, non-negative numbers".into());
    }
    if extents.iter().any(|e| (1.5 * e).ceil() > MAX_RADIUS) {
        return Err("Extent too large: Gaussian radius exceeds 100000 voxels".into());
    }
    Ok(())
}

pub fn decode(bytes: &[u8], dtype: i32, shape: &[u32]) -> Result<(Vec<f64>, [usize; 3])> {
    let shape: [u32; 3] = shape
        .try_into()
        .map_err(|_| "Volume must have three axes")?;
    let shape = shape.map(|n| n as usize);
    let count = shape
        .iter()
        .try_fold(1usize, |n, &d| n.checked_mul(d))
        .filter(|&n| n > 0)
        .ok_or("Empty or oversized volume")?;
    let dtype = DType::try_from(dtype).map_err(|_| "Unknown volume dtype")?;
    let width = match dtype {
        DType::DtypeU8 => 1,
        DType::DtypeU16 => 2,
        DType::DtypeF32 => 4,
        DType::DtypeF64 => 8,
        _ => return Err("Unsupported volume dtype".into()),
    };
    if count.checked_mul(width) != Some(bytes.len()) {
        return Err("Volume byte count does not match shape and dtype".into());
    }
    let mut values = Vec::new();
    values.try_reserve_exact(count).map_err(|e| e.to_string())?;
    for b in bytes.chunks_exact(width) {
        let value = match dtype {
            DType::DtypeU8 => b[0] as f64,
            DType::DtypeU16 => u16::from_le_bytes(b.try_into().unwrap()) as f64,
            DType::DtypeF32 => f32::from_le_bytes(b.try_into().unwrap()) as f64,
            DType::DtypeF64 => f64::from_le_bytes(b.try_into().unwrap()),
            _ => unreachable!(),
        };
        values.push(value);
    }
    Ok((values, shape))
}

pub fn blur(
    mut values: Vec<f64>,
    shape: [usize; 3],
    extents: [f64; 3],
    cancelled: impl Fn() -> bool,
) -> Result<Vec<f64>> {
    validate_kernel_extents(extents)?;
    if shape.contains(&0)
        || shape.iter().try_fold(1usize, |n, &d| n.checked_mul(d)) != Some(values.len())
    {
        return Err("Volume shape does not match intensities".into());
    }
    let minimum = values
        .iter()
        .copied()
        .filter(|value| value.is_finite())
        .reduce(f64::min)
        .ok_or("Volume contains no finite voxel")?;
    for value in &mut values {
        if value.is_nan() || *value == f64::NEG_INFINITY {
            *value = minimum;
        }
    }
    let mut scratch = Vec::new();
    scratch
        .try_reserve_exact(values.len())
        .map_err(|e| e.to_string())?;
    scratch.resize(values.len(), 0.0);
    let strides = [shape[1] * shape[2], shape[2], 1];
    for axis in 0..3 {
        let sigma = extents[axis] / 2.0;
        if sigma == 0.0 {
            continue;
        }
        let radius = (3.0 * sigma).ceil() as isize;
        let mut kernel: Vec<f64> = (-radius..=radius)
            .map(|i| {
                // This form also handles tiny positive sigma without 0/0 at i=0.
                let q = i as f64 / sigma;
                (-0.5 * q * q).exp()
            })
            .collect();
        let sum: f64 = kernel.iter().sum();
        for k in &mut kernel {
            *k /= sum;
        }
        let stride = strides[axis];
        for (i, output) in scratch.iter_mut().enumerate() {
            if i % 256 == 0 && cancelled() {
                return Err("Analysis canceled".into());
            }
            let coordinate = (i / stride) % shape[axis];
            let base = i - coordinate * stride;
            *output = kernel
                .iter()
                .enumerate()
                // Tiny positive sigmas can underflow off-center taps to zero.
                // Skip them so an allowed +Inf does not produce 0 * Inf = NaN.
                .filter(|(_, weight)| **weight != 0.0)
                .map(|(j, &weight)| {
                    let source = (coordinate as isize + j as isize - radius)
                        .clamp(0, shape[axis] as isize - 1)
                        as usize;
                    values[base + source * stride] * weight
                })
                .sum();
        }
        std::mem::swap(&mut values, &mut scratch);
    }
    if cancelled() {
        return Err("Analysis canceled".into());
    }
    Ok(values)
}

pub fn peak(values: &[f64], shape: [usize; 3]) -> [usize; 3] {
    let mut best = 0;
    for i in 1..values.len() {
        if values[i] > values[best] {
            best = i;
        }
    }
    [
        best / (shape[1] * shape[2]),
        best / shape[2] % shape[1],
        best % shape[2],
    ]
}
