mod analysis;
mod app;
mod declaration;
mod overlay;
mod plot;
#[cfg(test)]
mod tests;
mod threshold;
use std::sync::atomic::Ordering;
use vizi::declare::ManagementCommand;

#[tokio::main]
async fn main() -> std::process::ExitCode {
    vizi::declare::report_exit(run().await)
}

async fn run() -> Result<(), Box<dyn std::error::Error>> {
    let (app, worker) = app::new();
    let Some(plugin) = ManagementCommand::prepare(
        declaration::ID,
        env!("CARGO_PKG_VERSION"),
        std::env::args_os().skip(1),
        || app::declare(&app),
    )?
    else {
        return Ok(());
    };
    // The host transfers exclusive ownership of the inherited doorbell fd.
    let runtime = unsafe { plugin.into_runtime_from_env() }?;
    let context = runtime.context();
    let worker_app = app.clone();
    let worker = std::thread::Builder::new()
        .name("segmentation".into())
        .spawn(move || worker.run(worker_app, context))?;
    let result = runtime.serve_env().await;
    app.stop.store(true, Ordering::Relaxed);
    let _ = worker.join();
    result?;
    Ok(())
}

#[cfg(test)]
mod declaration_tests {
    use super::*;
    #[test]
    fn manifest_matches_development_snapshot() {
        let plugin = app::declare(&app::new().0).unwrap();
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
