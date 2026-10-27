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
/// Read `engines.vizi` (`"major.minor"`, `"major"` or `"major.x"`, with an
/// optional leading `^`, `~` or `>=`) as the minimum protocol it names.
///
/// The grammar is exact, `^(>=|\^|~)?[0-9]+(\.([0-9]+|x))?$`: at most one
/// prefix, then `MAJOR`, `MAJOR.MINOR` or `MAJOR.x` (`MAJOR` and `MAJOR.x`
/// read as minor 0), each component ASCII digits only (leading zeros
/// allowed) and at most `u32::MAX`, with no whitespace anywhere, no sign and
/// no other separator. Anything else is
/// `engines.vizi '…' is not a version`, or, for a component that is a minus
/// sign and digits, `… has a negative component`.
pub fn parse_protocol(s: &str) -> Result<(u32, u32)> {
    let written = s;
    let s = [">=", "^", "~"]
        .iter()
        .find_map(|p| s.strip_prefix(p))
        .unwrap_or(s);
    let not_a_version = || format!("engines.vizi '{written}' is not a version");
    let a: Vec<_> = s.split('.').collect();
    if a.is_empty() || a.len() > 2 {
        return Err(not_a_version());
    }
    let digits = |d: &str| !d.is_empty() && d.bytes().all(|b| b.is_ascii_digit());
    let component = |v: &str| {
        if !digits(v) {
            let negative = v.strip_prefix('-').is_some_and(digits);
            return Err(if negative {
                format!("engines.vizi '{written}' has a negative component")
            } else {
                not_a_version()
            });
        }
        // ASCII digits alone: the only failure left is overflow, which is
        // not a version, never wrapped or clamped.
        v.parse::<u32>().map_err(|_| not_a_version())
    };
    let major = component(a[0])?;
    let minor = match a.get(1) {
        None | Some(&"x") => 0,
        Some(v) => component(v)?,
    };
    Ok((major, minor))
}

/// The one `manifest_version` this SDK reads and writes. A 1.x addition
/// raises `engines.vizi`, never `manifest_version`.
pub const MANIFEST_VERSION: i64 = 1;

/// Whether `id` is a plugin id: lower-case reverse-DNS with at least one dot
/// (`^[a-z][a-z0-9]*(\.[a-z][a-z0-9_]*)+$`), 3 to 64 characters, whose first
/// part is not a Windows device name. Distinct ids then name distinct folders,
/// and every one is a folder Windows can create.
pub fn valid_id(id: &str) -> bool {
    let mut parts = id.split('.');
    let first = parts.next().unwrap_or("");
    let segment = |s: &str, more: fn(u8) -> bool| {
        s.as_bytes().first().is_some_and(u8::is_ascii_lowercase) && s.bytes().skip(1).all(more)
    };
    device_name(id).is_none()
        && (3..=64).contains(&id.len())
        && segment(first, |b| b.is_ascii_lowercase() || b.is_ascii_digit())
        && id.contains('.')
        && parts.all(|s| {
            segment(s, |b| {
                b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_'
            })
        })
}

/// `id`'s first dot-separated part when that part, in any case, is a Windows
/// device name (`con`, `prn`, `aux`, `nul`, `com0`-`com9`, `lpt0`-`lpt9`).
/// Windows refuses those as a folder name, and an installer's folder is named
/// by the id.
fn device_name(id: &str) -> Option<&str> {
    let first = id.split('.').next().unwrap_or("");
    let lower = first.to_ascii_lowercase();
    let numbered = ["com", "lpt"].iter().any(|p| {
        lower
            .strip_prefix(p)
            .is_some_and(|n| matches!(n.as_bytes(), [b'0'..=b'9']))
    });
    (matches!(lower.as_str(), "con" | "prn" | "aux" | "nul") || numbered).then_some(first)
}

fn id_message(id: &str) -> String {
    if let Some(part) = device_name(id) {
        return format!(
            "id '{id}' is not a plugin id: its first part '{part}' is a Windows device name"
        );
    }
    format!(
        "id '{id}' is not a plugin id: lower-case reverse-DNS, 3 to 64 characters, matching \
         ^[a-z][a-z0-9]*(\\.[a-z][a-z0-9_]*)+$"
    )
}

/// The `min_vizi` a written manifest states when its author sets none: vizi
/// 1.0.0, the first release that speaks protocol 1.0. An older host ignores
/// the field.
pub const DEFAULT_MIN_VIZI: &str = "1.0.0";

/// Whether `v` is a `min_vizi` this SDK accepts: a PEP 440 version in its
/// normalized public form without an epoch or a local label -- release
/// segments, then an optional `a`/`b`/`rc` pre-release, `.post` and `.dev`
/// (`1.0.0`, `1.0rc1`, `1.2.post3`, `2.0.dev1`).
pub fn valid_min_vizi(v: &str) -> bool {
    fn number(s: &str) -> Option<&str> {
        let n = s.bytes().take_while(u8::is_ascii_digit).count();
        (n > 0).then(|| &s[n..])
    }
    let Some(mut rest) = number(v) else {
        return false;
    };
    while let Some(after) = rest.strip_prefix('.').and_then(number) {
        rest = after;
    }
    for pre in ["a", "b", "rc"] {
        if let Some(after) = rest.strip_prefix(pre).and_then(number) {
            rest = after;
            break;
        }
    }
    if let Some(after) = rest.strip_prefix(".post").and_then(number) {
        rest = after;
    }
    if let Some(after) = rest.strip_prefix(".dev").and_then(number) {
        rest = after;
    }
    rest.is_empty()
}

/// The SDK release that built the plugin, in the release form every SDK
/// writes to `sdk_version` (`1.0.0` for the crate's `1.0.0`; a pre-release
/// crate version such as `1.1.0-alpha.1` is written `1.1a1`).
pub fn sdk_release() -> String {
    release_form(env!("CARGO_PKG_VERSION"))
}
fn release_form(semver: &str) -> String {
    let (base, pre) = semver.split_once('-').unwrap_or((semver, ""));
    // A final release keeps all three numbers (`1.0.0`); a pre-release drops
    // a zero patch, as PEP 440 writes it (`1.0.0-alpha.1` is `1.0a1`).
    let base = if pre.is_empty() {
        base
    } else {
        base.strip_suffix(".0")
            .filter(|b| b.contains('.'))
            .unwrap_or(base)
    };
    let pre = pre
        .replacen("alpha.", "a", 1)
        .replacen("beta.", "b", 1)
        .replacen("rc.", "rc", 1);
    format!("{base}{pre}")
}

/// For each permission this SDK knows, the protocol minor that introduced
/// it. A name is honored only when the manifest's `engines.vizi` minor is
/// at least that; otherwise it is warned about and ignored, like an unknown
/// name. Append-only within 1.x.
pub const PERMISSIONS_INTRODUCED: &[(&str, u32)] = &[("data_access", 0), ("contribute_rois", 0)];
/// A `table` widget's bounds (`manifest_schema.md` §3.3.1). Its
/// rows are data, not widgets, so they do not count against a list's 4096
/// realized cells. A whole statement or patch past `TABLE_MAX_ROWS` is refused
/// whole by the host, and one `PanelPatch` carrying a table op is at most
/// `TABLE_MAX_PATCH_BYTES` encoded.
pub const TABLE_MAX_ROWS: usize = 100_000;
pub const TABLE_MAX_COLUMNS: usize = 32;
pub const TABLE_MAX_PATCH_BYTES: usize = 1 << 20;
/// The larger repeated field one table message carries: its rows (or
/// upserts) or its deleted keys, each counted on its own, so that no message
/// passes §9's 65,536 for a repeated field (the same number in every
/// SDK).
pub const TABLE_MAX_ROWS_PER_MESSAGE: usize = 32_768;
pub const TABLE_DEFAULT_PRECISION: u32 = 3;
pub const TABLE_COLUMN_TYPES: [&str; 3] = ["string", "number", "bool"];
/// A table row key, like a list's: non-empty and at most this many characters.
pub const TABLE_MAX_KEY_CHARS: usize = 128;
/// For each subscription this SDK knows, the protocol minor that introduced it.
pub const SUBSCRIPTIONS_INTRODUCED: &[(&str, u32)] = &[("epochs", 0), ("view", 0), ("regions", 0)];
fn honoured(table: &[(&str, u32)], name: &str, minor: u32) -> bool {
    table
        .iter()
        .any(|(n, introduced)| *n == name && minor >= *introduced)
}

/// What a manifest is refused for.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[non_exhaustive]
pub enum ManifestErrorKind {
    /// `engines.vizi` names a protocol this SDK does not run: a different
    /// major, or a newer minor. Reported before any other check.
    EnginesIncompatible,
    /// The manifest breaks a rule of the schema.
    Invalid,
    /// The manifest file could not be read.
    Unreadable,
}

/// A refused manifest: its kind, and the diagnostic as text.
///
/// It reads as the diagnostic (`Display`, `Deref<Target = str>`, comparison
/// with strings, and `From` into `String`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ManifestError {
    kind: ManifestErrorKind,
    message: String,
}
impl ManifestError {
    fn new(kind: ManifestErrorKind, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
        }
    }
    pub fn kind(&self) -> ManifestErrorKind {
        self.kind
    }
    pub fn message(&self) -> &str {
        &self.message
    }
}
impl std::fmt::Display for ManifestError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}
impl std::error::Error for ManifestError {}
impl std::ops::Deref for ManifestError {
    type Target = str;
    fn deref(&self) -> &str {
        &self.message
    }
}
impl From<ManifestError> for String {
    fn from(e: ManifestError) -> Self {
        e.message
    }
}
impl PartialEq<str> for ManifestError {
    fn eq(&self, other: &str) -> bool {
        self.message == other
    }
}
impl PartialEq<&str> for ManifestError {
    fn eq(&self, other: &&str) -> bool {
        self.message == *other
    }
}
impl PartialEq<String> for ManifestError {
    fn eq(&self, other: &String) -> bool {
        &self.message == other
    }
}

/// The sentence for a plugin this protocol cannot run, said the same by every
/// implementation: `"<name> <version> needs vizi protocol <engines>; this vizi
/// speaks <major>.<minor>"`, naming the id when the name is missing.
pub fn engines_message(plugin: &str, version: &str, engines: &str) -> String {
    format!(
        "{plugin} {version} needs vizi protocol {engines}; this vizi speaks {}.{}",
        crate::PROTOCOL_VERSION.0,
        crate::PROTOCOL_VERSION.1
    )
}

/// `engines.vizi` first, whatever else the manifest holds, so that a plugin
/// built for a later 1.x is refused for that and not for the first thing that
/// 1.x added. Only a string `engines.vizi` is read here; a missing or
/// non-string one is the schema's to refuse, in its order.
fn check_engines(value: &Value) -> std::result::Result<(), ManifestError> {
    let Some(vizi) = value
        .get("engines")
        .and_then(|e| e.get("vizi"))
        .and_then(Value::as_str)
    else {
        return Ok(());
    };
    let (major, minor) =
        parse_protocol(vizi).map_err(|e| ManifestError::new(ManifestErrorKind::Invalid, e))?;
    if major == crate::PROTOCOL_VERSION.0 && minor <= crate::PROTOCOL_VERSION.1 {
        return Ok(());
    }
    let field = |key: &str| value.get(key).and_then(Value::as_str).unwrap_or("");
    let plugin = match field("name") {
        "" => field("id"),
        name => name,
    };
    Err(ManifestError::new(
        ManifestErrorKind::EnginesIncompatible,
        engines_message(plugin, field("version"), vizi),
    ))
}
impl Manifest {
    pub fn load(path: impl AsRef<Path>) -> std::result::Result<Self, ManifestError> {
        let path = path.as_ref();
        Self::parse_in_dir(
            &std::fs::read_to_string(path)
                .map_err(|e| ManifestError::new(ManifestErrorKind::Unreadable, e.to_string()))?,
            path.parent()
                .filter(|p| !p.as_os_str().is_empty())
                .unwrap_or_else(|| Path::new(".")),
        )
    }
    /// Parse using the working directory for pane icons. Prefer `load` for files.
    pub fn parse(json: &str) -> std::result::Result<Self, ManifestError> {
        Self::parse_in_dir(json, Path::new("."))
    }
    /// Parse a manifest with an explicit directory for its relative pane icons.
    pub fn parse_in_dir(
        json: &str,
        directory: impl AsRef<Path>,
    ) -> std::result::Result<Self, ManifestError> {
        let directory = directory.as_ref();
        Self::parse_with_icons(json, &|icon| load_icon(directory, icon))
    }
    pub(crate) fn parse_with_icons(
        json: &str,
        icons: &dyn Fn(&str) -> Result<Vec<u8>>,
    ) -> std::result::Result<Self, ManifestError> {
        let invalid = |e| ManifestError::new(ManifestErrorKind::Invalid, e);
        let value: Value = serde_json::from_str(json).map_err(|e| invalid(e.to_string()))?;
        check_engines(&value)?;
        Self::validate(value, icons).map_err(invalid)
    }
    fn validate(value: Value, icons: &dyn Fn(&str) -> Result<Vec<u8>>) -> Result<Self> {
        let m = object(&value, "manifest")?;
        let version = required(m, "manifest_version")?
            .as_i64()
            .filter(|_| !m["manifest_version"].is_boolean())
            .ok_or("field 'manifest_version' must be int")?;
        if version != MANIFEST_VERSION {
            let declared = match m.get("engines").and_then(|e| e.get("vizi")) {
                None => "None".to_owned(),
                Some(Value::String(s)) => format!("'{s}'"),
                Some(v) => v.to_string(),
            };
            return Err(format!(
                "manifest_version {version} unsupported (this host reads [1]; a 1.x addition \
                 raises engines.vizi, which this one declares as {declared}, never manifest_version)"
            ));
        }
        let id = name(m, "id")?;
        if !valid_id(id) {
            return Err(id_message(id));
        }
        name(m, "name")?;
        name(m, "version")?;
        // An omitted `engines` reads as {"vizi": "1.0"}, the lowest floor.
        let minimum_protocol = match m.get("engines") {
            None => parse_protocol("1.0")?,
            Some(engines) => parse_protocol(name(object(engines, "engines")?, "vizi")?)?,
        };
        if let Some(v) = m.get("min_vizi") {
            let v = v.as_str().ok_or("min_vizi must be a string")?;
            if !valid_min_vizi(v) {
                return Err(format!("min_vizi '{v}' is not a PEP 440 version"));
            }
        }
        if let Some(v) = m.get("sdk_version") {
            if v.as_str().is_none_or(str::is_empty) {
                return Err("sdk_version must be a non-empty string".into());
            }
        }
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
                                "panel '{}''s lists may realize at most 4096 cells together (each list's max_rows × row cells), not {cells}",
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
                        tool(item, icons)?;
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
        let minor = minimum_protocol.1;
        let mut data_access = false;
        if let Some(v) = m.get("permissions") {
            for s in strings(v, "permissions", false)? {
                if honoured(PERMISSIONS_INTRODUCED, s, minor) {
                    data_access |= s == "data_access";
                } else {
                    warnings.push(format!("unknown permission '{s}' grants nothing"));
                }
            }
        }
        if let Some(v) = m.get("subscriptions") {
            for s in strings(v, "subscriptions", false)? {
                if !honoured(SUBSCRIPTIONS_INTRODUCED, s, minor) {
                    warnings.push(format!("unknown subscription '{s}' ignored"));
                } else if matches!(s, "view" | "regions") && !data_access {
                    warnings.push(format!("subscription '{s}' needs data_access; ignored"));
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
    /// The column ids of the `table` widget `id` in `panel`, in declaration
    /// order; `None` when the panel declares no table of that id.
    pub fn table_columns(&self, panel: &str, id: &str) -> Option<Vec<String>> {
        fn find(widget: &Value, id: &str) -> Option<Vec<String>> {
            if widget["kind"] == "table" && widget["id"] == id {
                return Some(
                    widget["columns"]
                        .as_array()?
                        .iter()
                        .filter_map(|c| c["id"].as_str().map(str::to_owned))
                        .collect(),
                );
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
    /// Whether the slider `id` in `panel` declares `step` (`PanelWidget.has_step`):
    /// `manifest_schema.md` §3.3.1 remembers a declared step as such, so a declared
    /// 1.0 is not the default. A float slider with a step moves by it and reads
    /// out at its precision; one without has 1000 positions and three decimals.
    /// `None` when the panel has no slider of that id.
    pub fn slider_has_step(&self, panel: &str, id: &str) -> Option<bool> {
        fn find(widget: &Value, id: &str) -> Option<bool> {
            if widget["kind"] == "slider" && widget["id"] == id {
                return Some(widget.get("step").is_some());
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
    /// Whether a host honors this declared permission: a known name whose
    /// introduced minor is within `engines.vizi`.
    pub fn honours_permission(&self, permission: &str) -> bool {
        self.declares("permissions", permission)
            && honoured(PERMISSIONS_INTRODUCED, permission, self.minimum_protocol.1)
    }
    /// Whether a host arranges this declared subscription: a known name whose
    /// introduced minor is within `engines.vizi`, and for `view` and
    /// `regions`, only with `data_access` honored.
    pub fn arranges_subscription(&self, subscription: &str) -> bool {
        self.declares("subscriptions", subscription)
            && honoured(
                SUBSCRIPTIONS_INTRODUCED,
                subscription,
                self.minimum_protocol.1,
            )
            && (!matches!(subscription, "view" | "regions")
                || self.honours_permission("data_access"))
    }
    fn declares(&self, key: &str, name: &str) -> bool {
        self.value[key]
            .as_array()
            .is_some_and(|a| a.iter().any(|v| v.as_str() == Some(name)))
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
/// `x` as the reference's messages print a number (`%g`): six significant
/// digits, trailing zeros dropped, an exponent below 1e-4 or from 1e6.
fn general_number(x: f64) -> String {
    if x == 0.0 || !x.is_finite() {
        return x.to_string();
    }
    let scientific = format!("{x:.5e}");
    let (mantissa, exponent) = scientific.split_once('e').unwrap();
    let exponent: i32 = exponent.parse().unwrap();
    let trim = |s: &str| -> String {
        if s.contains('.') {
            s.trim_end_matches('0').trim_end_matches('.').to_owned()
        } else {
            s.to_owned()
        }
    };
    if !(-4..6).contains(&exponent) {
        let sign = if exponent < 0 { '-' } else { '+' };
        format!("{}e{sign}{:02}", trim(mantissa), exponent.abs())
    } else {
        trim(&format!("{x:.*}", (5 - exponent) as usize))
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
    ) || matches!(kind, "list" | "table")
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
        if multiple.as_bool() == Some(true) && !matches!(kind, "channel_picker" | "table") {
            return Err(format!("{kind} '{id}' does not take multiple"));
        }
        if !multiple.is_boolean() {
            return Err(format!("{kind} '{id}' multiple must be boolean"));
        }
    }
    if let Some(selectable) = m.get("selectable") {
        if (selectable.as_bool() == Some(true) && !matches!(kind, "list" | "table"))
            || !selectable.is_boolean()
        {
            return Err("selectable must be boolean and is only allowed on a list or table".into());
        }
    }
    if kind != "table" && (m.contains_key("columns") || m.contains_key("sortable")) {
        return Err(format!("{kind} '{id}' does not take columns or sortable"));
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
                if matches!(
                    cell["kind"].as_str(),
                    Some("list" | "table" | "row" | "column")
                ) {
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
                return Err(format!("list '{id}' may realize at most 4096 cells (max_rows × row cells), not {max_rows} × {}", cells.len()));
            }
        }
        "table" => table(m, id, commands)?,
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
            let step = get("step", 1.0);
            if !m.get("float").and_then(Value::as_bool).unwrap_or(false) && step.fract() != 0.0 {
                return Err(format!(
                    "slider '{id}' step {} is not a whole number; \
                     an integer slider moves by whole steps (or declare it float)",
                    general_number(step)
                ));
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
                return Err(format!("color_picker '{id}' value must be a #RRGGBB color"));
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

/// A `table` widget (§3.3.1) past the checks every widget takes. The texts
/// are the reference parser's, without its `panel '…'` context.
fn table(m: &Map<String, Value>, id: &str, commands: &BTreeSet<String>) -> Result<()> {
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
            return Err(format!("table widget '{id}' must bind a declared command"));
        }
    } else if m.contains_key("command") {
        return Err(format!("table '{id}' binds a command only when selectable"));
    }
    if m.get("multiple").and_then(Value::as_bool) == Some(true) && !selectable {
        return Err(format!("table '{id}' takes multiple only when selectable"));
    }
    let columns = required(m, "columns")?
        .as_array()
        .ok_or("field 'columns' must be list")?;
    if !(1..=TABLE_MAX_COLUMNS).contains(&columns.len()) {
        return Err(format!(
            "table '{id}' needs 1 to {TABLE_MAX_COLUMNS} columns, not {}",
            columns.len()
        ));
    }
    let mut seen = BTreeSet::new();
    for (j, column) in columns.iter().enumerate() {
        let at = format!("table '{id}' columns[{j}]");
        let column = column
            .as_object()
            .ok_or_else(|| format!("{at} must be an object"))?;
        let cid = text(required(column, "id")?, "id")?;
        if cid.trim().is_empty() || !seen.insert(cid) {
            return Err(format!("{at}: id '{cid}' must be non-empty and distinct"));
        }
        if column.get("header").is_some_and(|h| !h.is_string()) {
            return Err(format!("{at}: header must be a string"));
        }
        let kind = column.get("type").map_or(Some("string"), Value::as_str);
        if !kind.is_some_and(|k| TABLE_COLUMN_TYPES.contains(&k)) {
            return Err(format!(
                "{at}: type must be one of ['string', 'number', 'bool']"
            ));
        }
        if kind != Some("number")
            && (column.contains_key("precision") || column.contains_key("unit"))
        {
            return Err(format!(
                "{at}: only a number column takes precision or unit"
            ));
        }
        if column
            .get("precision")
            .is_some_and(|p| p.as_u64().is_none_or(|p| p > 15))
        {
            return Err(format!("{at}: precision must be an integer in [0, 15]"));
        }
        if column.get("unit").is_some_and(|u| !u.is_string()) {
            return Err(format!("{at}: unit must be a string"));
        }
    }
    if m.get("visible_rows")
        .is_some_and(|v| !v.as_u64().is_some_and(|n| (1..=1024).contains(&n)))
    {
        return Err(format!(
            "table '{id}' visible_rows must be an integer in [1, 1024]"
        ));
    }
    if m.get("sortable").is_some_and(|v| !v.is_boolean()) {
        return Err(format!("table '{id}' sortable must be a boolean"));
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
fn tool(m: &Map<String, Value>, icons: &dyn Fn(&str) -> Result<Vec<u8>>) -> Result<()> {
    view_targets(m)?;
    if m.contains_key("binding") {
        return Err("binding is not a field".into());
    }
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_release_form_of_a_crate_version() {
        assert_eq!(release_form("1.0.0"), "1.0.0");
        assert_eq!(release_form("1.0.1"), "1.0.1");
        assert_eq!(release_form("1.0.0-alpha.104"), "1.0a104");
        assert_eq!(release_form("1.2.0-beta.3"), "1.2b3");
        assert_eq!(release_form("2.0.0-rc.1"), "2.0rc1");
        assert_eq!(release_form("1.2.3"), "1.2.3");
        assert_eq!(sdk_release(), env!("CARGO_PKG_VERSION"));
        assert!(valid_min_vizi(&sdk_release()));
    }

    #[test]
    fn ids_and_min_vizi() {
        for id in ["a.b", "org.example2.my_tool"] {
            assert!(valid_id(id), "{id}");
        }
        for id in [
            "ab", "Org.x", "x", "1a.b", "a..b", "a.", ".a", "a_b.c", "a.1b", "a-b.c",
        ] {
            assert!(!valid_id(id), "{id}");
        }
        assert!(valid_id(&format!("a.{}", "b".repeat(62))));
        // A Windows device name as the first part, in any case, has its own
        // sentence; as a later part, or inside a longer first part, it is an
        // ordinary id.
        for (id, part) in [
            ("con.example", "con"),
            ("lpt1.tool", "lpt1"),
            ("LPT1.tool", "LPT1"),
            ("com9.x", "com9"),
            ("com0.x", "com0"),
            ("lpt0.x", "lpt0"),
            ("nul.a.b", "nul"),
            ("Aux", "Aux"),
            ("prn.x", "prn"),
        ] {
            assert!(!valid_id(id), "{id}");
            assert_eq!(
                id_message(id),
                format!("id '{id}' is not a plugin id: its first part '{part}' is a Windows device name")
            );
        }
        for id in ["org.Example", "exampletool", "my_org.thing"] {
            assert!(
                id_message(id).starts_with(&format!(
                    "id '{id}' is not a plugin id: lower-case reverse-DNS, 3 to 64 characters"
                )),
                "{id}"
            );
        }
        for id in [
            "console.example",
            "com10.x",
            "example.con",
            "org.lpt1.aux",
            "conx.nul",
        ] {
            assert!(valid_id(id), "{id}");
        }
        assert!(!valid_id(&format!("a.{}", "b".repeat(63))));
        for v in ["0", "0.9.5", "1.0rc2", "1.0a1.post2.dev3", "2.dev1"] {
            assert!(valid_min_vizi(v), "{v}");
        }
        for v in [
            "",
            "v1",
            "1.",
            ".1",
            "1.0-beta",
            "1.0+local",
            "1!2.0",
            "1.0c1",
            "1.0rc",
        ] {
            assert!(!valid_min_vizi(v), "{v}");
        }
    }

    #[test]
    fn the_engines_grammar() {
        for (v, want) in [
            ("1.0", (1, 0)),
            ("1", (1, 0)),
            ("1.x", (1, 0)),
            (">=1.x", (1, 0)),
            ("^1.2", (1, 2)),
            ("~1.2", (1, 2)),
            (">=1.2", (1, 2)),
            ("01.0", (1, 0)),
            ("1.007", (1, 7)),
            ("4294967295.4294967295", (u32::MAX, u32::MAX)),
        ] {
            assert_eq!(parse_protocol(v), Ok(want), "{v}");
        }
        // One prefix at most; digits only; no whitespace, sign, underscore or
        // other separator; a component past u32 is not a version, never
        // wrapped or clamped.
        for v in [
            "^^1.0",
            "^~1.0",
            "~>=1.0",
            ">=^1.0",
            "+1.0",
            "1.+0",
            " 1.0",
            "1.0 ",
            "1. 0",
            "^ 1.0",
            "1.0_0",
            "1_0",
            "1.0_1",
            "\u{0661}.\u{0660}",
            "1.\u{0660}",
            "4294967296.0",
            "1.4294967296",
            "99999999999999999999.0",
            "1.0.3",
            "1.*",
            "1.X",
            "x",
            "x.0",
            "",
            ".",
            "1.",
            ".0",
            "^",
            "one.zero",
        ] {
            assert_eq!(
                parse_protocol(v),
                Err(format!("engines.vizi '{v}' is not a version")),
                "{v:?}"
            );
        }
        for v in ["1.-1", "-1.0", "^-1"] {
            assert_eq!(
                parse_protocol(v),
                Err(format!("engines.vizi '{v}' has a negative component")),
                "{v:?}"
            );
        }
    }
}
