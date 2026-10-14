use crate::format::{error, values, Calibration, Crate, Writer};
use std::{
    fs::{self, File, OpenOptions},
    io::Read,
    path::Path,
};
use vizi::{control::Result, proto::error::Kind};

/// Compare file bytes in bounded storage, after validating/decoding the crates.
fn identical(a: &Path, b: &Path) -> Result<bool> {
    let read = || -> std::io::Result<bool> {
        let (mut a, mut b) = (File::open(a)?, File::open(b)?);
        if a.metadata()?.len() != b.metadata()?.len() {
            return Ok(false);
        }
        let (mut x, mut y) = ([0; 65536], [0; 65536]);
        loop {
            let n = a.read(&mut x)?;
            if n == 0 {
                return Ok(true);
            }
            b.read_exact(&mut y[..n])?;
            if x[..n] != y[..n] {
                return Ok(false);
            }
        }
    };
    read().map_err(|e| error(Kind::IoError, e.to_string()))
}
pub fn compare(a: &Path, b: &Path, cal: Option<&Calibration>) -> Result<String> {
    let (mut x, mut y) = (Crate::open(a)?, Crate::open(b)?);
    // Decode even byte-identical inputs: unfinished/missing-calibration is not a verdict.
    x.require_calibration(cal)?;
    y.require_calibration(cal)?;
    if x.header.dtype != y.header.dtype || x.header.shape != y.header.shape {
        return Ok("different: dtype or shape differs".into());
    }
    let mut notes = Vec::new();
    for file in [&x, &y] {
        if file.header.flags & crate::format::DERIVED != 0 {
            notes.push(format!("{}: DERIVED", file.name));
        }
        if !file.provenance.is_empty() {
            notes.push(format!("{}: provenance record present", file.name));
        }
    }
    let mut differing = 0;
    for t in 0..x.header.shape[0] {
        for c in 0..x.header.shape[1] {
            for file in [&mut x, &mut y] {
                if !file.raw(t, c)?.1 {
                    notes.push(format!(
                        "{}: CRC-32 failed at (t={t}, c={c}); compared bytes anyway",
                        file.name
                    ));
                }
            }
            let xv = x.volume(t, c, false, cal)?;
            let yv = y.volume(t, c, false, cal)?;
            // Float voxel equality is numerical, as in the reference comparator.
            let equal = if x.header.dtype == 3 {
                values(3, &xv)? == values(3, &yv)?
            } else {
                xv == yv
            };
            if !equal {
                differing += 1;
            }
        }
    }
    let tier = if identical(a, b)? {
        "byte-equal"
    } else if differing != 0 {
        notes.push(format!("{differing} volumes differ"));
        "different"
    } else {
        let doc = x.form == y.form && x.text == y.text;
        let spacing = x.header.calibration == y.header.calibration;
        if !doc {
            notes.push("metadata form or text differs".into());
        }
        if !spacing {
            notes.push("header spacing or time increment differs".into());
        }
        let names = (0..x.header.shape[1] as usize).all(|i| {
            x.names.get(i).map_or("", String::as_str) == y.names.get(i).map_or("", String::as_str)
        });
        if !names {
            notes.push("channel names differ".into());
        }
        if doc && spacing && names {
            "semantically equal"
        } else {
            "voxel-equal"
        }
    };
    Ok(if notes.is_empty() {
        tier.into()
    } else {
        format!("{tier}: {}", notes.join("; "))
    })
}
pub fn check(path: &Path) -> Result<(bool, String)> {
    let mut file = Crate::open(path)?;
    let failed = file.check()?;
    if let Some((t, c)) = failed.first() {
        Ok((
            false,
            format!(
                "{}: {} of {} blocks failed CRC; first: (t={t}, c={c})",
                path.display(),
                failed.len(),
                file.header.blocks()
            ),
        ))
    } else {
        Ok((
            true,
            format!(
                "{}: validated {} blocks, index and footer",
                path.display(),
                file.header.blocks()
            ),
        ))
    }
}
pub fn recreate(
    source: &Path,
    target: &Path,
    order: &str,
    cal: Option<&Calibration>,
) -> Result<()> {
    if !["ascending", "reverse", "scrambled"].contains(&order) {
        return Err(error(Kind::InvalidArgument, "unknown block order"));
    }
    let mut input = Crate::open(source)?;
    input.require_calibration(cal)?;
    // Avoid truncating an input, including through aliases, by refusing existing destinations.
    let output = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(target)
        .map_err(|e| error(Kind::IoError, format!("{}: {e}", target.display())))?;
    let outcome = (|| {
        let mut writer = Writer::begin_sections(
            output,
            input.header.clone(),
            &input.form,
            &input.text,
            &input.provenance,
            &input.names,
        )?;
        let mut slots: Vec<_> = (0..input.header.shape[0])
            .flat_map(|t| (0..input.header.shape[1]).map(move |c| (t, c)))
            .collect();
        if order == "reverse" {
            slots.reverse();
        } else if order == "scrambled" {
            let mut state = 0x5eed_u64;
            for i in (1..slots.len()).rev() {
                state ^= state << 13;
                state ^= state >> 7;
                state ^= state << 17;
                slots.swap(i, (state % (i as u64 + 1)) as usize);
            }
        }
        for (t, c) in slots {
            writer.block(t, c, &input.volume(t, c, true, cal)?, true)?;
        }
        writer.finish()?;
        Ok(())
    })();
    if outcome.is_err() {
        fs::remove_file(target).map_err(|e| {
            error(
                Kind::IoError,
                format!("{}: cleanup failed: {e}", target.display()),
            )
        })?;
    }
    outcome
}
