//! Manifest schema 0/1, validated against docs/spec/manifest_schema.md.
use serde_json::{Map, Value};
use std::{collections::BTreeSet, io::Read, path::Path};

#[derive(Clone, Debug)]
pub struct Manifest {
    value: Value,
    pub warnings: Vec<String>,
    pub minimum_protocol: (u32, u32),
}
type Result<T> = std::result::Result<T, String>;
fn object<'a>(v: &'a Value, at: &str) -> Result<&'a Map<String, Value>> {
    v.as_object()
        .ok_or_else(|| format!("{at} must be an object"))
}
fn text<'a>(v: &'a Value, at: &str) -> Result<&'a str> {
    v.as_str().ok_or_else(|| format!("{at} must be a string"))
}
fn required<'a>(m: &'a Map<String, Value>, key: &str) -> Result<&'a Value> {
    m.get(key)
        .ok_or_else(|| format!("missing required field '{key}'"))
}
fn name<'a>(m: &'a Map<String, Value>, key: &str) -> Result<&'a str> {
    let s = text(required(m, key)?, key)?;
    if s.trim().is_empty() {
        return Err(format!("{key} must be non-blank"));
    }
    Ok(s)
}
fn strings<'a>(v: &'a Value, at: &str, nonempty: bool) -> Result<Vec<&'a str>> {
    let a = v
        .as_array()
        .ok_or_else(|| format!("{at} must be a list of strings"))?;
    if nonempty && a.is_empty() {
        return Err(format!("{at} must be non-empty"));
    }
    a.iter().map(|v| text(v, at)).collect()
}
fn nonblank_suffix(s: &str) -> bool {
    // §3.1 includes VT, which u8::is_ascii_whitespace excludes.
    !s.trim_start_matches('.')
        .bytes()
        .all(|b| matches!(b, b' ' | b'\t' | b'\r' | b'\n' | 0x0b | 0x0c))
}
fn suffixes(item: &Map<String, Value>, key: &str) -> Result<BTreeSet<String>> {
    let Some(value) = item.get(key) else {
        return Ok(BTreeSet::new());
    };
    let values = value
        .as_array()
        .ok_or_else(|| format!("{key} must be a list"))?;
    values
        .iter()
        .map(|value| {
            let suffix = value
                .as_str()
                .filter(|s| nonblank_suffix(s))
                .ok_or_else(|| {
                    format!(
                        "each {}extension must be a non-empty string",
                        if key == "fallback_extensions" {
                            "fallback "
                        } else {
                            ""
                        }
                    )
                })?;
            Ok(format!(".{}", suffix.trim_start_matches('.')).to_lowercase())
        })
        .collect()
}
fn optional_text(m: &Map<String, Value>, keys: &[&str]) -> Result<()> {
    for k in keys {
        if let Some(v) = m.get(*k) {
            text(v, k)?;
        }
    }
    Ok(())
}
fn menu(m: &Map<String, Value>) -> Result<()> {
    if let Some(v) = m.get("menu") {
        if !v.is_null() && v.as_str() != Some("tools") {
            return Err("menu must be 'tools' or null".into());
        }
    }
    Ok(())
}
pub fn parse_protocol(s: &str) -> Result<(u32, u32)> {
    let s = s
        .trim()
        .trim_start_matches(">=")
        .trim_start_matches(['^', '~'])
        .trim();
    let a: Vec<_> = s.split('.').collect();
    if a.is_empty() || a.len() > 2 {
        return Err("engines.vizi is not a version".into());
    }
    let major = a[0]
        .parse::<u32>()
        .map_err(|_| "engines.vizi is not a version")?;
    let minor = match a.get(1) {
        None | Some(&"x") => 0,
        Some(v) => v
            .parse::<u32>()
            .map_err(|_| "engines.vizi is not a version")?,
    };
    Ok((major, minor))
}
impl Manifest {
    pub fn load(path: impl AsRef<Path>) -> Result<Self> {
        let path = path.as_ref();
        Self::parse_in_dir(
            &std::fs::read_to_string(path).map_err(|e| e.to_string())?,
            path.parent()
                .filter(|p| !p.as_os_str().is_empty())
                .unwrap_or_else(|| Path::new(".")),
        )
    }
    /// Parse using the working directory for pane icons. Prefer `load` for files.
    pub fn parse(json: &str) -> Result<Self> {
        Self::parse_in_dir(json, Path::new("."))
    }
    /// Parse a manifest with an explicit directory for its relative pane icons.
    pub fn parse_in_dir(json: &str, directory: impl AsRef<Path>) -> Result<Self> {
        let directory = directory.as_ref();
        Self::parse_with_icons(json, &|icon| load_icon(directory, icon))
    }
    pub(crate) fn parse_with_icons(
        json: &str,
        icons: &dyn Fn(&str) -> Result<Vec<u8>>,
    ) -> Result<Self> {
        let value: Value = serde_json::from_str(json).map_err(|e| e.to_string())?;
        let m = object(&value, "manifest")?;
        let version = required(m, "manifest_version")?
            .as_u64()
            .filter(|n| *n <= 1)
            .ok_or("manifest_version unsupported")?;
        name(m, "id")?;
        name(m, "name")?;
        name(m, "version")?;
        let minimum_protocol =
            parse_protocol(name(object(required(m, "engines")?, "engines")?, "vizi")?)?;
        let entry = object(required(m, "entry")?, "entry")?;
        strings(required(entry, "command")?, "entry.command", true)?;
        if let Some(seconds) = entry.get("startup_s").filter(|v| !v.is_null()) {
            seconds
                .as_f64()
                .filter(|n| n.is_finite() && *n > 0.0)
                .ok_or("entry.startup_s must be a positive number of seconds")?;
        }
        let empty = Map::new();
        let c = match m.get("contributions") {
            Some(v) => object(v, "contributions")?,
            None => &empty,
        };
        let mut ids = std::collections::BTreeMap::new();
        for kind in [
            "readers",
            "commands",
            "panels",
            "exporters",
            "overlays",
            "tools",
            "stages",
            "region_types",
        ] {
            let mut seen = BTreeSet::new();
            if let Some(v) = c.get(kind) {
                for item in v
                    .as_array()
                    .ok_or_else(|| format!("contributions.{kind} must be a list"))?
                {
                    let item = object(item, kind)?;
                    let id = name(item, "id")?;
                    if !seen.insert(id.to_owned()) {
                        return Err(if kind == "region_types" {
                            format!("duplicate region type id '{id}'")
                        } else {
                            format!("duplicate {kind} id '{id}'")
                        });
                    }
                }
            }
            ids.insert(kind, seen);
        }
        for (kind, entries) in c {
            if !ids.contains_key(kind.as_str()) {
                continue;
            }
            for entry in entries.as_array().unwrap() {
                let item = entry.as_object().unwrap();
                optional_text(item, &["name"])?;
                match kind.as_str() {
                    "readers" => {
                        let extensions = suffixes(item, "extensions")?;
                        let fallback = suffixes(item, "fallback_extensions")?;
                        if extensions.is_empty() && fallback.is_empty() {
                            return Err("extensions must be non-empty".into());
                        }
                        let overlap: Vec<_> = extensions.intersection(&fallback).collect();
                        if !overlap.is_empty() {
                            return Err(format!(
                                "a suffix is in both extensions and fallback_extensions: [{}]",
                                overlap
                                    .iter()
                                    .map(|s| format!("'{s}'"))
                                    .collect::<Vec<_>>()
                                    .join(", ")
                            ));
                        }
                    }
                    "exporters" => {
                        if !nonblank_suffix(text(required(item, "extension")?, "extension")?) {
                            return Err("extension must be non-empty".into());
                        }
                    }
                    "commands" => {
                        name(item, "title")?;
                        optional_text(item, &["description"])?;
                        menu(item)?;
                        version_one(item, version, &["pane"])?;
                        if let Some(v) = item.get("pane") {
                            pane(v, false, icons)?;
                        }
                        if let Some(v) = item.get("default_keybinding").filter(|v| !v.is_null()) {
                            if text(v, "default_keybinding")?.trim().is_empty() {
                                return Err("default_keybinding must be non-blank".into());
                            }
                        }
                    }
                    "panels" => {
                        let targets = view_targets(item)?;
                        navigation_targets(item, "follow_targets")?;
                        name(item, "title")?;
                        if let Some(v) = item.get("menu_label") {
                            let s = text(v, "menu_label")?;
                            if s.trim().is_empty() || s.chars().count() > 128 {
                                return Err("invalid menu_label".into());
                            }
                        }
                        widget(
                            required(item, "root")?,
                            1,
                            &mut BTreeSet::new(),
                            &ids["commands"],
                        )?;
                        let cells = list_cells(required(item, "root")?);
                        if cells > 4096 {
                            return Err(format!(
                                "panel '{}''s lists may realise at most 4096 cells together (each list's max_rows × row cells), not {cells}",
                                name(item, "id")?
                            ));
                        }
                        if !targets && any_moves_view(required(item, "root")?) {
                            return Err(
                                "panel has moves_view widgets and declares no view_targets".into(),
                            );
                        }
                    }
                    "overlays" => {
                        if item.get("chunked").is_some_and(|v| !v.is_boolean()) {
                            return Err("chunked must be a bool".into());
                        }
                        let chunked = item
                            .get("chunked")
                            .and_then(Value::as_bool)
                            .unwrap_or(false);
                        if chunked && item.contains_key("segment_mib") {
                            return Err("a chunked overlay's arena is sized by the host from the dataset; segment_mib does not apply".into());
                        }
                        if let Some(time) = item.get("time") {
                            if !matches!(time.as_str(), Some("per_t" | "static")) {
                                return Err("time must be one of ['per_t', 'static']".into());
                            }
                            if !chunked {
                                return Err("time applies only to a chunked overlay".into());
                            }
                        }
                        if let Some(v) = item.get("segment_mib") {
                            if !v.as_u64().is_some_and(|n| (1..=512).contains(&n)) {
                                return Err("segment_mib must be an int in [1, 512]".into());
                            }
                        }
                    }
                    "tools" => {
                        menu(item)?;
                        if let Some(v) = item.get("views") {
                            if strings(v, "views", true)?
                                .iter()
                                .any(|s| !matches!(*s, "2d" | "3d"))
                            {
                                return Err("views must be from ['2d', '3d']".into());
                            }
                        }
                        tool(item, version, icons)?;
                    }
                    "region_types" => {
                        if !m
                            .get("permissions")
                            .and_then(Value::as_array)
                            .is_some_and(|p| {
                                p.iter().any(|v| v.as_str() == Some("contribute_rois"))
                            })
                        {
                            return Err("region_types need the contribute_rois permission".into());
                        }
                        let id = name(item, "id")?;
                        if !id.as_bytes()[0].is_ascii_alphanumeric() {
                            return Err("region type id must start with a letter or digit".into());
                        }
                        if !id
                            .bytes()
                            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
                        {
                            return Err(
                                "region type id may contain only letters, digits, '.', '_' and '-'"
                                    .into(),
                            );
                        }
                        if matches!(id, "box" | "ellipsoid" | "cylinder" | "surfq6") {
                            return Err(format!("region type id '{id}' is a built-in type"));
                        }
                        if id.starts_with("vizi.") {
                            return Err(format!("region type id '{id}' is reserved: ids beginning 'vizi.' are the host's"));
                        }
                    }
                    "stages" => {
                        crate::stage::params::parse(item.get("params"))?;
                    }
                    _ => unreachable!(),
                }
            }
        }
        let has_starter = ["panels", "readers", "exporters", "stages", "region_types"]
            .iter()
            .any(|kind| !ids[kind].is_empty())
            || ["commands", "tools"].iter().any(|kind| {
                c.get(*kind)
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .any(|entry| {
                        // Only menu entries and pane buttons are always reachable.
                        entry.get("menu").is_none_or(|menu| !menu.is_null())
                            || entry.get("pane").is_some_and(|pane| !pane.is_null())
                    })
            });
        if !ids["overlays"].is_empty() && !has_starter {
            return Err("overlays are drawn only while the plugin runs, and nothing declared here starts it; declare a panel (or a command or tool in the Tools menu or on the pane, a reader, an exporter or a stage)".into());
        }
        let panel_moves_view = c
            .get("panels")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .any(|panel| {
                panel["view_targets"]
                    .as_array()
                    .is_some_and(|targets| !targets.is_empty())
            });
        if !panel_moves_view {
            for tool in c
                .get("tools")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                if tool["view_targets"]
                    .as_array()
                    .is_some_and(|targets| !targets.is_empty())
                {
                    return Err(format!("tool '{}' declares view_targets but no panel does; the move-the-view row lives on the plugin's panel", tool["id"].as_str().unwrap()));
                }
            }
        }
        let mut seen = BTreeSet::new();
        if let Some(v) = m.get("assets") {
            for a in v.as_array().ok_or("assets must be a list")? {
                let a = object(a, "asset")?;
                let id = name(a, "id")?;
                if !id.as_bytes()[0].is_ascii_alphanumeric()
                    || !id
                        .bytes()
                        .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(&c))
                {
                    return Err("invalid asset id".into());
                }
                if !seen.insert(id) {
                    return Err("duplicate asset id".into());
                }
                optional_text(a, &["name"])?;
                let file = name(a, "filename")?;
                if file.starts_with('.') || file.contains(['/', '\\', '\0']) {
                    return Err("filename must be one plain file name".into());
                }
                if required(a, "size")?.as_u64().is_none_or(|n| n == 0) {
                    return Err("size must be a positive integer".into());
                }
                let hash = name(a, "sha256")?;
                if hash.len() != 64 || !hash.bytes().all(|c| c.is_ascii_hexdigit()) {
                    return Err("sha256 must be exactly 64 hexadecimal characters".into());
                }
                let source = object(required(a, "source")?, "source")?;
                match name(source, "kind")? {
                    "manual" => {
                        if source.contains_key("url") {
                            return Err("manual source must not declare a url".into());
                        }
                    }
                    "https" => {
                        let url = url::Url::parse(name(source, "url")?)
                            .map_err(|_| "invalid source.url")?;
                        if url.scheme() != "https"
                            || url.host_str().is_none()
                            || !url.username().is_empty()
                            || url.password().is_some()
                        {
                            return Err("source.url must be HTTPS without credentials".into());
                        }
                    }
                    _ => return Err("source.kind must be manual or https".into()),
                }
            }
        }
        let mut warnings = Vec::new();
        if let Some(v) = m.get("permissions") {
            for s in strings(v, "permissions", false)? {
                if !matches!(s, "data_access" | "contribute_rois") {
                    warnings.push(format!("unknown permission '{s}' grants nothing"));
                }
            }
        }
        if let Some(v) = m.get("subscriptions") {
            for s in strings(v, "subscriptions", false)? {
                match s {
                    "epochs" => {}
                    "view" | "regions" => {
                        if !m
                            .get("permissions")
                            .and_then(Value::as_array)
                            .is_some_and(|a| a.iter().any(|v| v.as_str() == Some("data_access")))
                        {
                            warnings.push(format!("subscription '{s}' needs data_access; ignored"));
                        }
                    }
                    _ => warnings.push(format!("unknown subscription '{s}' ignored")),
                }
            }
        }
        Ok(Self {
            value,
            warnings,
            minimum_protocol,
        })
    }
    pub fn id(&self) -> &str {
        self.value["id"].as_str().unwrap()
    }
    pub fn version(&self) -> &str {
        self.value["version"].as_str().unwrap()
    }
    pub fn value(&self) -> &Value {
        &self.value
    }
    /// Structural widget capacity for a declared panel, including every possible
    /// list row. Template declarations still count once toward the 256-node limit.
    pub fn widget_count(&self, panel: &str) -> Option<usize> {
        fn count(widget: &Value) -> usize {
            if widget["kind"] == "list" {
                1 + widget["row"].as_array().unwrap().len()
                    * widget["max_rows"].as_u64().unwrap_or(256) as usize
            } else if matches!(widget["kind"].as_str(), Some("row" | "column")) {
                1 + widget["children"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .map(count)
                    .sum::<usize>()
            } else {
                1
            }
        }
        self.panel_root(panel).map(count)
    }
    pub(crate) fn panel_root(&self, panel: &str) -> Option<&Value> {
        self.value["contributions"]["panels"]
            .as_array()?
            .iter()
            .find(|v| v["id"] == panel)
            .map(|v| &v["root"])
    }
    pub(crate) fn list_max_rows(&self, panel: &str, id: &str) -> Option<usize> {
        fn find(widget: &Value, id: &str) -> Option<usize> {
            if widget["kind"] == "list" && widget["id"] == id {
                return Some(widget["max_rows"].as_u64().unwrap_or(256) as usize);
            }
            if matches!(widget["kind"].as_str(), Some("row" | "column")) {
                widget["children"]
                    .as_array()?
                    .iter()
                    .find_map(|child| find(child, id))
            } else {
                None
            }
        }
        find(self.panel_root(panel)?, id)
    }
    pub fn supports_host(&self, major: u32, minor: u32) -> bool {
        self.minimum_protocol.0 == major && minor >= self.minimum_protocol.1
    }
    pub fn contribution_ids(&self, kind: &str) -> BTreeSet<String> {
        self.value["contributions"][kind]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|v| v["id"].as_str().map(str::to_owned))
            .collect()
    }
}
fn view_targets(m: &Map<String, Value>) -> Result<bool> {
    navigation_targets(m, "view_targets")
}
fn navigation_targets(m: &Map<String, Value>, field: &str) -> Result<bool> {
    let Some(v) = m.get(field) else {
        return Ok(false);
    };
    let targets = strings(v, field, false)?;
    let mut seen = BTreeSet::new();
    for target in &targets {
        if !matches!(
            *target,
            "t" | "z" | "select_region" | "frame_region" | "look_at" | "orbit_point"
        ) || !seen.insert(*target)
        {
            return Err(format!("{field} must be distinct names from t, z, select_region, frame_region, look_at, orbit_point"));
        }
    }
    Ok(!targets.is_empty())
}
fn any_moves_view(v: &Value) -> bool {
    v["moves_view"].as_bool() == Some(true)
        || match v["kind"].as_str() {
            Some("row" | "column") => v["children"].as_array(),
            Some("list") => v["row"].as_array(),
            _ => None,
        }
        .is_some_and(|children| children.iter().any(any_moves_view))
}
// Called only after widget validation: list rows contain leaves, not lists.
fn list_cells(widget: &Value) -> u64 {
    if widget["kind"].as_str() == Some("list") {
        widget["max_rows"].as_u64().unwrap_or(256) * widget["row"].as_array().unwrap().len() as u64
    } else {
        widget["children"]
            .as_array()
            .into_iter()
            .flatten()
            .map(list_cells)
            .sum()
    }
}
fn widget(
    v: &Value,
    depth: usize,
    seen: &mut BTreeSet<String>,
    commands: &BTreeSet<String>,
) -> Result<()> {
    if depth > 8 {
        return Err("panel tree deeper than 8".into());
    }
    let m = object(v, "widget")?;
    let id = name(m, "id")?;
    if !seen.insert(id.to_owned()) {
        return Err("duplicate widget id".into());
    }
    if seen.len() > 256 {
        return Err("panel has more than 256 widgets".into());
    }
    let kind = name(m, "kind")?;
    optional_text(m, &["text", "placeholder", "command"])?;
    if let Some(format) = m.get("text_format") {
        if !matches!(format.as_str(), Some("plain" | "rich")) {
            return Err(format!(
                "{kind} '{id}' text_format must be one of ['plain', 'rich']"
            ));
        }
        if kind != "label" {
            return Err(format!(
                "{kind} '{id}' does not take text_format; only a label does"
            ));
        }
    }
    let interactive = matches!(
        kind,
        "button"
            | "slider"
            | "dropdown"
            | "checkbox"
            | "line_edit"
            | "channel_picker"
            | "color_picker"
            | "colormap_picker"
            | "roi_picker"
    ) || kind == "list"
        && m.get("selectable").and_then(Value::as_bool) == Some(true)
        || kind == "image" && m.contains_key("command");
    for flag in ["moves_view", "follows_selection"] {
        if m.get(flag).is_some_and(|v| !v.is_boolean()) {
            return Err(format!("{flag} must be boolean"));
        }
    }
    if m.get("moves_view").and_then(Value::as_bool) == Some(true) && !interactive {
        return Err("moves_view requires an interactive widget".into());
    }
    if kind != "roi_picker"
        && (m.get("follows_selection").and_then(Value::as_bool) == Some(true)
            || m.contains_key("filter"))
    {
        return Err("only roi_picker takes follows_selection or filter".into());
    }
    if kind == "roi_picker"
        && m.get("filter")
            .is_some_and(|v| !matches!(v.as_str(), Some("all" | "mine")))
    {
        return Err("roi_picker filter must be all or mine".into());
    }
    if kind == "image"
        && m.contains_key("command")
        && !m
            .get("command")
            .and_then(Value::as_str)
            .is_some_and(|s| commands.contains(s))
    {
        return Err("image widget must bind a declared command".into());
    }
    for k in ["min", "max", "step"] {
        if let Some(v) = m.get(k) {
            if !v.is_number() {
                return Err(format!("{k} must be numeric"));
            }
        }
    }
    if let Some(v) = m.get("float") {
        if !v.is_boolean() {
            return Err("float must be a boolean".into());
        }
    }
    if let Some(v) = m.get("options") {
        strings(v, "options", false)?;
    }
    if matches!(
        kind,
        "button"
            | "slider"
            | "dropdown"
            | "checkbox"
            | "line_edit"
            | "channel_picker"
            | "color_picker"
            | "colormap_picker"
            | "roi_picker"
    ) && !m
        .get("command")
        .and_then(Value::as_str)
        .is_some_and(|s| commands.contains(s))
    {
        return Err(format!("{kind} widget must bind a declared command"));
    }
    if let Some(multiple) = m.get("multiple") {
        if multiple.as_bool() == Some(true) && kind != "channel_picker" {
            return Err(format!("{kind} '{id}' does not take multiple"));
        }
        if !multiple.is_boolean() {
            return Err(format!("channel_picker '{id}' multiple must be boolean"));
        }
    }
    if let Some(selectable) = m.get("selectable") {
        if (selectable.as_bool() == Some(true) && kind != "list") || !selectable.is_boolean() {
            return Err("selectable must be boolean and is only allowed on a list".into());
        }
    }
    match kind {
        "list" => {
            let selectable = m
                .get("selectable")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            if selectable {
                if !m
                    .get("command")
                    .and_then(Value::as_str)
                    .is_some_and(|s| commands.contains(s))
                {
                    return Err("selectable list must bind a declared command".into());
                }
            } else if m.contains_key("command") {
                return Err("list binds a command only when selectable".into());
            }
            let cells = required(m, "row")?
                .as_array()
                .filter(|a| !a.is_empty())
                .ok_or("list row must be a non-empty array")?;
            for cell in cells {
                if matches!(cell["kind"].as_str(), Some("list" | "row" | "column")) {
                    return Err("list row holds leaf widgets only".into());
                }
                widget(cell, depth + 1, seen, commands)?;
            }
            for key in ["max_rows", "visible_rows"] {
                if m.get(key)
                    .is_some_and(|v| !v.as_u64().is_some_and(|n| (1..=1024).contains(&n)))
                {
                    return Err(format!("list {key} must be an integer in [1, 1024]"));
                }
            }
            let max_rows = m.get("max_rows").and_then(Value::as_u64).unwrap_or(256);
            if max_rows * cells.len() as u64 > 4096 {
                return Err(format!("list '{id}' may realise at most 4096 cells (max_rows × row cells), not {max_rows} × {}", cells.len()));
            }
        }
        "row" | "column" => {
            let children = required(m, "children")?
                .as_array()
                .filter(|a| !a.is_empty())
                .ok_or("children must be non-empty")?;
            for child in children {
                widget(child, depth + 1, seen, commands)?;
            }
        }
        "slider" => {
            let get = |k: &str, default: f64| m.get(k).and_then(Value::as_f64).unwrap_or(default);
            if get("min", 0.0) >= get("max", 1.0) || get("step", 1.0) <= 0.0 {
                return Err("slider needs min < max and step > 0".into());
            }
            if let Some(v) = m.get("value") {
                if !v.is_number() {
                    return Err("slider value must be numeric".into());
                }
            }
        }
        "dropdown" => {
            let opts = strings(required(m, "options")?, "options", true)?;
            if let Some(v) = m.get("value") {
                if !opts.contains(&text(v, "value")?) {
                    return Err("dropdown value not in options".into());
                }
            }
        }
        "progress" => {
            if let Some(v) = m.get("value") {
                let v = v.as_f64().ok_or("progress value must be numeric")?;
                if !(0.0..=1.0).contains(&v) {
                    return Err("progress value must be within [0, 1]".into());
                }
            }
            if m.get("indeterminate").is_some_and(|v| !v.is_boolean()) {
                return Err("progress indeterminate must be boolean".into());
            }
        }
        "checkbox" => {
            if let Some(v) = m.get("value") {
                if !v.is_boolean() {
                    return Err("checkbox value must be boolean".into());
                }
            }
        }
        "channel_picker" => {
            if let Some(v) = m.get("value") {
                let index = |v: &Value| v.as_u64().is_some();
                let valid = if m.get("multiple").and_then(Value::as_bool).unwrap_or(false) {
                    v.as_array()
                        .map_or_else(|| index(v), |a| a.iter().all(index))
                } else {
                    index(v)
                };
                if !valid {
                    return Err(format!(
                        "channel_picker '{id}' value must be a channel index{}",
                        if m.get("multiple").and_then(Value::as_bool) == Some(true) {
                            " list"
                        } else {
                            ""
                        }
                    ));
                }
            }
        }
        "color_picker" => {
            if m.get("value").is_some_and(|v| {
                !v.as_str().is_some_and(|s| {
                    s.len() == 7
                        && s.starts_with('#')
                        && s.as_bytes()[1..].iter().all(u8::is_ascii_hexdigit)
                })
            }) {
                return Err(format!(
                    "color_picker '{id}' value must be a #RRGGBB colour"
                ));
            }
        }
        "colormap_picker" | "roi_picker" => {
            optional_text(m, &["value"])?;
        }
        "line_edit" => {
            optional_text(m, &["value"])?;
        }
        "label" | "button" | "image" => {}
        _ => return Err(format!("unknown widget kind '{kind}'")),
    }
    Ok(())
}

fn version_one(m: &Map<String, Value>, version: u64, fields: &[&str]) -> Result<()> {
    if version == 0 {
        for field in fields {
            if m.contains_key(*field) {
                return Err(format!("{field} needs manifest_version 1"));
            }
        }
    }
    Ok(())
}
fn hold(m: &Map<String, Value>, default: &str) -> Result<String> {
    let value = m
        .get("hold")
        .map(|v| text(v, "hold"))
        .transpose()?
        .unwrap_or(default);
    if !matches!(value, "toggle" | "momentary") {
        return Err("hold must be toggle or momentary".into());
    }
    Ok(value.to_owned())
}
fn key(m: &Map<String, Value>) -> Result<Option<String>> {
    let Some(v) = m.get("key") else {
        return Ok(None);
    };
    let value = text(v, "key")?.trim();
    if value.is_empty() {
        return Err("key must be non-blank".into());
    }
    // Case and modifier order do not create distinct shortcuts.
    let mut parts: Vec<_> = value.split('+').map(|s| s.trim().to_lowercase()).collect();
    let key = parts.pop().unwrap();
    parts.sort();
    parts.dedup();
    parts.push(key);
    Ok(Some(parts.join("+")))
}
fn tool(
    m: &Map<String, Value>,
    version: u64,
    icons: &dyn Fn(&str) -> Result<Vec<u8>>,
) -> Result<()> {
    view_targets(m)?;
    if m.contains_key("binding") {
        return Err("binding is not a field".into());
    }
    version_one(
        m,
        version,
        &[
            "mode",
            "cancel_on_camera_move",
            "wants_pick",
            "triggers",
            "actions",
            "pane",
        ],
    )?;
    if let Some(v) = m.get("mode") {
        if !matches!(text(v, "mode")?, "gesture" | "fire") {
            return Err("mode must be gesture or fire".into());
        }
    }
    if m.get("cancel_on_camera_move")
        .is_some_and(|v| !v.is_boolean())
    {
        return Err("cancel_on_camera_move must be a boolean".into());
    }
    if m.get("wants_pick").is_some_and(|v| !v.is_boolean()) {
        return Err("wants_pick must be a boolean".into());
    }
    if let Some(v) = m.get("pane") {
        pane(v, true, icons)?;
    }
    let mut triggers = BTreeSet::new();
    let mut keys = BTreeSet::new();
    if let Some(v) = m.get("triggers") {
        for v in v.as_array().ok_or("triggers must be a list")? {
            let t = object(v, "trigger")?;
            let key = key(t)?;
            let button = t.get("button").map(|v| text(v, "button")).transpose()?;
            if key.is_some() == button.is_some() {
                return Err("a trigger names a key or a button, not both or neither".into());
            }
            let mut mods = BTreeSet::new();
            if let Some(k) = &key {
                if t.contains_key("modifiers") {
                    return Err("a key trigger carries its modifiers in the key name".into());
                }
                keys.insert(k.clone());
            } else {
                if !matches!(button, Some("left" | "right" | "middle")) {
                    return Err("invalid trigger button".into());
                }
                if let Some(v) = t.get("modifiers") {
                    let values = if let Some(s) = v.as_str() {
                        vec![s]
                    } else {
                        strings(v, "modifiers", false)?
                    };
                    for modifier in values {
                        if !matches!(modifier, "shift" | "ctrl" | "meta") {
                            return Err("mouse modifiers must be shift, ctrl or meta; alt is not a mouse modifier".into());
                        }
                        mods.insert(modifier.to_owned());
                    }
                }
                if button == Some("left") && mods.is_empty() {
                    return Err("the bare left button is never a trigger".into());
                }
            }
            let hold = hold(t, if key.is_some() { "toggle" } else { "momentary" })?;
            if button.is_some() && hold != "momentary" {
                return Err("a mouse trigger is momentary".into());
            }
            if !triggers.insert((key, button.map(str::to_owned), mods)) {
                return Err("duplicate trigger".into());
            }
        }
    }
    let mut ids = BTreeSet::new();
    let mut action_keys = BTreeSet::new();
    if let Some(v) = m.get("actions") {
        let actions = v.as_array().ok_or("actions must be a list")?;
        if actions.len() > 64 {
            return Err("more than 64 actions".into());
        }
        for v in actions {
            let a = object(v, "action")?;
            if !ids.insert(name(a, "id")?) {
                return Err("duplicate action id".into());
            }
            optional_text(a, &["name"])?;
            let key = key(a)?;
            if key.is_none() && !a.contains_key("pane") {
                return Err("an action needs a key or a pane button to be reachable".into());
            }
            if let Some(k) = key {
                if keys.contains(&k) {
                    return Err("action key is also one of this tool's triggers".into());
                }
                if !action_keys.insert(k) {
                    return Err("action key is also another action's of this tool".into());
                }
            }
            if let Some(v) = a.get("pane") {
                pane(v, false, icons)?;
            }
        }
    }
    Ok(())
}
fn pane(v: &Value, is_tool: bool, icons: &dyn Fn(&str) -> Result<Vec<u8>>) -> Result<()> {
    let m = object(v, "pane")?;
    let icon = name(m, "icon")?;
    if let Some(v) = m.get("tooltip") {
        if text(v, "pane.tooltip")?.chars().count() > 128 {
            return Err("pane.tooltip longer than 128 characters".into());
        }
    }
    let hold = hold(m, if is_tool { "toggle" } else { "momentary" })?;
    if !is_tool && hold != "momentary" {
        return Err("an action or command button fires once on click".into());
    }
    let portable = icon.replace('\\', "/");
    if portable.starts_with('/') || portable.contains(':') || portable.split('/').any(|s| s == "..")
    {
        return Err("pane.icon must be a path inside the plugin directory".into());
    }
    let path = Path::new(&portable);
    let suffix = path
        .extension()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    if !matches!(suffix.as_str(), "svg" | "png") {
        return Err("pane.icon must be .svg or .png".into());
    }
    let bytes = icons(&portable)?;
    if bytes.len() > 65536 {
        return Err("pane.icon exceeds 65536 bytes".into());
    }
    let valid = if suffix == "png" {
        bytes.starts_with(b"\x89PNG\r\n\x1a\n")
    } else {
        std::str::from_utf8(&bytes).is_ok_and(|s| {
            let s = s.trim_start_matches('\u{feff}').trim_start();
            s.starts_with('<') && s.contains("<svg")
        })
    };
    if !valid {
        return Err(format!("pane.icon is not a {suffix} file"));
    }
    Ok(())
}
// User-facing filesystem diagnostics should describe the cause, not expose errno.
pub(crate) fn io_cause(error: &std::io::Error) -> String {
    if error.raw_os_error().is_some() {
        std::io::Error::from(error.kind()).to_string()
    } else {
        error.to_string()
    }
}

pub(crate) fn load_icon(directory: &Path, icon: &str) -> Result<Vec<u8>> {
    let base = directory.canonicalize().map_err(|e| io_cause(&e))?;
    let path = base
        .join(icon)
        .canonicalize()
        .map_err(|_| "pane.icon not found in the plugin directory")?;
    if !path.starts_with(&base) {
        return Err("pane.icon must be inside the plugin directory".into());
    }
    let file = std::fs::File::open(path).map_err(|e| io_cause(&e))?;
    if !file.metadata().map_err(|e| io_cause(&e))?.is_file() {
        return Err("pane.icon must be a file".into());
    }
    let mut bytes = Vec::new();
    file.take(65537)
        .read_to_end(&mut bytes)
        .map_err(|e| io_cause(&e))?;
    Ok(bytes)
}
