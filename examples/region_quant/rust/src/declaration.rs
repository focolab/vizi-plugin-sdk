//! The executable declares its interface here; vizi-plugin.json is a development snapshot.
use vizi::declare::*;

pub const ID: &str = "org.example.region_quant";

pub fn builder() -> PluginBuilder {
    let mut plugin = PluginBuilder::new(ID, "Region Quant", env!("CARGO_PKG_VERSION"));
    plugin.permission(Permission::DataAccess);
    plugin.subscription(Subscription::View);
    plugin.subscription(Subscription::Regions);
    plugin
}

pub fn commands() -> [CommandSpec; 4] {
    [
        CommandSpec {
            id: "quant.channel".into(),
            title: "Measure a channel".into(),
            menu: None,
            description: None,
            default_keybinding: None,
            pane: None,
        },
        CommandSpec {
            id: "quant.pick".into(),
            title: "Select a region".into(),
            menu: None,
            description: None,
            default_keybinding: None,
            pane: None,
        },
        CommandSpec {
            id: "quant.brightest".into(),
            title: "Go to the brightest frame".into(),
            menu: None,
            description: None,
            default_keybinding: None,
            pane: None,
        },
        CommandSpec {
            id: "quant.export".into(),
            title: "Export region means".into(),
            menu: None,
            description: None,
            default_keybinding: None,
            pane: None,
        },
    ]
}

pub fn panels(
    plugin: &mut PluginBuilder,
    [quant_channel, quant_pick, quant_brightest, quant_export]: [CommandRef; 4],
) {
    plugin.panel(PanelSpec {
        id: "quant".into(),
        title: "Region Quant".into(),
        menu_label: None,
        view_targets: vec![ViewTarget::T, ViewTarget::SelectRegion],
        follow_targets: vec![ViewTarget::FrameRegion],
        root: Widget::Column {
            id: "root".into(),
            children: vec![
                Widget::ChannelPicker {
                    id: "channel".into(),
                    moves_view: false,
                    command: quant_channel.clone(),
                    value: Some(ChannelSelection::Single(0)),
                    multiple: false,
                },
                Widget::List {
                    id: "regions".into(),
                    moves_view: true,
                    command: Some(quant_pick.clone()),
                    row: vec![
                        Widget::Label {
                            id: "name".into(),
                            text: "".into(),
                        },
                        Widget::Label {
                            id: "voxels".into(),
                            text: "".into(),
                        },
                        Widget::Label {
                            id: "mean".into(),
                            text: "".into(),
                        },
                        Widget::Image {
                            id: "swatch".into(),
                            moves_view: false,
                            command: None,
                        },
                    ],
                    max_rows: 1024,
                    visible_rows: 8,
                    selectable: true,
                },
                Widget::Button {
                    id: "brightest".into(),
                    moves_view: true,
                    command: quant_brightest.clone(),
                    text: "Brightest frame".into(),
                },
                Widget::Button {
                    id: "export".into(),
                    moves_view: false,
                    command: quant_export.clone(),
                    text: "Export CSV…".into(),
                },
                Widget::Label {
                    id: "tracing".into(),
                    text: "".into(),
                },
                Widget::Label {
                    id: "status".into(),
                    text: "Ready".into(),
                },
            ],
        },
    });
}
