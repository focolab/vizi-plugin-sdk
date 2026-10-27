use format_bridge::{app::App, declaration, format::Calibration, offline};
use std::path::Path;
use vizi::declare::ManagementCommand;

fn cli(args: &[String]) -> i32 {
    let usage = || {
        eprintln!("usage: format-bridge compare A B [--calibration TABLE]\n       format-bridge recreate IN OUT [--order ascending|reverse|scrambled] [--calibration TABLE]\n       format-bridge check FILE\n       format-bridge                 (serve the plugin)");
        1
    };
    let mut paths = Vec::new();
    let mut calibration = None;
    let mut order = "ascending";
    let mut it = args[1..].iter();
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--calibration" if args[0] != "check" => {
                let Some(p) = it.next() else {
                    return usage();
                };
                calibration = Some(p);
            }
            "--order" if args[0] == "recreate" => {
                let Some(p) = it.next() else {
                    return usage();
                };
                if !["ascending", "reverse", "scrambled"].contains(&p.as_str()) {
                    return usage();
                }
                order = p;
            }
            s if s.starts_with('-') => return usage(),
            _ => paths.push(arg),
        }
    }
    if !matches!(
        (args[0].as_str(), paths.len()),
        ("compare" | "recreate", 2) | ("check", 1)
    ) {
        return usage();
    }
    let result: vizi::control::Result<i32> = (|| {
        let cal = calibration
            .map(|s| Calibration::load(Path::new(s)))
            .transpose()?;
        match args[0].as_str() {
            "compare" => {
                println!(
                    "{}",
                    offline::compare(Path::new(paths[0]), Path::new(paths[1]), cal.as_ref())?
                );
                Ok(0)
            }
            "recreate" => {
                offline::recreate(
                    Path::new(paths[0]),
                    Path::new(paths[1]),
                    order,
                    cal.as_ref(),
                )?;
                println!("wrote {} ({order} block order)", paths[1]);
                Ok(0)
            }
            _ => {
                let (ok, message) = offline::check(Path::new(paths[0]))?;
                println!("{message}");
                Ok(if ok { 0 } else { 3 })
            }
        }
    })();
    match result {
        Ok(code) => code,
        Err(e) => {
            eprintln!("{e}");
            2
        }
    }
}

#[tokio::main]
async fn main() -> std::process::ExitCode {
    vizi::declare::report_exit(run().await)
}

async fn run() -> Result<(), Box<dyn std::error::Error>> {
    // File tools have their own arguments; management paths can be non-UTF-8.
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    if args
        .first()
        .is_some_and(|arg| arg == "check" || arg == "compare" || arg == "recreate")
    {
        let args = args
            .into_iter()
            .map(|arg| {
                arg.into_string()
                    .map_err(|_| "file tool arguments must be UTF-8")
            })
            .collect::<Result<Vec<_>, _>>()?;
        std::process::exit(cli(&args));
    }
    let app = App::new();
    let Some(plugin) = ManagementCommand::prepare_with_resources(
        declaration::ID,
        env!("CARGO_PKG_VERSION"),
        args,
        || app.declare(),
        declaration::package_calibration,
    )?
    else {
        return Ok(());
    };
    // Adopt the inherited doorbell exactly once. The host launches this binary directly.
    let runtime = unsafe { plugin.into_runtime_from_env() }?;
    let result = runtime.serve_env().await;
    app.join();
    result?;
    Ok(())
}

#[cfg(test)]
mod tests {
    #[test]
    fn crc_ieee_check_value() {
        assert_eq!(crc32fast::hash(b"123456789"), 0xCBF43926);
    }
}

#[cfg(test)]
mod declaration_tests {
    use super::*;
    #[test]
    fn manifest_matches_development_snapshot() {
        let plugin = App::new().declare().unwrap();
        let checked = vizi::manifest::Manifest::load(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/vizi-plugin.json"
        ))
        .unwrap();
        let mut expected = checked.value().clone();
        let mut actual = plugin.manifest().value().clone();
        expected.as_object_mut().unwrap().remove("entry");
        actual.as_object_mut().unwrap().remove("entry");
        assert_eq!(actual, expected);
    }
}
