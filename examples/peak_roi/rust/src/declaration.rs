//! The executable declares its interface here; vizi-plugin.json is a development snapshot.
use vizi::declare::*;

pub const ID: &str = "org.example.peak_roi";

pub fn builder() -> PluginBuilder {
    let mut plugin = PluginBuilder::new(ID, "Peak ROI", env!("CARGO_PKG_VERSION"));
    plugin.permission(Permission::DataAccess);
    plugin.permission(Permission::ContributeRois);
    plugin
}

pub fn commands() -> [CommandSpec; 5] {
    [
        CommandSpec {
            id: "peak.set_z".into(),
            title: "Set Z extent".into(),
            menu: None,
            description: None,
            default_keybinding: None,
            pane: None,
        },
        CommandSpec {
            id: "peak.set_y".into(),
            title: "Set Y extent".into(),
            menu: None,
            description: None,
            default_keybinding: None,
            pane: None,
        },
        CommandSpec {
            id: "peak.set_x".into(),
            title: "Set X extent".into(),
            menu: None,
            description: None,
            default_keybinding: None,
            pane: None,
        },
        CommandSpec {
            id: "peak.set_mode".into(),
            title: "Set timepoint mode".into(),
            menu: None,
            description: None,
            default_keybinding: None,
            pane: None,
        },
        CommandSpec {
            id: "peak.run".into(),
            title: "Find peak".into(),
            menu: None,
            description: None,
            default_keybinding: None,
            pane: None,
        },
    ]
}

pub fn panels(
    plugin: &mut PluginBuilder,
    [peak_set_z, peak_set_y, peak_set_x, peak_set_mode, peak_run]: [CommandRef; 5],
) {
    plugin.panel(PanelSpec {
        id: "peak".into(),
        title: "Peak ROI".into(),
        menu_label: None,
        view_targets: vec![],
        follow_targets: vec![],
        root: Widget::Column {
            id: "root".into(),
            children: vec![
                Widget::Row {
                    id: "rz".into(),
                    children: vec![
                        Widget::Label {
                            id: "lz".into(),
                            text: "Z extent (voxels)".into(),
                        },
                        Widget::LineEdit {
                            id: "ez".into(),
                            moves_view: false,
                            command: peak_set_z.clone(),
                            value: Some("4".into()),
                            placeholder: None,
                        },
                    ],
                },
                Widget::Row {
                    id: "ry".into(),
                    children: vec![
                        Widget::Label {
                            id: "ly".into(),
                            text: "Y extent (voxels)".into(),
                        },
                        Widget::LineEdit {
                            id: "ey".into(),
                            moves_view: false,
                            command: peak_set_y.clone(),
                            value: Some("8".into()),
                            placeholder: None,
                        },
                    ],
                },
                Widget::Row {
                    id: "rx".into(),
                    children: vec![
                        Widget::Label {
                            id: "lx".into(),
                            text: "X extent (voxels)".into(),
                        },
                        Widget::LineEdit {
                            id: "ex".into(),
                            moves_view: false,
                            command: peak_set_x.clone(),
                            value: Some("8".into()),
                            placeholder: None,
                        },
                    ],
                },
                Widget::Dropdown {
                    id: "mode".into(),
                    moves_view: false,
                    command: peak_set_mode.clone(),
                    value: Some("Current timepoint".into()),
                    options: vec!["Current timepoint".into(), "All timepoints".into()],
                },
                Widget::Button {
                    id: "go".into(),
                    moves_view: false,
                    command: peak_run.clone(),
                    text: "Find peak".into(),
                },
                Widget::Label {
                    id: "status".into(),
                    text: "Ready".into(),
                },
            ],
        },
    });
}
