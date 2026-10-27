//! Convert the artifact calibration fields to micrometers and seconds.
use crate::format::clean;

pub fn convert(value: f64, unit: &str, time: bool) -> f64 {
    let factor = if time {
        match unit.trim() {
            "" | "s" | "sec" => 1.,
            "ms" => 1e-3,
            "min" => 60.,
            "h" | "hr" => 3600.,
            _ => 0.,
        }
    } else {
        match unit.trim() {
            "m" => 1e6,
            "cm" => 1e4,
            "mm" => 1e3,
            "" | "µm" | "μm" | "um" | "micron" => 1.,
            "nm" => 1e-3,
            "Å" | "angstrom" => 1e-4,
            _ => 0.,
        }
    };
    clean(clean(value) * factor)
}
#[derive(Debug, Clone)]
pub struct Resolved {
    pub values: [f64; 4],
    pub sources: [&'static str; 4],
}
impl Resolved {
    /// Diagnose partial calibration; all-known and all-unknown are quiet.
    pub fn source_diagnostic(&self) -> Option<String> {
        if self.sources.iter().all(|source| *source == "fields")
            || self.sources.iter().all(|source| *source == "unknown")
        {
            return None;
        }
        Some(
            ["z", "y", "x", "t"]
                .into_iter()
                .zip(self.sources)
                .map(|(axis, source)| format!("{axis} from {source}"))
                .collect::<Vec<_>>()
                .join(", "),
        )
    }
}
pub fn resolve(fields: [f64; 4], spacing_unit: &str, time_unit: &str) -> Resolved {
    let mut result = Resolved {
        values: [0.; 4],
        sources: ["unknown"; 4],
    };
    for (i, value) in fields.into_iter().enumerate() {
        let field = convert(value, if i == 3 { time_unit } else { spacing_unit }, i == 3);
        if field > 0. {
            result.values[i] = field;
            result.sources[i] = "fields";
        }
    }
    result
}
