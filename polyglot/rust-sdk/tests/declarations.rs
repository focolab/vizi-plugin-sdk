use vizi_doorbell::{declare::*, manifest::Manifest, proto as pb};

#[test]
fn reader_declarations_add_fallback_suffixes_without_changing_existing_literals() {
    let old = ReaderSpec {
        id: "r".into(),
        name: "Reader".into(),
        extensions: vec!["nwb".into()],
    };
    let mut b = builder();
    b.reader(old.clone(), |_, _| unreachable!());
    let plugin = b.build().unwrap();
    assert_eq!(
        plugin.manifest().value()["contributions"]["readers"][0],
        serde_json::to_value(&old).unwrap()
    );
    let mut b = builder();
    b.reader(
        old.clone().fallback_extensions(["h5", ".hdf5"]),
        |_, _| unreachable!(),
    );
    let plugin = b.build().unwrap();
    assert_eq!(
        plugin.manifest().value()["contributions"]["readers"][0]["fallback_extensions"],
        serde_json::json!(["h5", ".hdf5"])
    );
    let mut b = builder();
    b.reader(old.fallback_extensions([".NWB"]), |_, _| unreachable!());
    assert!(b.build().err().unwrap().contains("a suffix is in both"));
    let mut b = builder();
    b.reader(
        ReaderSpec::new("fallback", "Generic", [] as [&str; 0]).fallback_extensions([".h5"]),
        |_, _| unreachable!(),
    );
    b.build().unwrap();
}

#[test]
fn a_protocol_floor_newer_than_the_sdk_is_refused_with_the_engines_sentence() {
    let mut b = builder();
    b.minimum_protocol(1, 1);
    assert_eq!(
        b.build().err().unwrap(),
        "Declared 1.2.3 needs vizi protocol 1.1; this vizi speaks 1.0"
    );
    let mut b = builder();
    b.minimum_protocol(1, 0);
    b.build().unwrap();
}

#[test]
fn the_manifest_states_the_sdk_release_and_min_vizi() {
    let plugin = builder().build().unwrap();
    let value = plugin.manifest().value();
    assert_eq!(
        value["sdk_version"],
        serde_json::json!(vizi_doorbell::manifest::sdk_release())
    );
    assert_eq!(value["manifest_version"], serde_json::json!(1));
    // Unset, it is the first vizi release that speaks protocol 1.0.
    assert_eq!(value["min_vizi"], serde_json::json!("1.0.0"));
    let mut b = builder();
    b.min_vizi("0.9.7");
    assert_eq!(
        b.build().unwrap().manifest().value()["min_vizi"],
        serde_json::json!("0.9.7")
    );
    let mut b = builder();
    b.min_vizi("latest");
    assert_eq!(
        b.build().err().unwrap(),
        "min_vizi 'latest' is not a PEP 440 version"
    );
}

fn builder() -> PluginBuilder {
    let mut builder = PluginBuilder::new("org.example.declared", "Declared", "1.2.3");
    let run = builder.command(CommandSpec::new("run", "Run"), |_, _| {
        Ok(pb::CommandDone {
            ok: true,
            ..Default::default()
        })
    });
    builder.panel(PanelSpec::new(
        "controls",
        "Controls",
        Widget::column(
            "root",
            [
                Widget::label("status", "Ready"),
                Widget::button("run-button", "Run", &run),
            ],
        ),
    ));
    builder
}

#[test]
fn labels_keep_plain_defaults_and_declare_rich_text_for_rows_too() {
    let plain = serde_json::json!({"kind":"label","id":"name","text":"<b>literal</b>"});
    for widget in [
        Widget::label("name", "<b>literal</b>"),
        Widget::Label {
            id: "name".into(),
            text: "<b>literal</b>".into(),
        },
        Widget::label_with_format("name", "<b>literal</b>", TextFormat::Plain),
    ] {
        assert_eq!(serde_json::to_value(widget).unwrap(), plain);
    }
    let rich = Widget::label_with_format("rich", "<b>Bold</b>", TextFormat::Rich);
    assert_eq!(
        serde_json::to_value(&rich).unwrap(),
        serde_json::json!({
            "kind":"label","id":"rich","text":"<b>Bold</b>","text_format":"rich"
        })
    );
    for root in [rich.clone(), Widget::list("rows", [rich])] {
        let mut b = builder();
        b.panel(PanelSpec::new("formatted", "Formatted", root));
        let plugin = b.build().unwrap();
        let root = &plugin.manifest().value()["contributions"]["panels"][1]["root"];
        let label = if root["kind"] == "list" {
            &root["row"][0]
        } else {
            root
        };
        assert_eq!(label["text_format"], "rich");
    }
}

#[test]
fn host_pickers_declare_typed_defaults_and_require_local_commands() {
    let mut b = builder();
    let set = b.command(
        CommandSpec::new("set", "Set"),
        |_, _| Ok(Default::default()),
    );
    let widgets = vec![
        Widget::ChannelPicker {
            moves_view: false,
            id: "channels".into(),
            command: set.clone(),
            multiple: true,
            value: Some(ChannelSelection::Multiple(vec![0, 2])),
        },
        Widget::ChannelPicker {
            moves_view: false,
            id: "channel".into(),
            command: set.clone(),
            multiple: false,
            value: Some(ChannelSelection::Single(3)),
        },
        Widget::ColorPicker {
            moves_view: false,
            id: "color".into(),
            command: set.clone(),
            value: Some("#FF8800".into()),
        },
        Widget::ColormapPicker {
            moves_view: false,
            id: "map".into(),
            command: set.clone(),
            value: Some("gray".into()),
        },
        Widget::RoiPicker {
            follows_selection: false,
            filter: RoiFilter::All,
            moves_view: false,
            id: "region".into(),
            command: set,
            value: None,
        },
    ];
    b.panel(PanelSpec::new(
        "pickers",
        "Pickers",
        Widget::column("root", widgets.clone()),
    ));
    b.permission(Permission::DataAccess)
        .subscription(Subscription::View)
        .subscription(Subscription::Regions);
    b.on_view_changed(|_, _| Ok(()))
        .on_regions_changed(|_, _| Ok(()));
    let plugin = b.build().unwrap();
    assert!(plugin.manifest().warnings.is_empty());
    let json = plugin.manifest().value();
    assert_eq!(
        json["subscriptions"],
        serde_json::json!(["view", "regions"])
    );
    assert_eq!(
        json["contributions"]["panels"][1]["root"]["children"][0]["value"],
        serde_json::json!([0, 2])
    );
    for widget in widgets {
        let mut other = builder();
        other.command(
            CommandSpec::new("set", "Set"),
            |_, _| Ok(Default::default()),
        );
        other.panel(PanelSpec::new("foreign", "Foreign", widget));
        assert!(
            other.build().is_err(),
            "picker accepted another builder's command"
        );
    }
    let mut invalid = builder();
    let cmd = invalid.command(
        CommandSpec::new("set", "Set"),
        |_, _| Ok(Default::default()),
    );
    invalid.panel(PanelSpec::new(
        "bad",
        "Bad",
        Widget::ChannelPicker {
            moves_view: false,
            id: "ch".into(),
            command: cmd,
            multiple: false,
            value: Some(ChannelSelection::Multiple(vec![0, 2])),
        },
    ));
    assert!(invalid.build().is_err());
}

#[test]
fn declarations_cover_every_contribution_and_keep_command_bindings() {
    let mut b = builder();
    // Factories need not be constructed or invoked to describe the interface.
    b.reader(
        ReaderSpec::new("reader", "Reader", [".raw"]),
        |_, _| unreachable!(),
    );
    b.exporter(
        ExporterSpec::new("exporter", "Exporter", ".raw"),
        |_, _| unreachable!(),
    );
    b.overlay(OverlaySpec::new("outline", "Outline"));
    b.chunked_overlay(
        ChunkedOverlaySpec::new("labels", "Labels", ChunkTime::Static),
        |_, _| Ok(None),
    );
    let mut tool = ToolSpec::new("paint", "Paint");
    tool.triggers.push(Trigger::Key {
        key: "P".into(),
        hold: Hold::Toggle,
    });
    let mut action = ActionSpec::new("add", "Add point");
    action.key = Some("N".into());
    tool.actions.push(action);
    b.tool(tool);
    b.permission(Permission::DataAccess);
    b.asset(AssetSpec {
        id: "weights".into(),
        name: "Weights".into(),
        filename: "weights.bin".into(),
        size: 1,
        sha256: "0".repeat(64),
        source: AssetSource::Manual,
    });
    let p = b.build().unwrap();
    let v = p.manifest().value();
    assert_eq!(
        v["contributions"]["panels"][0]["root"]["children"][1]["command"],
        "run"
    );
    for kind in ["commands", "readers", "exporters", "panels", "tools"] {
        assert_eq!(v["contributions"][kind].as_array().unwrap().len(), 1);
    }
    assert_eq!(v["contributions"]["overlays"][1]["time"], "static");
    assert!(v["contributions"]["overlays"][1]
        .get("segment_mib")
        .is_none());
    Manifest::parse(&v.to_string()).unwrap();
}

#[test]
fn duplicate_registrations_cannot_silently_replace_handlers() {
    let mut b = builder();
    b.command(CommandSpec::new("run", "Other"), |_, _| unreachable!());
    assert!(b.build().err().unwrap().contains("duplicate commands"));
    let mut b = builder();
    b.overlay(OverlaySpec::new("same", "First"));
    b.chunked_overlay(
        ChunkedOverlaySpec::new("same", "Second", ChunkTime::PerT),
        |_, _| Ok(None),
    );
    assert!(b.build().err().unwrap().contains("duplicate overlays"));
}

#[test]
fn widget_references_are_owned_by_the_registering_builder() {
    let mut other = PluginBuilder::new("org.example.other", "Other", "1");
    let run = other.command(CommandSpec::new("run", "Run"), |_, _| unreachable!());
    let mut b = builder();
    // Even the same spelling as an actual command does not make this ours.
    b.panel(PanelSpec::new(
        "foreign",
        "Foreign",
        Widget::button("button", "Run", &run),
    ));
    assert!(b.build().err().unwrap().contains("another plugin builder"));
}

#[test]
fn schema_validation_rejects_bad_declarations_before_writing() {
    let mut b = builder();
    b.reader(
        ReaderSpec::new("bad", "Bad", Vec::<String>::new()),
        |_, _| unreachable!(),
    );
    assert!(b.build().err().unwrap().contains("non-empty"));
    let mut b = builder();
    b.panel(PanelSpec::new("empty", "Empty", Widget::column("root", [])));
    assert!(b.build().err().unwrap().contains("non-empty"));
    let mut b = builder();
    b.launch_command(["", "arg"]);
    assert!(b.build().err().unwrap().contains("non-blank executable"));
    let mut b = builder();
    b.icon("../outside.svg", b"<svg/>");
    assert!(b.build().err().unwrap().contains("relative path"));
    let mut b = builder();
    let mut command = CommandSpec::new("icon", "Icon");
    command.pane = Some(PaneButton::new("icon.svg"));
    b.command(command, |_, _| unreachable!());
    assert!(b.build().err().unwrap().contains("not supplied"));
}

#[test]
fn declared_startup_hint_is_optional_and_rejects_invalid_numbers() {
    assert!(builder().build().unwrap().manifest().value()["entry"]
        .get("startup_s")
        .is_none());
    for seconds in [0.25, 90.5, 600.0] {
        let mut b = builder();
        b.startup_s(seconds);
        let plugin = b.build().unwrap();
        assert_eq!(plugin.manifest().value()["entry"]["startup_s"], seconds);
    }
    for seconds in [0.0, -1.0, f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
        let mut b = builder();
        b.startup_s(seconds);
        assert_eq!(
            b.build().err().unwrap(),
            "entry.startup_s must be a positive number of seconds"
        );
    }
}

#[cfg(feature = "install")]
mod installation {
    use super::*;
    use std::{fs, path::Path, process::Command};

    #[test]
    fn declared_startup_hint_reaches_written_and_installed_manifests() {
        let root = tempfile::tempdir().unwrap();
        let mut b = builder();
        b.startup_s(90.5);
        let plugin = b.build().unwrap();
        let written = plugin.write_manifest(root.path().join("export")).unwrap();
        let installed = plugin
            .install(&InstallOptions {
                directory: Some(root.path().join("plugins")),
                copy_executable: false,
                ..Default::default()
            })
            .unwrap()
            .join("vizi-plugin.json");
        for path in [written, installed] {
            let manifest = Manifest::load(path).unwrap();
            assert_eq!(manifest.value()["entry"]["startup_s"], 90.5);
        }
    }

    fn with_icon(bytes: &[u8]) -> DeclaredPlugin {
        let mut b = builder();
        let mut command = CommandSpec::new("icon", "Icon");
        command.pane = Some(PaneButton::new("icons/button.svg"));
        b.command(command, |_, _| unreachable!());
        let mut tool = ToolSpec::new("tool", "Tool");
        tool.pane = Some(PaneButton::new("icons/button.svg"));
        let mut action = ActionSpec::new("action", "Action");
        action.pane = Some(PaneButton::new("icons/button.svg"));
        tool.actions.push(action);
        b.tool(tool);
        b.icon("icons/button.svg", bytes);
        b.build().unwrap()
    }

    #[test]
    fn installation_publishes_complete_icons_and_preserves_old_resources() {
        let root = tempfile::tempdir().unwrap();
        let options = InstallOptions {
            directory: Some(root.path().join("plugins with spaces é")),
            force: false,
            copy_executable: false,
        };
        let first = with_icon(b"<svg>first</svg>");
        let directory = first.install(&options).unwrap();
        let settings = directory.join("settings");
        assert!(
            !settings.exists(),
            "installation must not provision settings"
        );
        fs::create_dir(&settings).unwrap(); // The host creates this before launch.
        fs::write(settings.join("preferences.json"), "user settings").unwrap();
        let old = Manifest::load(directory.join("vizi-plugin.json")).unwrap();
        let old_icon = old.value()["contributions"]["commands"][1]["pane"]["icon"]
            .as_str()
            .unwrap();
        // Metadata-only edits and repeated writes do not consume another resource
        // generation. Exercise the direct writer as well as the installer.
        for _ in 0..3 {
            first.install(&options).unwrap();
            first.write_manifest(&directory).unwrap();
        }
        assert_eq!(
            Manifest::load(directory.join("vizi-plugin.json"))
                .unwrap()
                .value(),
            old.value()
        );
        assert_eq!(icon_directories(&directory), 1);
        fs::write(directory.join("user-notes.txt"), "keep").unwrap();
        with_icon(b"<svg>second</svg>").install(&options).unwrap();
        let new = Manifest::load(directory.join("vizi-plugin.json")).unwrap();
        let new_icon = new.value()["contributions"]["commands"][1]["pane"]["icon"]
            .as_str()
            .unwrap();
        assert_ne!(old_icon, new_icon);
        assert_eq!(icon_directories(&directory), 2);
        assert_eq!(
            fs::read(directory.join(old_icon)).unwrap(),
            b"<svg>first</svg>"
        );
        assert_eq!(
            fs::read(directory.join(new_icon)).unwrap(),
            b"<svg>second</svg>"
        );
        assert_eq!(
            fs::read_to_string(directory.join("user-notes.txt")).unwrap(),
            "keep"
        );
        assert_eq!(
            fs::read_to_string(settings.join("preferences.json")).unwrap(),
            "user settings"
        );
        for icon in [
            &new.value()["contributions"]["tools"][0]["pane"]["icon"],
            &new.value()["contributions"]["tools"][0]["actions"][0]["pane"]["icon"],
        ] {
            assert_eq!(icon.as_str().unwrap(), new_icon);
        }
        // Packaging relocates icons; it does not mutate the runtime declaration.
        assert_eq!(
            first.manifest().value()["contributions"]["commands"][1]["pane"]["icon"],
            "icons/button.svg"
        );
    }

    fn icon_directories(directory: &Path) -> usize {
        fs::read_dir(directory)
            .unwrap()
            .map(Result::unwrap)
            .filter(|entry| {
                entry
                    .file_name()
                    .to_string_lossy()
                    .starts_with(".vizi-rust-icons-")
            })
            .count()
    }

    #[test]
    fn metadata_changes_reuse_icons_but_modified_bytes_get_a_new_generation() {
        let directory = tempfile::tempdir().unwrap();
        let plugin = with_icon(b"<svg>first</svg>");
        plugin.write_manifest(directory.path()).unwrap();
        let path = directory.path().join("vizi-plugin.json");
        let old = Manifest::load(&path).unwrap();
        let icon = old.value()["contributions"]["commands"][1]["pane"]["icon"]
            .as_str()
            .unwrap();
        // Reuse depends on icon bytes, not plugin metadata.
        let mut metadata = old.value().clone();
        metadata["version"] = "different".into();
        fs::write(&path, metadata.to_string()).unwrap();
        plugin.write_manifest(directory.path()).unwrap();
        assert_eq!(icon_directories(directory.path()), 1);
        assert_eq!(Manifest::load(&path).unwrap().version(), "1.2.3");
        // Equal length and valid SVG are insufficient: compare the actual bytes.
        fs::write(directory.path().join(icon), b"<svg>other</svg>").unwrap();
        plugin.write_manifest(directory.path()).unwrap();
        let new = Manifest::load(&path).unwrap();
        let new_icon = new.value()["contributions"]["commands"][1]["pane"]["icon"]
            .as_str()
            .unwrap();
        assert_ne!(icon, new_icon);
        assert_eq!(icon_directories(directory.path()), 2);
        assert_eq!(
            fs::read(directory.path().join(new_icon)).unwrap(),
            b"<svg>first</svg>"
        );
        assert_eq!(
            fs::read(directory.path().join(icon)).unwrap(),
            b"<svg>other</svg>"
        );
    }

    #[test]
    fn unmanaged_directories_require_force_and_bad_ids_cannot_escape_root() {
        let root = tempfile::tempdir().unwrap();
        let target = root.path().join("org.example.declared");
        fs::create_dir(&target).unwrap();
        fs::write(target.join("vizi-plugin.json"), "original").unwrap();
        let mut options = InstallOptions {
            directory: Some(root.path().into()),
            force: false,
            copy_executable: false,
        };
        let plugin = builder().build().unwrap();
        assert!(plugin.install(&options).unwrap_err().contains("--force"));
        assert_eq!(
            fs::read_to_string(target.join("vizi-plugin.json")).unwrap(),
            "original"
        );
        options.force = true;
        plugin.install(&options).unwrap();
        Manifest::load(target.join("vizi-plugin.json")).unwrap();
        // The id grammar refuses these before installation is reached.
        for id in ["../outside", "a/b", "a\\b", "C:drive", "CON", "trailing."] {
            let error = PluginBuilder::new(id, "Name", "1").build().err().unwrap();
            assert!(error.contains("is not a plugin id"), "{id}: {error}");
        }
        // So does a Windows device name as the first part, which installation
        // would also refuse as a folder name.
        for id in ["con.example", "lpt1.txt"] {
            let error = PluginBuilder::new(id, "Name", "1").build().err().unwrap();
            assert!(error.contains("is a Windows device name"), "{id}: {error}");
        }
    }

    #[test]
    fn lock_and_publication_failure_preserve_existing_manifest() {
        let root = tempfile::tempdir().unwrap();
        let options = InstallOptions {
            directory: Some(root.path().into()),
            force: false,
            copy_executable: false,
        };
        let plugin = builder().build().unwrap();
        let target = plugin.install(&options).unwrap();
        assert_eq!(target, root.path().join("org.example.declared"));
        let old = fs::read(target.join("vizi-plugin.json")).unwrap();
        let lock = root
            .path()
            .join("org.example.declared")
            .join(".vizi-rust-install.lock");
        fs::write(&lock, "another installer").unwrap();
        let error = plugin.install(&options).unwrap_err();
        assert!(error.contains(&lock.display().to_string()));
        assert!(error.contains("confirm no installer is running"));
        assert!(error.contains("remove this lock file, and retry"));
        assert_eq!(fs::read(target.join("vizi-plugin.json")).unwrap(), old);
        assert!(lock.exists());
        fs::remove_file(lock).unwrap();
        // Following the diagnostic's recovery instruction restores installation.
        plugin.install(&options).unwrap();
        // Two valid icon names whose paths cannot both be files and directories.
        let mut b = builder();
        for path in ["a.svg", "a.svg/b.svg"] {
            let mut c = CommandSpec::new(path, path);
            c.pane = Some(PaneButton::new(path));
            b.command(c, |_, _| unreachable!());
            b.icon(path, b"<svg/>");
        }
        assert!(b.build().unwrap().install(&options).is_err());
        assert_eq!(fs::read(target.join("vizi-plugin.json")).unwrap(), old);
    }

    #[test]
    fn cli_options_do_not_require_a_manifest_or_host_environment() {
        assert!(ManagementCommand::parse(Vec::<String>::new())
            .unwrap()
            .is_none());
        assert!(matches!(
            ManagementCommand::parse(["--install"]).unwrap(),
            Some(ManagementCommand::Install(InstallOptions {
                directory: None,
                force: false,
                copy_executable: true
            }))
        ));
        for args in [
            vec!["--install", "a b", "--force"],
            vec!["--force", "--install=a b"],
        ] {
            let Some(ManagementCommand::Install(options)) = ManagementCommand::parse(args).unwrap()
            else {
                panic!()
            };
            assert!(options.force);
            assert_eq!(options.directory.unwrap(), Path::new("a b"));
        }
        for args in [
            vec!["--copy=false", "--install", "a b"],
            vec!["--install=a b", "--copy=false", "--force"],
        ] {
            let Some(ManagementCommand::Install(options)) = ManagementCommand::parse(args).unwrap()
            else {
                panic!()
            };
            assert!(!options.copy_executable);
        }
        assert!(matches!(
            ManagementCommand::parse(["--manifest"]).unwrap(),
            Some(ManagementCommand::Manifest(_))
        ));
        for args in [
            vec!["--force"],
            vec!["--manifest", "--install"],
            vec!["--install="],
            vec!["--help", "--force"],
            vec!["--bogus"],
            vec!["--copy=false"],
            vec!["--manifest", "--copy=true"],
            vec!["--copy=false", "--install", "--copy=true"],
            vec!["--install", "--copy=maybe"],
        ] {
            assert!(ManagementCommand::parse(args).is_err());
        }
    }

    #[test]
    fn explicit_launch_command_preserves_arguments() {
        let root = tempfile::tempdir().unwrap();
        let mut b = builder();
        let command = vec![
            "/a path/with spaces/plugin".to_owned(),
            "a quoted \"value\"".into(),
            "".into(),
        ];
        b.launch_command(command.clone());
        let plugin = b.build().unwrap();
        let path = plugin.write_manifest(root.path()).unwrap();
        assert_eq!(path, root.path().join("vizi-plugin.json"));
        assert_eq!(
            Manifest::load(path).unwrap().value()["entry"]["command"],
            serde_json::json!(command)
        );
    }

    #[test]
    fn custom_launcher_requires_explicit_in_place_install() {
        let root = tempfile::tempdir().unwrap();
        let options = InstallOptions {
            directory: Some(root.path().into()),
            ..Default::default()
        };
        let mut b = builder();
        b.launch_command(["custom-runner", "--arg"]);
        let plugin = b.build().unwrap();
        assert!(plugin
            .install(&options)
            .unwrap_err()
            .contains("--copy=false"));
        assert!(!root
            .path()
            .join("org.example.declared/vizi-plugin.json")
            .exists());
        let directory = plugin
            .install(&InstallOptions {
                copy_executable: false,
                ..options
            })
            .unwrap();
        assert_eq!(
            Manifest::load(directory.join("vizi-plugin.json"))
                .unwrap()
                .value()["entry"]["command"],
            serde_json::json!(["custom-runner", "--arg"])
        );
    }

    // Run this test in a child process so checking environment precedence is safe
    // alongside the parallel test suite; no process-global env mutation is needed.
    #[test]
    fn install_child() {
        if std::env::var_os("VIZI_TEST_INSTALL_CHILD").is_none() {
            return;
        }
        builder()
            .build()
            .unwrap()
            .install(&InstallOptions::default())
            .unwrap();
    }

    #[test]
    fn running_executable_installs_itself_and_the_result_launches() {
        let root = tempfile::tempdir().unwrap();
        let plugins = root.path().join("plugins space é");
        let exe = std::env::current_exe().unwrap();
        let source = root
            .path()
            .join(format!("source{}", std::env::consts::EXE_SUFFIX));
        fs::copy(&exe, &source).unwrap();
        let output = Command::new(&source)
            .args(["--exact", "installation::install_child", "--nocapture"])
            .env("VIZI_TEST_INSTALL_CHILD", "1")
            .env("VIZI_PLUGINS_DIR", &plugins)
            .current_dir(root.path())
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        let directory = plugins.join("org.example.declared");
        let installed = Manifest::load(directory.join("vizi-plugin.json")).unwrap();
        let command = installed.value()["entry"]["command"].as_array().unwrap();
        assert_eq!(command.len(), 1);
        let copied = directory.join(command[0].as_str().unwrap());
        assert_ne!(copied, exe.canonicalize().unwrap());
        assert!(command[0].as_str().unwrap().starts_with("./bin/plugin-"));
        assert_eq!(fs::read(&copied).unwrap(), fs::read(&exe).unwrap());
        // Same build reuses the immutable artifact.
        let rerun = Command::new(&source)
            .args(["--exact", "installation::install_child", "--nocapture"])
            .env("VIZI_TEST_INSTALL_CHILD", "1")
            .env("VIZI_PLUGINS_DIR", &plugins)
            .output()
            .unwrap();
        assert!(rerun.status.success());
        assert_eq!(fs::read_dir(directory.join("bin")).unwrap().count(), 1);
        fs::remove_file(&source).unwrap();
        // Resolve the relative manifest entry against the plugin directory as
        // vizi does. current_dir alone does not resolve the program on Windows.
        let output = Command::new(&copied)
            .arg("--list")
            .current_dir(&directory)
            .output()
            .unwrap();
        assert!(output.status.success());
        assert!(String::from_utf8_lossy(&output.stdout).contains("install_child"));
    }

    #[cfg(unix)]
    #[test]
    fn installer_refuses_symlink_targets_and_manifest_files() {
        use std::os::unix::fs::symlink;
        let root = tempfile::tempdir().unwrap();
        let outside = tempfile::tempdir().unwrap();
        symlink(outside.path(), root.path().join("org.example.declared")).unwrap();
        let p = builder().build().unwrap();
        let options = InstallOptions {
            directory: Some(root.path().into()),
            force: true,
            copy_executable: false,
        };
        assert!(p.install(&options).unwrap_err().contains("symlink"));
        let victim = root.path().join("victim");
        fs::write(&victim, "unchanged").unwrap();
        symlink(&victim, outside.path().join("vizi-plugin.json")).unwrap();
        assert!(p
            .write_manifest(outside.path())
            .unwrap_err()
            .contains("symlink"));
        assert_eq!(fs::read_to_string(victim).unwrap(), "unchanged");
    }
}

#[test]
fn progress_and_epoch_subscriptions_need_no_commands_or_tools() {
    for value in [0.0, 0.5, 1.0, -0.1, 1.1, f64::NAN, f64::INFINITY] {
        let mut b = PluginBuilder::new("org.example.progress", "Progress", "1");
        b.subscription(Subscription::Epochs);
        b.panel(PanelSpec::new(
            "p",
            "Progress",
            Widget::progress("bar", value, false),
        ));
        let result = b.build();
        assert_eq!(result.is_ok(), (0.0..=1.0).contains(&value));
        if let Ok(p) = result {
            let manifest = p.manifest().value();
            assert_eq!(manifest["subscriptions"], serde_json::json!(["epochs"]));
            assert!(manifest["contributions"].get("commands").is_none());
            assert!(manifest["contributions"].get("tools").is_none());
            assert!(manifest["contributions"]["panels"][0]["root"]
                .get("command")
                .is_none());
        }
    }
}

#[test]
fn lists_share_command_ownership_and_validate_the_template() {
    let mut b = builder();
    let cmd = b.command(CommandSpec::new("cell", "Cell"), |_, _| {
        Ok(Default::default())
    });
    let plain = Widget::list(
        "rows",
        [Widget::button("cell", "Run", &cmd), Widget::image("img")],
    );
    let selectable = Widget::List {
        moves_view: false,
        id: "select".into(),
        row: vec![Widget::label("name", "Name")],
        max_rows: 12,
        visible_rows: 3,
        selectable: true,
        command: Some(cmd),
    };
    b.panel(PanelSpec::new(
        "lists",
        "Lists",
        Widget::column("root", [plain.clone(), selectable.clone()]),
    ));
    let plugin = b.build().unwrap();
    assert_eq!(
        plugin.manifest().widget_count("lists"),
        Some(1 + 1 + 2 * 256 + 1 + 12)
    );
    let root = &plugin.manifest().value()["contributions"]["panels"][1]["root"];
    assert_eq!(root["children"][0]["visible_rows"], 8);
    assert!(root["children"][0].get("command").is_none());
    assert_eq!(root["children"][1]["command"], "cell");
    for widget in [plain, selectable] {
        let mut other = builder();
        other.command(CommandSpec::new("cell", "Cell"), |_, _| {
            Ok(Default::default())
        });
        other.panel(PanelSpec::new("foreign", "Foreign", widget));
        assert!(other.build().is_err());
    }
    for cells in [
        vec![],
        vec![Widget::column("nested", [Widget::label("cell", "")])],
    ] {
        let mut b = builder();
        b.panel(PanelSpec::new(
            "invalid",
            "Invalid",
            Widget::list("rows", cells),
        ));
        assert!(b.build().is_err());
    }
}

#[test]
fn set_view_declarations_and_clickable_image_ownership() {
    let mut b = builder();
    let command = b.command(CommandSpec::new("jump", "Jump"), |_, _| {
        Ok(Default::default())
    });
    let image = Widget::Image {
        id: "trace".into(),
        command: Some(command.clone()),
        moves_view: true,
    };
    let mut panel = PanelSpec::new(
        "navigation",
        "Navigate",
        Widget::column(
            "root",
            [
                image.clone(),
                Widget::RoiPicker {
                    id: "roi".into(),
                    command,
                    value: None,
                    moves_view: false,
                    follows_selection: true,
                    filter: RoiFilter::Mine,
                },
            ],
        ),
    );
    panel.view_targets = vec![ViewTarget::T, ViewTarget::SelectRegion, ViewTarget::LookAt];
    panel.follow_targets = vec![ViewTarget::FrameRegion, ViewTarget::OrbitPoint];
    b.panel(panel);
    let mut tool = ToolSpec::new("navigation", "Navigate");
    tool.view_targets = vec![
        ViewTarget::Z,
        ViewTarget::FrameRegion,
        ViewTarget::OrbitPoint,
    ];
    b.tool(tool);
    let plugin = b.build().unwrap();
    let v = plugin.manifest().value();
    assert_eq!(
        v["contributions"]["panels"][1]["follow_targets"],
        serde_json::json!(["frame_region", "orbit_point"])
    );
    assert_eq!(
        v["contributions"]["panels"][1]["view_targets"],
        serde_json::json!(["t", "select_region", "look_at"])
    );
    assert_eq!(
        v["contributions"]["tools"][0]["view_targets"],
        serde_json::json!(["z", "frame_region", "orbit_point"])
    );
    assert_eq!(
        v["contributions"]["panels"][1]["root"]["children"][1]["filter"],
        "mine"
    );
    let mut other = builder();
    other.command(CommandSpec::new("jump", "Jump"), |_, _| {
        Ok(Default::default())
    });
    let mut panel = PanelSpec::new("foreign", "Foreign", image);
    panel.view_targets = vec![ViewTarget::T];
    other.panel(panel);
    assert!(
        other.build().is_err(),
        "foreign clickable image command accepted"
    );
}

#[test]
fn declarations_enforce_reachable_starters_and_realised_cell_budget() {
    for pane in [false, true] {
        let mut b = PluginBuilder::new("org.example.test", "Test", "1");
        b.overlay(OverlaySpec::new("overlay", "Overlay"));
        let mut command = CommandSpec::new("run", "Run");
        command.menu = None;
        command.default_keybinding = Some("T".into());
        if pane {
            b.icon(
                "icons/tool.svg",
                b"<svg xmlns=\"http://www.w3.org/2000/svg\"/>",
            );
            command.pane = Some(PaneButton::new("icons/tool.svg"));
        }
        b.command(command, |_, _| Ok(pb::CommandDone::default()));
        assert_eq!(b.build().is_ok(), pane);
    }
    for cells in [4, 5] {
        let mut b = PluginBuilder::new("org.example.test", "Test", "1");
        let widget = Widget::List {
            id: "rows".into(),
            max_rows: 1024,
            visible_rows: 8,
            selectable: false,
            command: None,
            moves_view: false,
            row: (0..cells)
                .map(|n| Widget::label(format!("cell{n}"), ""))
                .collect(),
        };
        b.panel(PanelSpec::new("p", "P", widget));
        assert_eq!(b.build().is_ok(), cells == 4);
    }
}

#[test]
fn region_type_builder_declares_and_registers_shaper_and_requires_permission() {
    use vizi_doorbell::{
        declare::{Permission, PluginBuilder},
        shape::Shape,
    };
    let mut b = PluginBuilder::new("test.shapes", "Shapes", "1");
    b.region_type("tetra", Some("Tetrahedron".into()), |_, _| {
        Ok(Shape::rejected("not now"))
    });
    assert!(b.build().err().unwrap().contains("contribute_rois"));
    let mut b = PluginBuilder::new("test.shapes", "Shapes", "1");
    b.permission(Permission::ContributeRois);
    b.region_type("tetra", None, |_, _| Ok(Shape::rejected("not now")));
    b.on_shape_edit_ended(|_, _| Ok(()));
    let plugin = b.build().unwrap();
    assert_eq!(
        plugin.manifest().value()["contributions"]["region_types"][0]["id"],
        "tetra"
    );
}

#[test]
fn declaration_only_region_types_are_validated_without_requiring_a_handler() {
    use vizi_doorbell::declare::{Permission, PluginBuilder};
    let mut b = PluginBuilder::new("test.unshaped", "Unshaped", "1");
    b.region_type_declaration("unshaped", Some("No shaper".into()));
    assert!(b.build().err().unwrap().contains("contribute_rois"));
    let mut b = PluginBuilder::new("test.unshaped", "Unshaped", "1");
    b.permission(Permission::ContributeRois);
    b.region_type_declaration("unshaped", Some("No shaper".into()));
    assert_eq!(
        b.build().unwrap().manifest().value()["contributions"]["region_types"],
        serde_json::json!([{"id":"unshaped", "name":"No shaper"}])
    );
    let mut b = PluginBuilder::new("test.unshaped", "Unshaped", "1");
    b.permission(Permission::ContributeRois);
    b.region_type_declaration("unshaped", None);
    b.region_type("unshaped", None, |_, _| {
        Ok(vizi_doorbell::shape::Shape::rejected("unused"))
    });
    assert!(b.build().err().unwrap().contains("duplicate region type"));
}

/// `wants_pick` is a v1 boolean, default true.
#[test]
fn a_tool_declares_whether_it_wants_the_hosts_pick() {
    let wants = ToolSpec::new("t", "T");
    assert!(wants.wants_pick);
    // The default is not written: every tool before the field wanted the pick.
    assert!(serde_json::to_value(&wants)
        .unwrap()
        .get("wants_pick")
        .is_none());
    let mut declines = ToolSpec::new("t", "T");
    declines.wants_pick = false;
    assert_eq!(
        serde_json::to_value(&declines).unwrap()["wants_pick"],
        serde_json::json!(false)
    );

    let parse = |version: u64, field: &str| {
        Manifest::parse(&format!(
            r#"{{"manifest_version":{version},"id":"org.example.x","name":"X","version":"1","engines":{{"vizi":"1.0"}},"entry":{{"command":["x"]}},"contributions":{{"tools":[{{"id":"t"{field}}}]}}}}"#
        ))
    };
    for accepted in ["", r#","wants_pick":true"#, r#","wants_pick":false"#] {
        parse(1, accepted).unwrap();
    }
    for bad in [r#""no""#, "0", "null", "[false]"] {
        assert_eq!(
            parse(1, &format!(r#","wants_pick":{bad}"#)).unwrap_err(),
            "wants_pick must be a boolean",
            "{bad}"
        );
    }
    // manifest_version 0 is refused for itself.
    assert!(parse(0, r#","wants_pick":false"#)
        .unwrap_err()
        .starts_with("manifest_version 0 unsupported"));
}

#[test]
fn a_slider_declares_step_only_when_given() {
    let mut b = builder();
    let set = b.command(
        CommandSpec::new("set", "Set"),
        |_, _| Ok(Default::default()),
    );
    let slider = |id: &str, step| Widget::Slider {
        id: id.into(),
        moves_view: false,
        command: set.clone(),
        min: 0.0,
        max: 1.0,
        step,
        value: None,
        float: true,
    };
    let free = serde_json::to_value(slider("free", None)).unwrap();
    assert!(free.get("step").is_none(), "{free}");
    let stepped = serde_json::to_value(slider("stepped", Some(0.05))).unwrap();
    assert_eq!(stepped["step"], 0.05);
}

#[test]
fn an_integer_slider_with_a_fractional_step_is_refused_at_build() {
    let mut b = builder();
    let set = b.command(
        CommandSpec::new("set", "Set"),
        |_, _| Ok(Default::default()),
    );
    b.panel(PanelSpec::new(
        "sliders",
        "Sliders",
        Widget::Slider {
            id: "count".into(),
            moves_view: false,
            command: set,
            min: 0.0,
            max: 10.0,
            step: Some(2.5),
            value: None,
            float: false,
        },
    ));
    let e = b.build().err().expect("refused").to_string();
    assert!(
        e.contains("slider 'count' step 2.5 is not a whole number; an integer slider moves by whole steps (or declare it float)"),
        "{e}"
    );
}
