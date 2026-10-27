//! The executable declares its interface here; vizi-plugin.json is a development snapshot.
use vizi::declare::*;

pub const ID: &str = "org.example.movie_seg";

pub fn builder() -> PluginBuilder {
    let mut plugin = PluginBuilder::new(ID, "Movie seg", env!("CARGO_PKG_VERSION"));
    plugin.permission(Permission::DataAccess);
    plugin
}

pub fn commands() -> [CommandSpec; 5] {
    [
        ("threshold", "Set threshold"),
        ("band_visible", "Show or hide a band"),
        ("band_1", "Show band 1 (dim)"),
        ("band_2", "Show band 2 (mid)"),
        ("band_3", "Show band 3 (bright)"),
    ]
    .map(|(id, title)| CommandSpec {
        id: format!("movie_seg.{id}"),
        title: title.into(),
        menu: None,
        description: None,
        default_keybinding: None,
        pane: None,
    })
}

pub fn panel(plugin: &mut PluginBuilder, threshold: CommandRef, bands: [CommandRef; 3]) {
    let mut children = vec![Widget::Row {
        id: "rt".into(),
        children: vec![
            Widget::Label {
                id: "lt".into(),
                text: "Threshold".into(),
            },
            Widget::LineEdit {
                id: "threshold".into(),
                command: threshold,
                value: Some("400".into()),
                placeholder: None,
                moves_view: false,
            },
        ],
    }];
    children.extend(
        bands
            .into_iter()
            .enumerate()
            .map(|(band, command)| Widget::Checkbox {
                id: format!("band_{}", band + 1),
                command,
                text: format!("Band {} ({})", band + 1, crate::bands::NAMES[band]),
                value: Some(true),
                moves_view: false,
            }),
    );
    plugin.panel(PanelSpec::new(
        "movie_seg",
        "Movie seg",
        Widget::column("root", children),
    ));
}

pub fn overlays() -> [ChunkedOverlaySpec; 2] {
    [
        ChunkedOverlaySpec {
            id: "bands".into(),
            name: "Intensity bands".into(),
            time: ChunkTime::PerT,
        },
        ChunkedOverlaySpec {
            id: "footprint".into(),
            name: "Footprint".into(),
            time: ChunkTime::Static,
        },
    ]
}
