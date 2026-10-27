//! The executable declares its interface here; vizi-plugin.json is a development snapshot.
use vizi::declare::*;

pub const ID: &str = "org.example.interactive_seg";

pub fn builder() -> PluginBuilder {
    let mut plugin = PluginBuilder::new(ID, "Interactive Segmentation", env!("CARGO_PKG_VERSION"));
    plugin.permission(Permission::DataAccess);
    plugin.permission(Permission::ContributeRois);
    plugin.overlay(OverlaySpec {
        id: "seg.trail".into(),
        name: "Lasso trail".into(),
        segment_mib: 32,
    });
    plugin.overlay(OverlaySpec {
        id: "seg.labels".into(),
        name: "Segmentation".into(),
        segment_mib: 64,
    });
    plugin.tool(ToolSpec {
        id: "seg.lasso".into(),
        name: "Lasso segment".into(),
        views: vec![View::D2],
        view_targets: vec![],
        menu: Some(Menu::Tools),
        mode: ToolMode::Gesture,
        cancel_on_camera_move: true,
        wants_pick: true,
        triggers: vec![],
        actions: vec![],
        pane: None,
    });
    plugin
}

pub fn commands() -> [CommandSpec; 9] {
    [
        CommandSpec {
            id: "seg.set_min_size".into(),
            title: "Set minimum size".into(),
            menu: None,
            description: None,
            default_keybinding: None,
            pane: None,
        },
        CommandSpec {
            id: "seg.set_threshold".into(),
            title: "Set threshold scale".into(),
            menu: None,
            description: None,
            default_keybinding: None,
            pane: None,
        },
        CommandSpec {
            id: "seg.set_3d".into(),
            title: "Set 3D growth".into(),
            menu: None,
            description: None,
            default_keybinding: None,
            pane: None,
        },
        CommandSpec {
            id: "seg.set_mode".into(),
            title: "Set accumulate mode".into(),
            menu: None,
            description: None,
            default_keybinding: None,
            pane: None,
        },
        CommandSpec {
            id: "seg.set_region".into(),
            title: "Set measured region".into(),
            menu: None,
            description: None,
            default_keybinding: None,
            pane: None,
        },
        CommandSpec {
            id: "seg.pick".into(),
            title: "Activate lasso".into(),
            menu: None,
            description: None,
            default_keybinding: None,
            pane: None,
        },
        CommandSpec {
            id: "seg.commit".into(),
            title: "Commit to annotations".into(),
            menu: None,
            description: None,
            default_keybinding: None,
            pane: None,
        },
        CommandSpec {
            id: "seg.measure".into(),
            title: "Measure over time".into(),
            menu: None,
            description: None,
            default_keybinding: None,
            pane: None,
        },
        CommandSpec {
            id: "seg.reset".into(),
            title: "Clear segmentation".into(),
            menu: None,
            description: None,
            default_keybinding: None,
            pane: None,
        },
    ]
}

pub fn panels(
    plugin: &mut PluginBuilder,
    [seg_set_min_size, seg_set_threshold, seg_set_3d, seg_set_mode, seg_set_region, seg_pick, seg_commit, seg_measure, seg_reset]: [CommandRef; 9],
) {
    plugin.panel(PanelSpec {
        id: "seg".into(),
        title: "Interactive Segmentation".into(),
        menu_label: None,
        view_targets: vec![],
        follow_targets: vec![],
        root: Widget::Column {
            id: "root".into(),
            children: vec![
                Widget::Button {
                    id: "pick".into(),
                    moves_view: false,
                    command: seg_pick.clone(),
                    text: "Select region…".into(),
                },
                Widget::Row {
                    id: "rsize".into(),
                    children: vec![
                        Widget::Label {
                            id: "lsize".into(),
                            text: "Minimum size (voxels)".into(),
                        },
                        Widget::Slider {
                            id: "size".into(),
                            moves_view: false,
                            command: seg_set_min_size.clone(),
                            value: Some(50.0),
                            min: 1.0,
                            max: 5000.0,
                            step: Some(10.0),
                            float: false,
                        },
                    ],
                },
                Widget::Row {
                    id: "rthresh".into(),
                    children: vec![
                        Widget::Label {
                            id: "lthresh".into(),
                            text: "Threshold ×".into(),
                        },
                        Widget::Slider {
                            id: "thresh".into(),
                            moves_view: false,
                            command: seg_set_threshold.clone(),
                            value: Some(1.0),
                            min: 0.5,
                            max: 2.0,
                            step: Some(0.05),
                            float: true,
                        },
                    ],
                },
                Widget::Checkbox {
                    id: "grow3d".into(),
                    moves_view: false,
                    command: seg_set_3d.clone(),
                    text: "Grow through Z".into(),
                    value: Some(true),
                },
                Widget::Dropdown {
                    id: "mode".into(),
                    moves_view: false,
                    command: seg_set_mode.clone(),
                    value: Some("Replace".into()),
                    options: vec!["Replace".into(), "Add".into()],
                },
                Widget::Button {
                    id: "commit".into(),
                    moves_view: false,
                    command: seg_commit.clone(),
                    text: "Commit to annotations".into(),
                },
                Widget::Row {
                    id: "rmeasure".into(),
                    children: vec![
                        Widget::Dropdown {
                            id: "region".into(),
                            moves_view: false,
                            command: seg_set_region.clone(),
                            value: Some("(none)".into()),
                            options: vec!["(none)".into()],
                        },
                        Widget::Button {
                            id: "measure".into(),
                            moves_view: false,
                            command: seg_measure.clone(),
                            text: "Measure".into(),
                        },
                    ],
                },
                Widget::Image {
                    id: "plot".into(),
                    moves_view: false,
                    command: None,
                },
                Widget::Button {
                    id: "reset".into(),
                    moves_view: false,
                    command: seg_reset.clone(),
                    text: "Clear".into(),
                },
                Widget::Label {
                    id: "status".into(),
                    text: "Ready".into(),
                },
            ],
        },
    });
}
