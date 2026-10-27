//! SPEC.md §6, including its non-finite policy.

pub fn threshold(
    volume: &[f32],
    shape: [usize; 3],
    selected: &[bool],
    scale: f64,
) -> Result<f64, &'static str> {
    let columns = shape[1]
        .checked_mul(shape[2])
        .ok_or("Invalid volume shape")?;
    if shape.contains(&0)
        || shape[0].checked_mul(columns) != Some(volume.len())
        || selected.len() != columns
    {
        return Err("Invalid volume shape");
    }
    if !scale.is_finite() || !(0.5..=2.0).contains(&scale) {
        return Err("Threshold scale must be in [0.5, 2.0]");
    }
    if !selected.iter().any(|&v| v) {
        return Err("Drag a closed loop around a region.");
    }
    let mut maxima = Vec::new();
    for (column, &include) in selected.iter().enumerate() {
        if !include {
            continue;
        }
        let mut maximum = f64::NEG_INFINITY;
        for z in 0..shape[0] {
            let voxel = f64::from(volume[z * columns + column]);
            if !voxel.is_nan() && voxel > maximum {
                maximum = voxel;
            }
        }
        if maximum != f64::NEG_INFINITY {
            maxima.push(maximum);
        }
    }
    if maxima.is_empty() {
        return Err("Nothing measurable under the loop.");
    }
    maxima.sort_unstable_by(f64::total_cmp);
    let middle = maxima.len() / 2;
    let median = if maxima.len() % 2 == 0 {
        (maxima[middle - 1] + maxima[middle]) / 2.0
    } else {
        maxima[middle]
    };
    Ok(scale * median)
}

#[cfg(test)]
mod tests {
    use super::threshold;

    #[test]
    fn column_maxima_even_median_and_scale() {
        let w = [1., 2., 3., 4., 5., 6., 7., 8., 0., 0., 0., 0.];
        assert_eq!(threshold(&w, [3, 1, 4], &[true; 4], 1.0), Ok(6.5));
        assert_eq!(threshold(&w, [3, 1, 4], &[true; 4], 2.0), Ok(13.0));
        assert_eq!(
            threshold(&w, [3, 1, 4], &[true, true, true, false], 1.0),
            Ok(6.0)
        );
    }

    #[test]
    fn negative_infinity_and_nan_columns_are_dropped() {
        let w = [f32::NAN, f32::NEG_INFINITY, 4., f32::INFINITY];
        assert_eq!(threshold(&w, [1, 1, 4], &[true; 4], 1.0), Ok(f64::INFINITY));
        let w = [f32::NAN, 2., 6., f32::NEG_INFINITY, 5., 4.];
        assert_eq!(threshold(&w, [2, 1, 3], &[true; 3], 1.0), Ok(5.5));
    }

    #[test]
    fn all_negative_infinity_refuses_instead_of_becoming_foreground() {
        for v in [f32::NEG_INFINITY, f32::NAN] {
            assert_eq!(
                threshold(&[v; 2], [1, 1, 2], &[true; 2], 1.0),
                Err("Nothing measurable under the loop.")
            );
        }
    }

    #[test]
    fn opposite_infinities_leave_positive_infinity_foreground() {
        let w = [f32::NEG_INFINITY, f32::INFINITY];
        let t = threshold(&w, [1, 1, 2], &[true; 2], 1.0).unwrap();
        assert_eq!(t, f64::INFINITY);
        assert_eq!(w.map(|v| f64::from(v) >= t), [false, true]);
        assert_eq!(
            [
                f64::NAN,
                f64::NEG_INFINITY,
                f64::from(f32::MAX),
                f64::INFINITY
            ]
            .map(|v| v >= t),
            [false, false, false, true]
        );
    }

    #[test]
    fn extreme_finite_f32_values_stay_finite_in_f64() {
        for v in [f32::MIN, f32::MAX] {
            assert_eq!(
                threshold(&[v; 2], [1, 1, 2], &[true; 2], 2.0),
                Ok(2.0 * f64::from(v))
            );
        }
    }

    #[test]
    fn input_shape_selection_and_scale_validation() {
        assert_eq!(
            threshold(&[1.], [1, 1, 1], &[false], 1.0),
            Err("Drag a closed loop around a region.")
        );
        assert!(threshold(&[1.], [1, 1, 2], &[true; 2], 1.0).is_err());
        for scale in [f64::NAN, f64::INFINITY, 0., -1., 2.1] {
            assert!(threshold(&[1.], [1, 1, 1], &[true], scale).is_err());
        }
    }
}
