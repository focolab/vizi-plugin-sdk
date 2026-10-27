use super::*;
use vizi_doorbell::control::Error;

/// §8.3: a handler's outcome is its
/// `CommandDone`, and `command_id` echoes the invoked command's id on
/// success, on failure and on an argument-validation failure alike. A
/// handler `Err` (an `InvalidArgument` argument check among them) and a panic
/// report `ok: false` with the failure's text, as Go, Java and Python do.
/// What is not a handler outcome stays `Error`: an undeclared command
/// (`NOT_FOUND`, as Java) and a cancellation (`CANCELLED`); a declared command
/// with no handler is `CommandDone{ok: false}` (`command_outcomes_follow_section_8_3`).
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn every_command_done_echoes_the_invoked_command_id() {
    let app = Registry::new().command("run", |_, command| {
        let mode = match command.args.get("mode").and_then(|v| v.kind.as_ref()) {
            Some(pb::value::Kind::S(mode)) => mode.clone(),
            Some(_) => {
                return Err(Error::new(
                    pb::error::Kind::InvalidArgument,
                    "mode: expected a string",
                ))
            }
            None => {
                return Err(Error::new(
                    pb::error::Kind::InvalidArgument,
                    "mode: missing",
                ))
            }
        };
        match mode.as_str() {
            "ok" => Ok(done()),
            "names-another" => Ok(pb::CommandDone {
                command_id: "other".into(),
                ok: true,
                ..Default::default()
            }),
            // The handler's own refusal, with the id left for the SDK.
            "refused" => Ok(pb::CommandDone {
                ok: false,
                message: "refused".into(),
                ..Default::default()
            }),
            "io" => Err(Error::new(pb::error::Kind::IoError, "disk full")),
            "cancelled" => Err(Error::cancelled("stopped")),
            _ => panic!("boom"),
        }
    });
    let mut s = Session::new(app).await;
    s.recv().await;
    let invoke = |command: &str, mode: Option<pb::Value>| {
        Host::InvokeCommand(pb::InvokeCommand {
            command_id: command.into(),
            args: mode.into_iter().map(|v| ("mode".into(), v)).collect(),
            ..Default::default()
        })
    };
    for (id, mode, ok, message) in [
        ("success", Some("ok".into()), true, ""),
        ("names-another", Some("names-another".into()), true, ""),
        ("refused", Some("refused".into()), false, "refused"),
        ("missing-arg", None, false, "mode: missing"),
        (
            "ill-typed-arg",
            Some(pb::Value {
                kind: Some(pb::value::Kind::I(3)),
            }),
            false,
            "mode: expected a string",
        ),
        ("handler-error", Some("io".into()), false, "disk full"),
        ("panic", Some("panic".into()), false, "handler panic: boom"),
    ] {
        s.send(id, invoke("run", mode)).await;
        let reply = s.recv().await;
        assert_eq!(reply.id, id);
        match reply.msg {
            Some(Plugin::CommandDone(c)) => {
                assert_eq!(c.command_id, "run", "{id}");
                assert_eq!(c.ok, ok, "{id}");
                assert_eq!(c.message, message, "{id}");
            }
            other => panic!("{id}: {other:?}"),
        }
    }
    for (id, command, mode, kind) in [
        (
            "unknown",
            "nobody-declared-this",
            None,
            pb::error::Kind::NotFound,
        ),
        (
            "cancelled",
            "run",
            Some("cancelled".into()),
            pb::error::Kind::Cancelled,
        ),
    ] {
        s.send(id, invoke(command, mode)).await;
        let reply = s.recv().await;
        assert_eq!(reply.id, id);
        assert!(
            matches!(&reply.msg, Some(Plugin::Error(e)) if e.kind == kind as i32),
            "{id}: {:?}",
            reply.msg
        );
    }
    s.stop().await;
}

/// §8.3, uniform across the four SDKs: an undeclared command is
/// `Error{NOT_FOUND}`; a declared command with no handler registered is
/// `CommandDone{ok: false}` naming it; a cancellation is `Error{CANCELLED}`
/// -- here the handler's `Err`, and the host's `CancelWork` in
/// `work::unwinding_command_bypasses_result_mapping_and_discards_after_reply`.
/// The texts are Java's.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn command_outcomes_follow_section_8_3() {
    // The test manifest declares `run`; this registry registers no handler.
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let invoke = |command: &str| {
        Host::InvokeCommand(pb::InvokeCommand {
            command_id: command.into(),
            ..Default::default()
        })
    };
    s.send("declared", invoke("run")).await;
    let reply = s.recv().await;
    assert_eq!(reply.id, "declared");
    match reply.msg {
        Some(Plugin::CommandDone(c)) => {
            assert_eq!(c.command_id, "run");
            assert!(!c.ok);
            assert_eq!(c.message, "no command run");
        }
        other => panic!("declared without a handler: {other:?}"),
    }
    s.send("undeclared", invoke("nobody-declared-this")).await;
    let reply = s.recv().await;
    assert_eq!(reply.id, "undeclared");
    match reply.msg {
        Some(Plugin::Error(e)) => {
            assert_eq!(e.kind, pb::error::Kind::NotFound as i32);
            assert_eq!(
                e.message,
                "command \"nobody-declared-this\" is not one this plugin declares"
            );
        }
        other => panic!("undeclared: {other:?}"),
    }
    s.stop().await;

    let mut s =
        Session::new(Registry::new().command("run", |_, _| Err(Error::cancelled("stopped")))).await;
    s.recv().await;
    s.send("cancelled", invoke("run")).await;
    let reply = s.recv().await;
    assert!(
        matches!(&reply.msg, Some(Plugin::Error(e)) if e.kind == pb::error::Kind::Cancelled as i32),
        "canceled: {:?}",
        reply.msg
    );
    s.stop().await;
}
