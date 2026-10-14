use vizi_doorbell::manifest::Manifest;

#[test]
fn reader_suffixes_validate_both_lists_in_reference_order() {
    use serde_json::json;
    for (reader, error) in [
        (
            json!({"id":"r","fallback_extensions":[".h5",".ome.tif"]}),
            None,
        ),
        (
            json!({"id":"r","extensions":[],"fallback_extensions":["H5"]}),
            None,
        ),
        (
            json!({"id":"r","extensions":[".ome.tif"],"fallback_extensions":["tif"]}),
            None,
        ),
        (json!({"id":"r"}), Some("extensions must be non-empty")),
        (
            json!({"id":"r","extensions":[],"fallback_extensions":[]}),
            Some("extensions must be non-empty"),
        ),
        (
            json!({"id":"r","extensions":null,"fallback_extensions":42}),
            Some("extensions must be a list"),
        ),
        (
            json!({"id":"r","extensions":[],"fallback_extensions":42}),
            Some("fallback_extensions must be a list"),
        ),
        (
            json!({"id":"r","extensions":["."],"fallback_extensions":42}),
            Some("each extension must be a non-empty string"),
        ),
        (
            json!({"id":"r","extensions":["."],"fallback_extensions":".h5"}),
            Some("each extension must be a non-empty string"),
        ),
        (
            json!({"id":"r","extensions":[".x"],"fallback_extensions":[". \t"]}),
            Some("each fallback extension must be a non-empty string"),
        ),
        (
            json!({"id":"r","extensions":[".."]}),
            Some("each extension must be a non-empty string"),
        ),
        (
            json!({"id":"r","extensions":[42]}),
            Some("each extension must be a non-empty string"),
        ),
        (
            json!({"id":"r","fallback_extensions":[""]}),
            Some("each fallback extension must be a non-empty string"),
        ),
        (
            json!({"id":"r","fallback_extensions":[null]}),
            Some("each fallback extension must be a non-empty string"),
        ),
        (
            json!({"id":"r","extensions":["..H5",".OME.TIF"],"fallback_extensions":["ome.tif","h5"]}),
            Some("a suffix is in both extensions and fallback_extensions: ['.h5', '.ome.tif']"),
        ),
    ] {
        let manifest = json!({"manifest_version":1,"id":"x","name":"X","version":"1",
            "engines":{"vizi":"1.0"},"entry":{"command":["x"]},"contributions":{"readers":[reader]}});
        let result = Manifest::parse(&manifest.to_string());
        match error {
            Some(error) => assert_eq!(result.unwrap_err(), error),
            None => {
                result.unwrap();
            }
        }
    }
}

#[test]
fn reader_suffixes_reject_only_ascii_blank_after_leading_dots() {
    use serde_json::json;
    for key in ["extensions", "fallback_extensions"] {
        let error = if key == "extensions" {
            "each extension must be a non-empty string"
        } else {
            "each fallback extension must be a non-empty string"
        };
        for (suffix, accepted) in [
            ("", false),
            ("...", false),
            (" ", false),
            (".\t", false),
            (".\r", false),
            (".\n", false),
            (".\u{b}", false),
            (".\u{c}", false),
            (".. \t\r\n\u{b}\u{c}", false),
            (".\u{a0}", true),
            (".\u{2003}", true),
            (". h5 ", true),
            (". .", true),
        ] {
            let mut reader = json!({"id":"r"});
            reader[key] = json!([suffix]);
            let manifest = json!({"manifest_version":1,"id":"x","name":"X","version":"1",
                "engines":{"vizi":"1.0"},"entry":{"command":["x"]},"contributions":{"readers":[reader]}});
            let result = Manifest::parse(&manifest.to_string());
            if accepted {
                assert!(result.is_ok(), "{key}: {suffix:?}: {result:?}");
            } else {
                assert_eq!(result.unwrap_err(), error, "{key}: {suffix:?}");
            }
        }
    }
}

#[test]
fn exporter_extensions_follow_the_reader_suffix_rule() {
    use serde_json::json;
    for (extension, accepted) in [
        ("", false),
        (".", false),
        ("...", false),
        (" ", false),
        (".\t", false),
        (".\r", false),
        (".\n", false),
        (".\u{b}", false),
        (".\u{c}", false),
        (".. \t\r\n\u{b}\u{c}", false),
        ("\u{a0}", true),
        (".\u{2003}", true),
        ("..CSV", true),
        (".ome.tif", true),
        (". h5 ", true),
        (". .", true),
    ] {
        let manifest = json!({"manifest_version":1,"id":"x","name":"X","version":"1",
            "engines":{"vizi":"1.0"},"entry":{"command":["x"]},
            "contributions":{"exporters":[{"id":"e","extension":extension}]}});
        let result = Manifest::parse(&manifest.to_string());
        if accepted {
            assert!(result.is_ok(), "{extension:?}: {result:?}");
        } else {
            assert_eq!(
                result.unwrap_err(),
                "extension must be non-empty",
                "{extension:?}"
            );
        }
    }
}

#[test]
fn overlays_require_a_reachable_menu_or_pane_starter() {
    use serde_json::json;
    let directory = IconDirectory::new();
    let error = "overlays are drawn only while the plugin runs, and nothing declared here starts it; declare a panel (or a command or tool in the Tools menu or on the pane, a reader, an exporter or a stage)";
    for overlay in [
        json!({"id":"labels"}),
        json!({"id":"labels","chunked":true,"time":"static"}),
    ] {
        for (contributions, accepted) in [
            (json!({}), false),
            (
                json!({"panels":[],"tools":[],"readers":[],"exporters":[],"stages":[],"commands":[]}),
                false,
            ),
            (
                json!({"commands":[{"id":"c","title":"C","menu":null}]}),
                false,
            ),
            (
                json!({"commands":[{"id":"c","title":"C","menu":null,"default_keybinding":null}]}),
                false,
            ),
            (json!({"commands":[{"id":"c","title":"C"}]}), true),
            (
                json!({"commands":[{"id":"c","title":"C","menu":"tools"}]}),
                true,
            ),
            (
                json!({"commands":[{"id":"c","title":"C","menu":null,"default_keybinding":"Ctrl+Shift+M"}]}),
                false,
            ),
            (
                json!({"panels":[{"id":"p","title":"P","root":{"id":"l","kind":"label"}}]}),
                true,
            ),
            (json!({"tools":[{"id":"t","menu":null}]}), false),
            (
                json!({"tools":[{"id":"t","menu":null,"triggers":[{"key":"T"}]}]}),
                false,
            ),
            (json!({"tools":[{"id":"t"}]}), true),
            (
                json!({"tools":[{"id":"t","menu":null,"pane":{"icon":"icons/tool.svg"}}]}),
                true,
            ),
            (
                json!({"commands":[{"id":"c","title":"C","menu":null,"pane":{"icon":"icons/tool.svg"}}]}),
                true,
            ),
            (json!({"readers":[{"id":"r","extensions":["raw"]}]}), true),
            (json!({"exporters":[{"id":"e","extension":"raw"}]}), true),
            (json!({"stages":[{"id":"s"}]}), true),
        ] {
            let mut value = json!({"manifest_version":1,"id":"x","name":"X","version":"1",
                "engines":{"vizi":"1.0"},"entry":{"command":["x"]},"contributions":contributions});
            // Without overlays even an empty plugin or hidden command remains valid.
            Manifest::parse_in_dir(&value.to_string(), &directory.0).unwrap();
            value["contributions"]["overlays"] = json!([overlay]);
            let result = Manifest::parse_in_dir(&value.to_string(), &directory.0);
            if accepted {
                result.unwrap();
            } else {
                assert_eq!(result.unwrap_err(), error, "{value}");
            }
        }
    }
}

#[test]
fn text_format_checks_value_before_kind_and_applies_to_list_cells() {
    use serde_json::json;
    let base = json!({"manifest_version":1,"id":"x","name":"X","version":"1",
        "engines":{"vizi":"1.0"},"entry":{"command":["x"]},"contributions":{
        "commands":[{"id":"c","title":"C"}],
        "panels":[{"id":"p","title":"P","root":{}}]}});
    for kind in ["label", "button", "image", "progress", "line_edit"] {
        for format in [
            None,
            Some(json!("plain")),
            Some(json!("rich")),
            Some(json!("html")),
            Some(json!(null)),
            Some(json!(true)),
            Some(json!(42)),
            Some(json!([])),
            Some(json!({})),
        ] {
            let mut widget = json!({"kind":kind,"id":"w","text":"<b>as written</b>"});
            if matches!(kind, "button" | "line_edit") {
                widget["command"] = json!("c");
            }
            if let Some(format) = &format {
                widget["text_format"] = format.clone();
            }
            for cell in [false, true] {
                let mut manifest = base.clone();
                manifest["contributions"]["panels"][0]["root"] = if cell {
                    json!({"kind":"list","id":"rows","row":[widget.clone()]})
                } else {
                    widget.clone()
                };
                let result = Manifest::parse(&manifest.to_string());
                match &format {
                    Some(v) if !matches!(v.as_str(), Some("plain" | "rich")) => {
                        assert_eq!(
                            result.unwrap_err(),
                            format!("{kind} 'w' text_format must be one of ['plain', 'rich']")
                        );
                    }
                    Some(_) if kind != "label" => {
                        assert_eq!(
                            result.unwrap_err(),
                            format!("{kind} 'w' does not take text_format; only a label does")
                        );
                    }
                    _ => {
                        result.unwrap();
                    }
                }
            }
        }
    }
}

#[test]
fn all_published_manifest_fixtures() {
    let directory = IconDirectory::new();
    let spec = include_str!("../../../docs/spec/manifest_schema.md");
    // Rust's established wording differs from the reference. Pin the complete
    // diagnostic for those cases; never let an unrelated rejection pass.
    let diagnostics: std::collections::BTreeMap<String, Vec<String>> =
        serde_json::from_str(include_str!("manifest_diagnostics.json")).unwrap();
    let mut tested = 0;
    for fixture in spec.split("\n<!-- fixture: ").skip(1) {
        if !fixture.starts_with("accepted") && !fixture.starts_with("rejected") {
            continue;
        }
        let (label, tail) = fixture.split_once("-->").unwrap();
        let json = tail
            .split_once("```json\n")
            .unwrap()
            .1
            .split_once("```")
            .unwrap()
            .0;
        let accepted = label.starts_with("accepted");
        let result = Manifest::parse_in_dir(json, &directory.0);
        assert_eq!(result.is_ok(), accepted, "{label}: {result:?}\n{json}");
        if !accepted {
            let expected = label
                .trim()
                .strip_prefix("rejected ")
                .unwrap()
                .trim_matches('"');
            let error = result.unwrap_err();
            if let Some(alternatives) = diagnostics.get(expected) {
                assert!(alternatives.contains(&error), "{label}: {error}");
            } else if expected == "extension must be non-empty"
                || expected == "entry.startup_s must be a positive number of seconds"
                || expected.starts_with("panel 'p''s lists may realise")
            {
                assert_eq!(error, expected, "{label}");
            } else {
                assert!(error.contains(expected), "{label}: {error}");
            }
            // These published overlay fixtures predate the starter rule.
            // Also check them with a starter so removing their own validation
            // cannot be masked by the later no-starter refusal.
            let mut value: serde_json::Value = serde_json::from_str(json).unwrap();
            if value["contributions"]["overlays"].is_array()
                && !expected.starts_with("overlays are drawn only")
            {
                value["contributions"]["panels"] = serde_json::json!([
                    {"id":"fixture-starter","title":"Starter","root":{"kind":"label","id":"label"}}
                ]);
                assert_eq!(
                    Manifest::parse_in_dir(&value.to_string(), &directory.0).unwrap_err(),
                    error,
                    "overlay rejection must not depend on the starter rule: {label}"
                );
            }
        }
        tested += 1;
    }
    assert_eq!(tested, 169, "published fixture count changed");
}

#[test]
fn startup_hint_is_optional_positive_and_not_limited_by_host_policy() {
    use serde_json::json;
    let mut value = json!({"manifest_version":1,"id":"x","name":"X","version":"1",
        "engines":{"vizi":"1.0"},"entry":{"command":["x"]}});
    Manifest::parse(&value.to_string()).unwrap();
    for seconds in [json!(null), json!(0.25), json!(600)] {
        value["entry"]["startup_s"] = seconds.clone();
        let parsed = Manifest::parse(&value.to_string()).unwrap();
        assert_eq!(parsed.value()["entry"]["startup_s"], seconds);
    }
    // The published fixtures cover zero, a numeric string and true.
    for seconds in [json!(-1), json!(false), json!([]), json!({})] {
        value["entry"]["startup_s"] = seconds;
        assert_eq!(
            Manifest::parse(&value.to_string()).unwrap_err(),
            "entry.startup_s must be a positive number of seconds"
        );
    }
}

#[test]
fn picker_rules_and_subscription_permissions() {
    let base = serde_json::json!({"manifest_version":1,"id":"x","name":"X","version":"1",
        "engines":{"vizi":"1.0"},"entry":{"command":["x"]},
        "contributions":{"commands":[{"id":"set","title":"Set"}],
        "panels":[{"id":"p","title":"P","root":{}}]}});
    for (kind, value, multiple, valid) in [
        ("channel_picker", serde_json::json!(0), None, true),
        ("channel_picker", serde_json::json!(-1), None, false),
        ("channel_picker", serde_json::json!(true), None, false),
        ("channel_picker", serde_json::json!(1.5), None, false),
        (
            "channel_picker",
            serde_json::json!([]),
            Some(serde_json::json!(true)),
            true,
        ),
        (
            "channel_picker",
            serde_json::json!([0, 2]),
            Some(serde_json::json!(true)),
            true,
        ),
        (
            "channel_picker",
            serde_json::json!([0, -1]),
            Some(serde_json::json!(true)),
            false,
        ),
        (
            "channel_picker",
            serde_json::json!(0),
            Some(serde_json::json!("yes")),
            false,
        ),
        ("color_picker", serde_json::json!("#aAbBcC"), None, true),
        ("color_picker", serde_json::json!("#12345678"), None, false),
        ("color_picker", serde_json::json!("#xyz123"), None, false),
        (
            "color_picker",
            serde_json::json!("#123456"),
            Some(serde_json::json!(false)),
            true,
        ),
        ("colormap_picker", serde_json::json!("custom"), None, true),
        ("roi_picker", serde_json::json!(""), None, true),
        ("roi_picker", serde_json::json!(1), None, false),
    ] {
        let mut manifest = base.clone();
        let mut widget =
            serde_json::json!({"id":"pick", "kind":kind,"command":"set","value":value});
        if let Some(multiple) = multiple {
            widget["multiple"] = multiple;
        }
        manifest["contributions"]["panels"][0]["root"] = widget;
        assert_eq!(
            Manifest::parse(&manifest.to_string()).is_ok(),
            valid,
            "{manifest}"
        );
        manifest["contributions"]["panels"][0]["root"]["command"] = serde_json::json!("missing");
        assert!(Manifest::parse(&manifest.to_string()).is_err());
    }
    for allowed in [false, true] {
        let mut manifest = base.clone();
        manifest["contributions"]["panels"][0]["root"] =
            serde_json::json!({"kind":"label","id":"status"});
        manifest["subscriptions"] = serde_json::json!(["epochs", "view", "regions"]);
        if allowed {
            manifest["permissions"] = serde_json::json!(["data_access"]);
        }
        let manifest = Manifest::parse(&manifest.to_string()).unwrap();
        assert_eq!(manifest.warnings.len(), if allowed { 0 } else { 2 });
        assert!(manifest.warnings.iter().all(|w| w.contains("data_access")));
    }
}

#[test]
fn protocol_floor_and_unknown_permissions() {
    let m=Manifest::parse(r#"{"manifest_version":1,"id":"x","name":"X","version":"1","engines":{"vizi":"^1.2"},"entry":{"command":["plugin"]},"permissions":["future"]}"#).unwrap();
    assert!(!m.supports_host(1, 1));
    assert!(m.supports_host(1, 2));
    assert!(m.supports_host(1, 99));
    assert!(!m.supports_host(2, 0));
    assert_eq!(m.warnings.len(), 1);
}

#[test]
fn bundled_idl_matches_the_published_specification() {
    assert_eq!(
        include_bytes!("../proto/vizi_plugin.proto").as_slice(),
        include_bytes!("../../../proto/vizi_plugin.proto").as_slice()
    );
    assert_eq!(
        include_bytes!("../proto/roi.proto").as_slice(),
        include_bytes!("../../../proto/roi.proto").as_slice()
    );
}

struct IconDirectory(std::path::PathBuf);
impl IconDirectory {
    fn new() -> Self {
        static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
        let path = std::env::temp_dir().join(format!(
            "vizi-manifest-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
        ));
        std::fs::create_dir(&path).unwrap();
        std::fs::create_dir(path.join("icons")).unwrap();
        std::fs::write(
            path.join("icons/tool.svg"),
            b"<svg xmlns=\"http://www.w3.org/2000/svg\"/>",
        )
        .unwrap();
        // The manifest checks the signature; rendering belongs to the host.
        std::fs::write(path.join("icons/go.png"), b"\x89PNG\r\n\x1a\n").unwrap();
        std::fs::write(path.join("icons/big.png"), vec![0; 65537]).unwrap();
        for name in ["fake.png", "fake.svg"] {
            std::fs::write(path.join("icons").join(name), b"not an image").unwrap();
        }
        Self(path)
    }
}
impl Drop for IconDirectory {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.0).unwrap();
    }
}

#[test]
fn pane_icons_resolve_relative_to_manifest_and_modal_fields_are_validated() {
    let directory = IconDirectory::new();
    let mut value = serde_json::json!({
        "manifest_version": 1, "id":"tool.test", "name":"Tool", "version":"1",
        "engines":{"vizi":"1.0"}, "entry":{"command":["plugin"]},
        "contributions":{"tools":[{"id":"tool", "pane":{"icon":"icons/tool.svg"},
            "triggers":[{"key":"Ctrl+Shift+T"}],
            "actions":[{"id":"add", "key":"N"}]}]}
    });
    let file = directory.0.join("vizi-plugin.json");
    std::fs::write(&file, value.to_string()).unwrap();
    Manifest::load(&file).unwrap();
    value["contributions"]["tools"][0]["actions"][0]["key"] = "shift+ctrl+t".into();
    assert!(Manifest::parse_in_dir(&value.to_string(), &directory.0)
        .unwrap_err()
        .contains("triggers"));
    value["contributions"]["tools"][0]["actions"][0]["key"] = "N".into();
    value["contributions"]["tools"][0]["triggers"] = serde_json::json!([
        {"button":"left", "modifiers":["ctrl", "shift"]},
        {"button":"left", "modifiers":["shift", "ctrl"]}
    ]);
    assert!(Manifest::parse_in_dir(&value.to_string(), &directory.0)
        .unwrap_err()
        .contains("duplicate trigger"));
    value["contributions"]["tools"][0]["triggers"] = serde_json::json!([{ "button":"right" }]);
    // Chord conflicts depend on the host's views and are resolved there.
    Manifest::parse_in_dir(&value.to_string(), &directory.0).unwrap();
}

#[test]
fn subscriptions_are_string_lists_with_unknown_names_warned_and_ignored() {
    let mut value = serde_json::json!({
        "manifest_version":1, "id":"x", "name":"X", "version":"1",
        "engines":{"vizi":"1.0"}, "entry":{"command":["plugin"]}, "subscriptions":["epochs", "future"]
    });
    let m = Manifest::parse(&value.to_string()).unwrap();
    assert_eq!(m.warnings.len(), 1);
    assert!(m.warnings[0].contains("future"));
    for invalid in [serde_json::json!("epochs"), serde_json::json!([1])] {
        value["subscriptions"] = invalid;
        assert!(Manifest::parse(&value.to_string()).is_err());
    }
}

#[test]
fn processing_stage_manifest_entries() {
    for (stages, valid) in [
        (serde_json::json!([{"id":"filter"}]), true),
        (serde_json::json!([{"id":"filter", "name":"Filter"}]), true),
        (serde_json::json!([{"id":"filter"},{"id":"other"}]), true),
        (serde_json::json!([{"id":"filter"},{"id":"filter"}]), false),
        (serde_json::json!([{"id":"  "}]), false),
        (serde_json::json!([{"name":"Missing id"}]), false),
        (serde_json::json!([{"id":3}]), false),
        (serde_json::json!([{"id":"filter", "name":3}]), false),
        (serde_json::json!({"id":"filter"}), false),
    ] {
        let manifest = serde_json::json!({"manifest_version":1,"id":"stage.test","name":"Test","version":"1","engines":{"vizi":"1.0"},"entry":{"command":["plugin"]},"contributions":{"stages":stages}});
        assert_eq!(
            Manifest::parse(&manifest.to_string()).is_ok(),
            valid,
            "{manifest}"
        );
    }
}

#[test]
fn list_rules_and_structural_capacity() {
    let base = serde_json::json!({"manifest_version":1,"id":"x","name":"X","version":"1",
        "engines":{"vizi":"1.0"},"entry":{"command":["x"]},
        "contributions":{"commands":[{"id":"set","title":"Set"}],
        "panels":[{"id":"p","title":"P","root":{"kind":"list","id":"rows",
            "row":[{"kind":"label","id":"cell"}]}}]}});
    let parse = |v: &serde_json::Value| Manifest::parse(&v.to_string());
    assert_eq!(parse(&base).unwrap().widget_count("p"), Some(257));
    assert_eq!(parse(&base).unwrap().widget_count("missing"), None);
    for field in ["max_rows", "visible_rows"] {
        for value in [
            serde_json::json!(0),
            serde_json::json!(1025),
            serde_json::json!(-1),
            serde_json::json!(true),
            serde_json::json!(1.0),
            serde_json::json!("8"),
            serde_json::Value::Null,
        ] {
            let mut v = base.clone();
            v["contributions"]["panels"][0]["root"][field] = value;
            assert!(parse(&v).is_err(), "{v}");
        }
        for n in [1, 1024] {
            let mut v = base.clone();
            v["contributions"]["panels"][0]["root"][field] = serde_json::json!(n);
            assert!(parse(&v).is_ok());
        }
    }
    for row in [
        serde_json::json!([]),
        serde_json::json!(null),
        serde_json::json!([{"kind":"label","id":"rows"}]),
        serde_json::json!([{"kind":"label","id":"cell"},{"kind":"image","id":"cell"}]),
        serde_json::json!([{"kind":"column","id":"col","children":[{"kind":"image","id":"img"}]}]),
        serde_json::json!([{"kind":"list","id":"nested","row":[{"kind":"image","id":"img"}]}]),
    ] {
        let mut v = base.clone();
        v["contributions"]["panels"][0]["root"]["row"] = row;
        assert!(parse(&v).is_err(), "{v}");
    }
    for (selectable, command, valid) in [
        (true, None, false),
        (true, Some("missing"), false),
        (true, Some("set"), true),
        (false, Some("set"), false),
    ] {
        let mut v = base.clone();
        let root = &mut v["contributions"]["panels"][0]["root"];
        root["selectable"] = serde_json::json!(selectable);
        if let Some(command) = command {
            root["command"] = serde_json::json!(command);
        }
        assert_eq!(parse(&v).is_ok(), valid);
    }
    let mut v = base.clone();
    v["contributions"]["panels"][0]["root"]["row"][0]["selectable"] = serde_json::json!(false);
    assert!(parse(&v).is_ok());
    let cells: Vec<_> = (0..255)
        .map(|n| serde_json::json!({"kind":"image","id":format!("cell{n}")}))
        .collect();
    v = base;
    v["contributions"]["panels"][0]["root"]["max_rows"] = serde_json::json!(1024);
    v["contributions"]["panels"][0]["root"]["row"] = serde_json::json!(cells);
    assert!(parse(&v)
        .unwrap_err()
        .contains("may realise at most 4096 cells"));
    v["contributions"]["panels"][0]["root"]["max_rows"] = serde_json::json!(16);
    assert_eq!(parse(&v).unwrap().widget_count("p"), Some(1 + 255 * 16));
    v["contributions"]["panels"][0]["root"]["row"]
        .as_array_mut()
        .unwrap()
        .push(serde_json::json!({"kind":"label","id":"excess"}));
    assert!(parse(&v).is_err());
}

#[test]
fn set_view_manifest_rules() {
    let base = serde_json::json!({"manifest_version":1,"id":"x","name":"X","version":"1",
        "engines":{"vizi":"1.0"},"entry":{"command":["x"]},
        "contributions":{"commands":[{"id":"set","title":"Set"}],
        "panels":[{"id":"p","title":"P","view_targets":["t"],"root":
            {"kind":"button","id":"go","command":"set","moves_view":true}}],
        "tools":[{"id":"tool"}]}});
    let parse = |v: &serde_json::Value| Manifest::parse(&v.to_string());
    for targets in [
        serde_json::json!(["t", "t"]),
        serde_json::json!(["camera"]),
        serde_json::json!([3]),
        serde_json::json!(null),
        serde_json::json!("z"),
    ] {
        for kind in ["panels", "tools"] {
            let mut v = base.clone();
            v["contributions"][kind][0]["view_targets"] = targets.clone();
            assert!(parse(&v).is_err(), "{v}");
        }
    }
    for widget in [
        serde_json::json!({"kind":"label","id":"x","moves_view":true}),
        serde_json::json!({"kind":"image","id":"x","moves_view":true}),
        serde_json::json!({"kind":"image","id":"x","command":"missing"}),
        serde_json::json!({"kind":"button","id":"x","command":"set","moves_view":"yes"}),
        serde_json::json!({"kind":"button","id":"x","command":"set","follows_selection":true}),
        serde_json::json!({"kind":"image","id":"x","filter":"all"}),
        serde_json::json!({"kind":"roi_picker","id":"x","command":"set","follows_selection":1}),
        serde_json::json!({"kind":"list","id":"x","moves_view":true,"row":[{"kind":"label","id":"cell"}]}),
    ] {
        let mut v = base.clone();
        v["contributions"]["panels"][0]["root"] = widget;
        assert!(parse(&v).is_err(), "{v}");
    }
    // A moving cell inside a nonselectable list still requires panel targets.
    let mut v = base;
    let cell = v["contributions"]["panels"][0]["root"].clone();
    v["contributions"]["panels"][0]["root"] =
        serde_json::json!({"kind":"list","id":"rows","row":[cell]});
    assert!(parse(&v).is_ok());
    v["contributions"]["panels"][0]["view_targets"] = serde_json::json!([]);
    assert!(parse(&v).is_err());
}

#[test]
fn navigating_tools_need_a_panel_switch_but_not_matching_targets() {
    let base = serde_json::json!({"manifest_version":1,"id":"x","name":"X","version":"1",
        "engines":{"vizi":"1.0"},"entry":{"command":["x"]},
        "contributions":{"tools":[{"id":"pick","view_targets":["look_at"]}]}});
    let error = Manifest::parse(&base.to_string()).unwrap_err();
    assert_eq!(error, "tool 'pick' declares view_targets but no panel does; the move-the-view row lives on the plugin's panel");
    for targets in [
        None,
        Some(serde_json::json!([])),
        Some(serde_json::json!(["t"])),
    ] {
        let mut v = base.clone();
        v["contributions"]["panels"] =
            serde_json::json!([{"id":"p","title":"P","root":{"kind":"label","id":"label"}}]);
        if let Some(targets) = &targets {
            v["contributions"]["panels"][0]["view_targets"] = targets.clone();
        }
        assert_eq!(
            Manifest::parse(&v.to_string()).is_ok(),
            targets == Some(serde_json::json!(["t"]))
        );
    }
    let mut v = base;
    v["contributions"]["tools"][0]["view_targets"] = serde_json::json!([]);
    assert!(Manifest::parse(&v.to_string()).is_ok());
}

#[test]
fn follow_target_rules() {
    let base = serde_json::json!({"manifest_version":1,"id":"x","name":"X","version":"1",
        "engines":{"vizi":"1.0"},"entry":{"command":["x"]},
        "contributions":{"panels":[{"id":"p","title":"P","root":{"kind":"label","id":"l"}}]}});
    for (targets, valid) in [
        (serde_json::json!([]), true),
        (
            serde_json::json!([
                "t",
                "z",
                "select_region",
                "frame_region",
                "look_at",
                "orbit_point"
            ]),
            true,
        ),
        (serde_json::json!(["t", "t"]), false),
        (serde_json::json!(["camera"]), false),
        (serde_json::json!([1]), false),
        (serde_json::json!("t"), false),
        (serde_json::json!(null), false),
    ] {
        let mut value = base.clone();
        value["contributions"]["panels"][0]["follow_targets"] = targets;
        assert_eq!(
            Manifest::parse(&value.to_string()).is_ok(),
            valid,
            "{value}"
        );
    }
}

#[test]
fn widget_boolean_flags_reject_true_on_wrong_kinds_but_accept_false() {
    use serde_json::json;
    let base = json!({"manifest_version":1,"id":"x","name":"X","version":"1",
        "engines":{"vizi":"1.0"},"entry":{"command":["x"]},
        "contributions":{"commands":[{"id":"set","title":"Set"}],
        "panels":[{"id":"p","title":"P","root":{}}]}});
    for flag in ["selectable", "multiple", "follows_selection", "moves_view"] {
        for cell in [false, true] {
            for value in [
                json!(false),
                json!(true),
                json!(null),
                json!("false"),
                json!(0),
            ] {
                let mut widget = json!({"kind":"label","id":"label"});
                widget[flag] = value.clone();
                let mut manifest = base.clone();
                manifest["contributions"]["panels"][0]["root"] = if cell {
                    json!({"kind":"list","id":"rows","row":[widget]})
                } else {
                    widget
                };
                assert_eq!(
                    Manifest::parse(&manifest.to_string()).is_ok(),
                    value == json!(false),
                    "{manifest}"
                );
            }
        }
    }
    for value in [json!(0), json!(4), json!([]), json!([0, 4])] {
        let mut manifest = base.clone();
        manifest["contributions"]["panels"][0]["root"] = json!({
            "kind":"channel_picker","id":"pick","command":"set","multiple":true,"value":value
        });
        Manifest::parse(&manifest.to_string()).unwrap();
    }
}

#[test]
fn list_cell_limit_and_malformed_menu_or_cell_kind() {
    use serde_json::json;
    let base = json!({"manifest_version":1,"id":"x","name":"X","version":"1",
        "engines":{"vizi":"1.0"},"entry":{"command":["x"]},"contributions":{
        "commands":[{"id":"c","title":"C"}],
        "panels":[{"id":"p","title":"P","root":{"kind":"list","id":"cells","max_rows":1024,
            "row":[{"kind":"label","id":"a"},{"kind":"label","id":"b"},
                   {"kind":"label","id":"c"},{"kind":"label","id":"d"}]}}]}});
    assert_eq!(
        Manifest::parse(&base.to_string())
            .unwrap()
            .widget_count("p"),
        Some(4097)
    );
    let mut excess = base.clone();
    excess["contributions"]["panels"][0]["root"]["row"]
        .as_array_mut()
        .unwrap()
        .push(json!({"kind":"label","id":"e"}));
    assert_eq!(
        Manifest::parse(&excess.to_string()).unwrap_err(),
        "list 'cells' may realise at most 4096 cells (max_rows × row cells), not 1024 × 5"
    );
    for invalid in [json!([]), json!({}), json!(true), json!(42)] {
        let mut value = base.clone();
        value["contributions"]["commands"][0]["menu"] = invalid.clone();
        assert!(Manifest::parse(&value.to_string())
            .unwrap_err()
            .contains("menu"));
        value = base.clone();
        value["contributions"]["panels"][0]["root"]["row"][0]["kind"] = invalid;
        assert_eq!(
            Manifest::parse(&value.to_string()).unwrap_err(),
            "kind must be a string"
        );
    }
}

#[test]
fn all_lists_in_one_panel_share_the_cell_budget() {
    use serde_json::json;
    fn list(id: &str, rows: usize) -> serde_json::Value {
        json!({"kind":"list", "id":id, "max_rows": rows,
            "row": [{"kind":"label","id":format!("{id}-a")},
                    {"kind":"label","id":format!("{id}-b")},
                    {"kind":"label","id":format!("{id}-c")} ]})
    }
    for (first, second, extra, accepted) in [(1024, 341, 1, true), (1024, 341, 2, false)] {
        let mut m = json!({"manifest_version":1,"id":"x","name":"X","version":"1", "entry":{"command":["x"]},"engines":{"vizi":"1.0"},
            "contributions":{"panels":[{"id":"p","title":"P","root":{"kind":"column","id":"root","children":[list("a",first),list("b",second),{"kind":"list","id":"extra","max_rows":extra,"row":[{"kind":"label","id":"extra-cell"}]}]}}]}});
        let result = Manifest::parse(&m.to_string());
        assert_eq!(result.is_ok(), accepted);
        if !accepted {
            assert_eq!(
                result.unwrap_err(),
                "panel 'p''s lists may realise at most 4096 cells together (each list's max_rows × row cells), not 4097"
            );
        }
        // Independent panels have independent budgets.
        m["contributions"]["panels"][0]["root"]["children"] = json!([list("a", first)]);
        let mut other = m["contributions"]["panels"][0].clone();
        other["id"] = json!("q");
        m["contributions"]["panels"]
            .as_array_mut()
            .unwrap()
            .push(other);
        Manifest::parse(&m.to_string()).unwrap();
    }
}
