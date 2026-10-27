//! Run with --install to register this executable with Vizi, or --manifest DIR
//! to write the generated interface for inspection or a custom installation.
use std::sync::{Arc, Mutex};
use vizi_doorbell::{
    declare::{CommandSpec, ManagementCommand, PanelSpec, PluginBuilder, Widget},
    proto as pb,
};

#[tokio::main]
async fn main() -> std::process::ExitCode {
    vizi_doorbell::declare::report_exit(run().await)
}

async fn run() -> std::result::Result<(), Box<dyn std::error::Error>> {
    let Some(plugin) = ManagementCommand::prepare(
        "org.vizi.rust_example",
        "0.1.0",
        std::env::args_os().skip(1),
        || {
            let mut plugin =
                PluginBuilder::new("org.vizi.rust_example", "Rust SDK example", "0.1.0");
            let state = Arc::new(Mutex::new((false, String::from("Ready"))));
            let command_state = state.clone();
            let hello = plugin.command(
                CommandSpec::new("hello", "Say hello from Rust"),
                move |ctx, _| {
                    let mut state = command_state.lock().unwrap();
                    state.1 = "Hello from Rust".into();
                    if state.0 {
                        ctx.panel("status").set_value("message", state.1.clone())?;
                    }
                    Ok(pb::CommandDone {
                        ok: true,
                        message: "Rust plugin is running".into(),
                        ..Default::default()
                    })
                },
            );
            plugin.panel(PanelSpec::new(
                "status",
                "Rust status",
                Widget::column(
                    "root",
                    [
                        Widget::label("message", "Ready"),
                        Widget::button("hello-button", "Say hello", &hello),
                    ],
                ),
            ));
            plugin.on_panel_opened(move |ctx, panel| {
                if panel.panel_id == "status" {
                    let mut state = state.lock().unwrap();
                    state.0 = true;
                    ctx.panel("status").set_value("message", state.1.clone())?;
                }
                Ok(())
            });
            plugin.on_shutdown(|_, reason| {
                eprintln!("Rust plugin shutdown: {}", reason.reason);
                Ok(())
            });
            plugin.build()
        },
    )?
    else {
        return Ok(());
    };
    // Vizi transfers exclusive ownership of the inherited launch fd.
    let runtime = unsafe { plugin.into_runtime_from_env() }?;
    runtime.serve_env().await?;
    Ok(())
}
