use serde::{Serialize, Serializer};
use std::sync::Arc;

#[derive(Clone, Debug, Serialize)]
pub struct StageSpec {
    pub id: String,
    pub name: String,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub params: Vec<crate::stage::ParamSpec>,
}
impl StageSpec {
    pub fn new(id: impl Into<String>) -> Self {
        let id = id.into();
        Self {
            name: id.clone(),
            id,
            params: Vec::new(),
        }
    }
}

/// A command registered with a particular builder. Widgets use this reference
/// so their manifest binding and the dispatch registration share one ID.
#[derive(Clone, Debug)]
pub struct CommandRef {
    pub(super) id: String,
    pub(super) owner: Arc<()>,
}
impl CommandRef {
    pub fn id(&self) -> &str {
        &self.id
    }
}
impl Serialize for CommandRef {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&self.id)
    }
}

/// `None` in a command or tool's `menu` field hides it from the Tools menu.
#[derive(Clone, Copy, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
#[non_exhaustive]
pub enum Menu {
    Tools,
}

#[derive(Clone, Copy, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
#[non_exhaustive]
pub enum Hold {
    Toggle,
    Momentary,
}

#[derive(Clone, Debug, Serialize)]
pub struct PaneButton {
    pub icon: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tooltip: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub hold: Option<Hold>,
}
impl PaneButton {
    /// The icon is supplied with `PluginBuilder::icon`; defaults to momentary
    /// on commands/actions and toggle on tools.
    pub fn new(icon: impl Into<String>) -> Self {
        Self {
            icon: icon.into(),
            tooltip: None,
            hold: None,
        }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct CommandSpec {
    pub id: String,
    pub title: String,
    pub menu: Option<Menu>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub default_keybinding: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pane: Option<PaneButton>,
}
impl CommandSpec {
    pub fn new(id: impl Into<String>, title: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            title: title.into(),
            menu: Some(Menu::Tools),
            description: None,
            default_keybinding: None,
            pane: None,
        }
    }
    /// A command for widgets or bindings, without a Tools-menu entry.
    pub fn hidden(mut self) -> Self {
        self.menu = None;
        self
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct ReaderSpec {
    pub id: String,
    pub name: String,
    pub extensions: Vec<String>,
}
impl ReaderSpec {
    /// Add suffixes offered alongside a more specific reader. The wrapper
    /// leaves ReaderSpec struct literals valid.
    pub fn fallback_extensions(
        self,
        extensions: impl IntoIterator<Item = impl Into<String>>,
    ) -> ReaderDeclaration {
        ReaderDeclaration {
            reader: self,
            fallback_extensions: extensions.into_iter().map(Into::into).collect(),
        }
    }
    pub fn new(
        id: impl Into<String>,
        name: impl Into<String>,
        extensions: impl IntoIterator<Item = impl Into<String>>,
    ) -> Self {
        Self {
            id: id.into(),
            name: name.into(),
            extensions: extensions.into_iter().map(Into::into).collect(),
        }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct ReaderDeclaration {
    #[serde(flatten)]
    pub reader: ReaderSpec,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub fallback_extensions: Vec<String>,
}
impl From<ReaderSpec> for ReaderDeclaration {
    fn from(reader: ReaderSpec) -> Self {
        reader.fallback_extensions(std::iter::empty::<String>())
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct ExporterSpec {
    pub id: String,
    pub name: String,
    pub extension: String,
}
impl ExporterSpec {
    pub fn new(
        id: impl Into<String>,
        name: impl Into<String>,
        extension: impl Into<String>,
    ) -> Self {
        Self {
            id: id.into(),
            name: name.into(),
            extension: extension.into(),
        }
    }
}

/// The host builds this tree; `Context::patch` and `push_image` update
/// it. This declaration does not introduce a second runtime widget model.
#[derive(Clone, Debug, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
#[non_exhaustive]
pub enum Widget {
    Label {
        id: String,
        text: String,
    },
    /// An explicit label format; serializes as the same `label` widget kind.
    #[serde(rename = "label")]
    FormattedLabel {
        id: String,
        text: String,
        #[serde(skip_serializing_if = "TextFormat::is_plain")]
        text_format: TextFormat,
    },
    Button {
        id: String,
        moves_view: bool,
        text: String,
        command: CommandRef,
    },
    Slider {
        id: String,
        moves_view: bool,
        command: CommandRef,
        min: f64,
        max: f64,
        /// Declared only when `Some`: a float slider without a step gets the
        /// host's 1000 positions and a three-decimal readout (manifest
        /// schema §3.3.1, `has_step`); an integer slider's default is 1, and
        /// a step it declares must be whole, or `build` refuses it. Either
        /// kind moves one step at a time over `min + k·step`, the last move
        /// stopping at `max`.
        #[serde(skip_serializing_if = "Option::is_none")]
        step: Option<f64>,
        #[serde(skip_serializing_if = "Option::is_none")]
        value: Option<f64>,
        float: bool,
    },
    Dropdown {
        id: String,
        moves_view: bool,
        command: CommandRef,
        options: Vec<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        value: Option<String>,
    },
    Checkbox {
        id: String,
        moves_view: bool,
        command: CommandRef,
        text: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        value: Option<bool>,
    },
    LineEdit {
        id: String,
        moves_view: bool,
        command: CommandRef,
        #[serde(skip_serializing_if = "Option::is_none")]
        placeholder: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        value: Option<String>,
    },
    ChannelPicker {
        id: String,
        moves_view: bool,
        command: CommandRef,
        multiple: bool,
        #[serde(skip_serializing_if = "Option::is_none")]
        value: Option<ChannelSelection>,
    },
    ColorPicker {
        id: String,
        moves_view: bool,
        command: CommandRef,
        #[serde(skip_serializing_if = "Option::is_none")]
        value: Option<String>,
    },
    ColormapPicker {
        id: String,
        moves_view: bool,
        command: CommandRef,
        #[serde(skip_serializing_if = "Option::is_none")]
        value: Option<String>,
    },
    RoiPicker {
        id: String,
        moves_view: bool,
        follows_selection: bool,
        filter: RoiFilter,
        command: CommandRef,
        #[serde(skip_serializing_if = "Option::is_none")]
        value: Option<String>,
    },
    Progress {
        id: String,
        value: f64,
        indeterminate: bool,
    },
    Image {
        id: String,
        moves_view: bool,
        #[serde(skip_serializing_if = "Option::is_none")]
        command: Option<CommandRef>,
    },
    /// Runtime rows, each instantiated from the leaf-only template. Cell IDs
    /// share the panel namespace; commands receive their row key in args["row"].
    List {
        id: String,
        moves_view: bool,
        row: Vec<Widget>,
        max_rows: u32,
        visible_rows: u32,
        selectable: bool,
        #[serde(skip_serializing_if = "Option::is_none")]
        command: Option<CommandRef>,
    },
    /// Rows of typed cells under declared columns, supplied at runtime with
    /// `Panel::set_table` and `Panel::patch_table`; up to 100,000 rows. With
    /// `selectable`, `command` fires with `{"rows": [key, …]}`
    /// (`Value.strings`), several keys with `multiple`; with `sortable`, a
    /// header click sorts on the host's side.
    Table {
        id: String,
        moves_view: bool,
        columns: Vec<TableColumn>,
        visible_rows: u32,
        sortable: bool,
        selectable: bool,
        multiple: bool,
        #[serde(skip_serializing_if = "Option::is_none")]
        command: Option<CommandRef>,
    },
    Row {
        id: String,
        children: Vec<Widget>,
    },
    Column {
        id: String,
        children: Vec<Widget>,
    },
}
/// A `table` column's cell type.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
#[non_exhaustive]
pub enum ColumnType {
    #[default]
    String,
    /// Right-aligned, sorted numerically; may declare precision and a unit.
    Number,
    Bool,
}
/// One `table` column. `header` defaults to the id; `precision` (decimals
/// shown, 0 to 15, default 3) and `unit` (shown in the header) belong to a
/// number column only.
#[derive(Clone, Debug, Serialize)]
pub struct TableColumn {
    pub id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub header: Option<String>,
    #[serde(rename = "type")]
    pub column_type: ColumnType,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub precision: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub unit: Option<String>,
}
impl TableColumn {
    fn new(id: impl Into<String>, header: impl Into<String>, column_type: ColumnType) -> Self {
        Self {
            id: id.into(),
            header: Some(header.into()),
            column_type,
            precision: None,
            unit: None,
        }
    }
    pub fn string(id: impl Into<String>, header: impl Into<String>) -> Self {
        Self::new(id, header, ColumnType::String)
    }
    pub fn number(id: impl Into<String>, header: impl Into<String>) -> Self {
        Self::new(id, header, ColumnType::Number)
    }
    pub fn bool(id: impl Into<String>, header: impl Into<String>) -> Self {
        Self::new(id, header, ColumnType::Bool)
    }
    /// Decimals shown in a number column.
    pub fn precision(mut self, precision: u32) -> Self {
        self.precision = Some(precision);
        self
    }
    /// A unit shown in a number column's header.
    pub fn unit(mut self, unit: impl Into<String>) -> Self {
        self.unit = Some(unit.into());
        self
    }
}
/// Channel-picker default: one 0-based index, or a list when multiple is true.
/// The host displays channel names (falling back to 1-based "Channel N" labels).
#[derive(Clone, Debug, Serialize)]
#[serde(untagged)]
pub enum ChannelSelection {
    Single(u32),
    Multiple(Vec<u32>),
}
/// Which regions a host ROI picker offers.
#[derive(Clone, Copy, Debug, Default, Serialize)]
#[serde(rename_all = "snake_case")]
#[non_exhaustive]
pub enum RoiFilter {
    #[default]
    All,
    Mine,
}

/// Applies to a label's initial text and subsequent set_value updates.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
#[non_exhaustive]
pub enum TextFormat {
    #[default]
    Plain,
    /// Qt's rich-text subset; links are styled but never opened.
    Rich,
}
impl TextFormat {
    fn is_plain(&self) -> bool {
        *self == Self::Plain
    }
}

impl Widget {
    /// A nonselectable list with the protocol defaults (256 rows, 8 visible).
    pub fn list(id: impl Into<String>, row: impl IntoIterator<Item = Widget>) -> Self {
        Self::List {
            moves_view: false,
            id: id.into(),
            row: row.into_iter().collect(),
            max_rows: 256,
            visible_rows: 8,
            selectable: false,
            command: None,
        }
    }
    /// A read-only, unsortable, nonselectable table with 8 visible rows. Use
    /// `Widget::Table` to set `visible_rows`, `sortable`, or `selectable` (and
    /// `multiple`) with a registered `CommandRef`.
    pub fn table(id: impl Into<String>, columns: impl IntoIterator<Item = TableColumn>) -> Self {
        Self::Table {
            id: id.into(),
            moves_view: false,
            columns: columns.into_iter().collect(),
            visible_rows: 8,
            sortable: false,
            selectable: false,
            multiple: false,
            command: None,
        }
    }
    /// A noninteractive indicator; it has no command binding or host echo.
    pub fn progress(id: impl Into<String>, value: f64, indeterminate: bool) -> Self {
        Self::Progress {
            id: id.into(),
            value,
            indeterminate,
        }
    }

    /// A plain-text label, including when the text looks like markup.
    pub fn label(id: impl Into<String>, text: impl Into<String>) -> Self {
        Self::Label {
            id: id.into(),
            text: text.into(),
        }
    }
    pub fn label_with_format(
        id: impl Into<String>,
        text: impl Into<String>,
        text_format: TextFormat,
    ) -> Self {
        Self::FormattedLabel {
            id: id.into(),
            text: text.into(),
            text_format,
        }
    }
    pub fn button(id: impl Into<String>, text: impl Into<String>, command: &CommandRef) -> Self {
        Self::Button {
            moves_view: false,
            id: id.into(),
            text: text.into(),
            command: command.clone(),
        }
    }
    pub fn image(id: impl Into<String>) -> Self {
        Self::Image {
            id: id.into(),
            command: None,
            moves_view: false,
        }
    }
    pub fn row(id: impl Into<String>, children: impl IntoIterator<Item = Widget>) -> Self {
        Self::Row {
            id: id.into(),
            children: children.into_iter().collect(),
        }
    }
    pub fn column(id: impl Into<String>, children: impl IntoIterator<Item = Widget>) -> Self {
        Self::Column {
            id: id.into(),
            children: children.into_iter().collect(),
        }
    }
    pub(super) fn check_owner(&self, owner: &Arc<()>) -> Result<(), String> {
        match self {
            Self::List { row, command, .. } => {
                if let Some(command) = command {
                    if !Arc::ptr_eq(owner, &command.owner) {
                        return Err(format!(
                            "widget command '{}' belongs to another plugin builder",
                            command.id
                        ));
                    }
                }
                for cell in row {
                    cell.check_owner(owner)?;
                }
            }
            Self::Image {
                command: Some(command),
                ..
            }
            | Self::Table {
                command: Some(command),
                ..
            }
            | Self::Button { command, .. }
            | Self::Slider { command, .. }
            | Self::Dropdown { command, .. }
            | Self::Checkbox { command, .. }
            | Self::LineEdit { command, .. }
            | Self::ChannelPicker { command, .. }
            | Self::ColorPicker { command, .. }
            | Self::ColormapPicker { command, .. }
            | Self::RoiPicker { command, .. } => {
                if !Arc::ptr_eq(owner, &command.owner) {
                    return Err(format!(
                        "widget command '{}' belongs to another plugin builder",
                        command.id
                    ));
                }
            }
            Self::Row { children, .. } | Self::Column { children, .. } => {
                for child in children {
                    child.check_owner(owner)?;
                }
            }
            _ => {}
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct PanelSpec {
    pub id: String,
    pub title: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub menu_label: Option<String>,
    pub root: Widget,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub view_targets: Vec<ViewTarget>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub follow_targets: Vec<ViewTarget>,
}
impl PanelSpec {
    pub fn new(id: impl Into<String>, title: impl Into<String>, root: Widget) -> Self {
        Self {
            id: id.into(),
            title: title.into(),
            root,
            view_targets: Vec::new(),
            follow_targets: Vec::new(),
            menu_label: None,
        }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct OverlaySpec {
    pub id: String,
    pub name: String,
    pub segment_mib: u16,
}
impl OverlaySpec {
    pub fn new(id: impl Into<String>, name: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            name: name.into(),
            segment_mib: 32,
        }
    }
}

#[derive(Clone, Copy, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
#[non_exhaustive]
pub enum ChunkTime {
    PerT,
    Static,
}

#[derive(Clone, Debug, Serialize)]
pub struct ChunkedOverlaySpec {
    pub id: String,
    pub name: String,
    pub time: ChunkTime,
}
impl ChunkedOverlaySpec {
    pub fn new(id: impl Into<String>, name: impl Into<String>, time: ChunkTime) -> Self {
        Self {
            id: id.into(),
            name: name.into(),
            time,
        }
    }
}

#[derive(Clone, Copy, Debug, Serialize)]
#[non_exhaustive]
pub enum View {
    #[serde(rename = "2d")]
    D2,
    #[serde(rename = "3d")]
    D3,
}
#[derive(Clone, Copy, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ToolMode {
    Gesture,
    Fire,
}
#[derive(Clone, Copy, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
#[non_exhaustive]
pub enum MouseButton {
    Left,
    Middle,
    Right,
}
#[derive(Clone, Copy, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
#[non_exhaustive]
pub enum Modifier {
    Shift,
    Ctrl,
    Meta,
}

#[derive(Clone, Debug, Serialize)]
#[serde(untagged)]
#[non_exhaustive]
pub enum Trigger {
    Key {
        key: String,
        hold: Hold,
    },
    /// Mouse triggers are always momentary.
    Mouse {
        button: MouseButton,
        modifiers: Vec<Modifier>,
    },
}

#[derive(Clone, Debug, Serialize)]
pub struct ActionSpec {
    pub id: String,
    pub name: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub key: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pane: Option<PaneButton>,
}
impl ActionSpec {
    pub fn new(id: impl Into<String>, name: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            name: name.into(),
            key: None,
            pane: None,
        }
    }
}

fn is_true(value: &bool) -> bool {
    *value
}

#[derive(Clone, Debug, Serialize)]
pub struct ToolSpec {
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub view_targets: Vec<ViewTarget>,
    pub id: String,
    pub name: String,
    pub views: Vec<View>,
    pub menu: Option<Menu>,
    pub mode: ToolMode,
    pub cancel_on_camera_move: bool,
    /// Whether the tool wants the host's surface pick on its 3D presses
    /// (`E` §3). True is the default and is not written. False saves the host
    /// a trace per press for a tool that resolves its own anchor from the ray.
    /// It is a request about what
    /// the host computes, not a promise about what arrives: handle `Ray` and
    /// `RayAndPicked` whichever is declared. `InputEvent::ray()` is the ray
    /// under either, and `InputEvent::pick()` the pick or None.
    #[serde(skip_serializing_if = "is_true")]
    pub wants_pick: bool,
    pub triggers: Vec<Trigger>,
    pub actions: Vec<ActionSpec>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pane: Option<PaneButton>,
}
/// Navigation targets a panel or tool may request; the host still requires
/// a user cause and its own opt-in toggle for every move.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
#[non_exhaustive]
pub enum ViewTarget {
    T,
    Z,
    SelectRegion,
    FrameRegion,
    LookAt,
    OrbitPoint,
}

impl ToolSpec {
    pub fn new(id: impl Into<String>, name: impl Into<String>) -> Self {
        Self {
            view_targets: Vec::new(),
            id: id.into(),
            name: name.into(),
            views: vec![View::D2, View::D3],
            menu: Some(Menu::Tools),
            mode: ToolMode::Gesture,
            cancel_on_camera_move: true,
            wants_pick: true,
            triggers: Vec::new(),
            actions: Vec::new(),
            pane: None,
        }
    }
}

#[derive(Clone, Copy, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
#[non_exhaustive]
pub enum Permission {
    DataAccess,
    ContributeRois,
}

#[derive(Clone, Debug, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
#[non_exhaustive]
pub enum AssetSource {
    Manual,
    Https { url: String },
}

/// Assets are provisioned by the host; declaring one does not copy or download it.
#[derive(Clone, Debug, Serialize)]
pub struct AssetSpec {
    pub id: String,
    pub name: String,
    pub filename: String,
    pub size: u64,
    pub sha256: String,
    pub source: AssetSource,
}

/// Host pushes requested by a session, independent of its permissions.
#[derive(Clone, Copy, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
#[non_exhaustive]
pub enum Subscription {
    Epochs,
    /// Requires Permission::DataAccess.
    View,
    /// Requires Permission::DataAccess.
    Regions,
}
