mod shapes;
use vizi::{
    control,
    declare::{CommandSpec, DeclaredPlugin, ManagementCommand, Permission, PluginBuilder},
    proto as pb,
};

fn declare() -> Result<DeclaredPlugin, String> {
    let mut b = PluginBuilder::new(
        "org.example.tetra_shape",
        "Tetra Shape",
        env!("CARGO_PKG_VERSION"),
    );
    b.permission(Permission::ContributeRois);
    b.command(
        CommandSpec::new("tetra.contribute", "Contribute the tetrahedra").hidden(),
        |ctx, _| {
            let reply = ctx.request(pb::plugin_msg::Msg::ContributeRois(pb::ContributeRois {
                rois: shapes::contributions(),
                replace_all: true,
            }))?;
            if !matches!(reply.message, pb::host_msg::Msg::RoisContributed(_)) {
                return Err(control::Error::protocol("expected RoisContributed"));
            }
            Ok(pb::CommandDone {
                ok: true,
                ..Default::default()
            })
        },
    );
    b.region_type(
        "tetra.volume",
        Some("Tetrahedron (keeps its volume)".into()),
        |_, edit| Ok(shapes::conserve_volume(edit)),
    );
    b.region_type_declaration("tetra.unshaped", Some("Tetrahedron (no shaper)".into()));
    b.on_shape_edit_ended(|_, ended| {
        let outcome = pb::shape_edit_ended::Outcome::try_from(ended.outcome)
            .map(|o| o.as_str_name().to_owned())
            .unwrap_or_else(|_| ended.outcome.to_string());
        eprintln!(
            "tetra_shape: drag {} ended {outcome}: {}",
            ended.drag_id, ended.detail
        );
        Ok(())
    });
    b.build()
}

#[tokio::main]
async fn main() -> std::process::ExitCode {
    vizi::declare::report_exit(run().await)
}

async fn run() -> Result<(), Box<dyn std::error::Error>> {
    let Some(plugin) = ManagementCommand::prepare(
        "org.example.tetra_shape",
        env!("CARGO_PKG_VERSION"),
        std::env::args_os().skip(1),
        declare,
    )?
    else {
        return Ok(());
    };
    // The host transfers exclusive ownership of its inherited doorbell.
    let runtime = unsafe { plugin.into_runtime_from_env() }?;
    runtime.serve_env().await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    #[test]
    fn manifest_matches_spec_and_development_snapshot() {
        let mut actual = super::declare().unwrap().manifest().value().clone();
        let mut expected: serde_json::Value =
            serde_json::from_str(include_str!("../vizi-plugin.json")).unwrap();
        actual.as_object_mut().unwrap().remove("entry");
        expected.as_object_mut().unwrap().remove("entry");
        assert_eq!(actual, expected);
        assert_eq!(
            actual["permissions"],
            serde_json::json!(["contribute_rois"])
        );
        assert_eq!(
            actual["contributions"]["commands"],
            serde_json::json!([{
                "id":"tetra.contribute", "title":"Contribute the tetrahedra", "menu":null
            }])
        );
        assert_eq!(
            actual["contributions"]["region_types"],
            serde_json::json!([
                {"id":"tetra.volume", "name":"Tetrahedron (keeps its volume)"},
                {"id":"tetra.unshaped", "name":"Tetrahedron (no shaper)"}
            ])
        );
    }
}
