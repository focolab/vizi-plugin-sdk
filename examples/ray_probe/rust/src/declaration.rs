//! The executable declares its interface here; vizi-plugin.json is a development snapshot.
use vizi::declare::*;

pub const ID: &str = "org.example.ray_probe";

pub fn builder() -> PluginBuilder {
    let mut plugin = PluginBuilder::new(ID, "Ray probe", env!("CARGO_PKG_VERSION"));
    plugin.permission(Permission::DataAccess);
    plugin.permission(Permission::ContributeRois);
    plugin.overlay(OverlaySpec {
        id: "probe.picks".into(),
        name: "Picks".into(),
        segment_mib: 4,
    });
    plugin.overlay(OverlaySpec {
        id: "probe.aim".into(),
        name: "Aim".into(),
        segment_mib: 4,
    });
    plugin.tool(ToolSpec {
        id: "probe.point".into(),
        name: "Probe".into(),
        views: vec![View::D2, View::D3],
        view_targets: vec![],
        menu: Some(Menu::Tools),
        mode: ToolMode::Gesture,
        cancel_on_camera_move: false,
        wants_pick: true,
        triggers: vec![
            Trigger::Key {
                key: "Shift+P".into(),
                hold: Hold::Toggle,
            },
            Trigger::Mouse {
                button: MouseButton::Middle,
                modifiers: vec![],
            },
        ],
        actions: vec![
            ActionSpec {
                id: "undo".into(),
                name: "Undo pick".into(),
                key: Some("U".into()),
                pane: Some(PaneButton {
                    icon: "icons/undo.svg".into(),
                    tooltip: Some("Undo the last pick".into()),
                    hold: None,
                }),
            },
            ActionSpec {
                id: "clear".into(),
                name: "Clear picks".into(),
                key: Some("X".into()),
                pane: Some(PaneButton {
                    icon: "icons/clear.svg".into(),
                    tooltip: Some("Clear every pick".into()),
                    hold: None,
                }),
            },
        ],
        pane: Some(PaneButton {
            icon: "icons/probe.svg".into(),
            tooltip: Some("Probe: the brightest voxel under the pointer".into()),
            hold: Some(Hold::Toggle),
        }),
    });
    plugin.icon("icons/probe.svg", include_bytes!("../icons/probe.svg"));
    plugin.icon("icons/undo.svg", include_bytes!("../icons/undo.svg"));
    plugin.icon("icons/clear.svg", include_bytes!("../icons/clear.svg"));
    plugin
}

pub fn commands() -> [CommandSpec; 5] {
    [
        CommandSpec {
            id: "probe.set_radius".into(),
            title: "Set ROI radius".into(),
            menu: None,
            description: None,
            default_keybinding: None,
            pane: None,
        },
        CommandSpec {
            id: "probe.set_refine".into(),
            title: "Refine at level 0".into(),
            menu: None,
            description: None,
            default_keybinding: None,
            pane: None,
        },
        CommandSpec {
            id: "probe.undo".into(),
            title: "Undo pick".into(),
            menu: None,
            description: None,
            default_keybinding: None,
            pane: None,
        },
        CommandSpec {
            id: "probe.clear".into(),
            title: "Clear picks".into(),
            menu: None,
            description: None,
            default_keybinding: None,
            pane: None,
        },
        CommandSpec {
            id: "probe.contribute".into(),
            title: "Contribute picks as ROIs".into(),
            menu: Some(Menu::Tools),
            description: None,
            default_keybinding: None,
            pane: None,
        },
    ]
}

pub fn panels(
    plugin: &mut PluginBuilder,
    [probe_set_radius, probe_set_refine, probe_undo, probe_clear, probe_contribute]: [CommandRef;
        5],
) {
    plugin.panel(PanelSpec {
        id: "probe".into(),
        title: "Ray probe".into(),
        menu_label: None,
        view_targets: vec![],
        follow_targets: vec![],
        root: Widget::Column {
            id: "root".into(),
            children: vec![
                Widget::Label {
                    id: "status".into(),
                    text: "Ready".into(),
                },
                Widget::Row {
                    id: "rradius".into(),
                    children: vec![
                        Widget::Label {
                            id: "lradius".into(),
                            text: "ROI radius (voxels)".into(),
                        },
                        Widget::LineEdit {
                            id: "radius".into(),
                            moves_view: false,
                            command: probe_set_radius.clone(),
                            value: Some("3".into()),
                            placeholder: None,
                        },
                    ],
                },
                Widget::Checkbox {
                    id: "refine".into(),
                    moves_view: false,
                    command: probe_set_refine.clone(),
                    text: "Refine at level 0".into(),
                    value: Some(true),
                },
                Widget::Row {
                    id: "rbuttons".into(),
                    children: vec![
                        Widget::Button {
                            id: "undo".into(),
                            moves_view: false,
                            command: probe_undo.clone(),
                            text: "Undo".into(),
                        },
                        Widget::Button {
                            id: "clear".into(),
                            moves_view: false,
                            command: probe_clear.clone(),
                            text: "Clear".into(),
                        },
                        Widget::Button {
                            id: "contribute".into(),
                            moves_view: false,
                            command: probe_contribute.clone(),
                            text: "Contribute".into(),
                        },
                    ],
                },
                Widget::Label {
                    id: "count".into(),
                    text: "No picks".into(),
                },
            ],
        },
    });
    plugin.panel(PanelSpec {
        id: "profile".into(),
        title: "Ray profile".into(),
        menu_label: None,
        view_targets: vec![],
        follow_targets: vec![],
        root: Widget::Column {
            id: "root".into(),
            children: vec![
                Widget::Image {
                    id: "plot".into(),
                    moves_view: false,
                    command: None,
                },
                Widget::Label {
                    id: "pick".into(),
                    text: "No pick yet".into(),
                },
            ],
        },
    });
}
