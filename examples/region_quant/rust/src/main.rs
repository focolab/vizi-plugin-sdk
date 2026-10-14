mod app;
mod declaration;
mod table;
mod trace;
use std::sync::{Arc, Mutex};
use vizi::declare::ManagementCommand;

#[tokio::main]
async fn main() -> std::process::ExitCode {
    vizi::declare::report_exit(run().await)
}

async fn run() -> Result<(), Box<dyn std::error::Error>> {
    let traces = Arc::new(trace::Traces::default());
    let Some(plugin) = ManagementCommand::prepare(
        declaration::ID,
        env!("CARGO_PKG_VERSION"),
        std::env::args_os().skip(1),
        || app::declare(Arc::new(Mutex::new(app::Model::default())), traces.clone()),
    )?
    else {
        return Ok(());
    };
    // The host transfers exclusive ownership of the inherited doorbell fd.
    let runtime = unsafe { plugin.into_runtime_from_env() }?;
    traces.start(runtime.context())?;
    let result = runtime.serve_env().await;
    // Also clean up on host loss or a session fault, when no hook is promised.
    traces.stop_and_join()?;
    result?;
    Ok(())
}

#[cfg(test)]
mod declaration_tests {
    use super::*;
    #[test]
    fn manifest_matches_development_snapshot() {
        let plugin = app::declare(
            Arc::new(Mutex::new(app::Model::default())),
            Arc::new(trace::Traces::default()),
        )
        .unwrap();
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
