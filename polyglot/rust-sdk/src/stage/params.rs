//! Per-position stage settings: manifest declarations and shared wire validation.
use crate::{
    control::{Error, Result},
    proto as pb,
};
use serde::Serialize;
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet, HashMap};

/// Default and largest permitted string parameter limit, in UTF-8 bytes.
pub const STRING_PARAM_MAX_BYTES: usize = 4096;

/// One manifest parameter, declared alongside the stage implementation.
#[derive(Clone, Debug, Serialize)]
pub struct ParamSpec {
    pub key: String,
    pub name: String,
    #[serde(flatten)]
    pub kind: ParamType,
}
impl ParamSpec {
    pub fn new(key: impl Into<String>, kind: ParamType) -> Self {
        let key = key.into();
        Self {
            name: key.clone(),
            key,
            kind,
        }
    }
}

/// Defaults are for the host UI. Every plan and arm must supply every value.
#[derive(Clone, Debug, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ParamType {
    Float {
        value: f64,
        #[serde(skip_serializing_if = "Option::is_none")]
        min: Option<f64>,
        #[serde(skip_serializing_if = "Option::is_none")]
        max: Option<f64>,
    },
    Int {
        value: i64,
        #[serde(skip_serializing_if = "Option::is_none")]
        min: Option<i64>,
        #[serde(skip_serializing_if = "Option::is_none")]
        max: Option<i64>,
    },
    Bool {
        value: bool,
    },
    Choice {
        value: String,
        options: Vec<String>,
    },
    String {
        value: String,
        /// UTF-8 bytes, 1..=4096. None uses the default of 4096.
        #[serde(skip_serializing_if = "Option::is_none")]
        max_length: Option<usize>,
    },
}

/// A checked value in ordinary Rust types, without protobuf oneof wrappers.
#[derive(Clone, Debug, PartialEq)]
pub enum ParamValue {
    Float(f64),
    Int(i64),
    Bool(bool),
    Choice(String),
    String(String),
}
pub type Params = BTreeMap<String, ParamValue>;

pub(crate) struct Declaration {
    key: String,
    kind: Constraint,
}
enum Constraint {
    Float { min: Option<f64>, max: Option<f64> },
    // Defaults and bounds share Value.i's sint64 range, without float rounding.
    Int { min: Option<i64>, max: Option<i64> },
    Bool,
    Choice(Vec<String>),
    String { max_length: usize },
}
fn integer(v: &Value) -> Option<i64> {
    v.as_i64()
}
fn finite(v: &Value) -> Option<f64> {
    v.as_f64().filter(|n| n.is_finite())
}
fn range<T: PartialOrd + Copy>(value: T, min: Option<T>, max: Option<T>) -> bool {
    !min.zip(max).is_some_and(|(lo, hi)| lo > hi)
        && !min.is_some_and(|lo| value < lo)
        && !max.is_some_and(|hi| value > hi)
}
fn numeric<T: PartialOrd + Copy>(
    entry: &serde_json::Map<String, Value>,
    read: impl Fn(&Value) -> Option<T>,
) -> std::result::Result<(Option<T>, Option<T>), String> {
    let value = read(&entry["value"]).ok_or("invalid numeric default")?;
    let bound = |key| {
        entry
            .get(key)
            .map(|v| read(v).ok_or("invalid numeric bound"))
            .transpose()
    };
    let (min, max) = (bound("min")?, bound("max")?);
    if !range(value, min, max) {
        return Err("default outside range or min exceeds max".into());
    }
    Ok((min, max))
}

/// Used by both the manifest reader and runtime, including hand-written manifests.
pub(crate) fn parse(raw: Option<&Value>) -> std::result::Result<Vec<Declaration>, String> {
    let Some(raw) = raw else {
        return Ok(Vec::new());
    };
    let entries = raw.as_array().ok_or("stage params must be a list")?;
    let mut seen = BTreeSet::new();
    entries
        .iter()
        .map(|raw| {
            let entry = raw.as_object().ok_or("stage param must be an object")?;
            let key = entry
                .get("key")
                .and_then(Value::as_str)
                .filter(|s| !s.trim().is_empty())
                .ok_or("stage param key must be non-blank")?;
            if !seen.insert(key) {
                return Err(format!("duplicate stage param key {key:?}"));
            }
            if entry.get("name").is_some_and(|v| !v.is_string()) {
                return Err("stage param name must be a string".into());
            }
            if !entry.contains_key("value") {
                return Err("stage param needs a default value".into());
            }
            let kind = entry
                .get("type")
                .and_then(Value::as_str)
                .ok_or("stage param type missing")?;
            let fields: &[&str] = match kind {
                "float" | "int" => &["min", "max"],
                "choice" => &["options"],
                "string" => &["max_length"],
                "bool" => &[],
                _ => return Err("unknown stage param type".into()),
            };
            if entry.keys().any(|k| {
                !["key", "name", "type", "value"].contains(&k.as_str())
                    && !fields.contains(&k.as_str())
            }) {
                return Err(format!("stage param {key:?}: field not allowed for {kind}"));
            }
            let kind = match kind {
                "float" => {
                    let (min, max) = numeric(entry, finite)?;
                    Constraint::Float { min, max }
                }
                "int" => {
                    let (min, max) = numeric(entry, integer)?;
                    Constraint::Int { min, max }
                }
                "bool" if entry["value"].is_boolean() => Constraint::Bool,
                "bool" => return Err("stage bool default must be boolean".into()),
                "string" => {
                    let value = entry["value"]
                        .as_str()
                        .ok_or("stage string default must be a string")?;
                    let max_length = match entry.get("max_length") {
                        None => STRING_PARAM_MAX_BYTES,
                        Some(v) => v
                            .as_u64()
                            .filter(|n| (1..=STRING_PARAM_MAX_BYTES as u64).contains(n))
                            .ok_or("stage string max_length must be an integer in [1, 4096]")?
                            as usize,
                    };
                    if value.len() > max_length {
                        return Err("stage string default exceeds max_length in UTF-8 bytes".into());
                    }
                    Constraint::String { max_length }
                }
                "choice" => {
                    let options = entry
                        .get("options")
                        .and_then(Value::as_array)
                        .ok_or("stage choice options must be a list")?;
                    let options: Vec<String> = options
                        .iter()
                        .map(|v| {
                            v.as_str()
                                .map(str::to_owned)
                                .ok_or("stage choice options must be strings")
                        })
                        .collect::<std::result::Result<_, _>>()?;
                    if options.is_empty()
                        || options.iter().collect::<BTreeSet<_>>().len() != options.len()
                        || !entry["value"]
                            .as_str()
                            .is_some_and(|v| options.iter().any(|o| o == v))
                    {
                        return Err(
                            "stage choice needs distinct options and a default among them".into(),
                        );
                    }
                    Constraint::Choice(options)
                }
                _ => unreachable!(),
            };
            Ok(Declaration {
                key: key.to_owned(),
                kind,
            })
        })
        .collect()
}

/// The same checker gates PlanStage and StageArm; callers select the wire error.
pub(crate) fn check(declared: &[Declaration], wire: &HashMap<String, pb::Value>) -> Result<Params> {
    let invalid = |detail: String| Error::new(pb::error::Kind::InvalidArgument, detail);
    if wire.len() != declared.len() || declared.iter().any(|d| !wire.contains_key(&d.key)) {
        return Err(invalid(
            "stage params must contain every declared key and no other".into(),
        ));
    }
    declared
        .iter()
        .map(|d| {
            use pb::value::Kind;
            let value = match (&d.kind, &wire[&d.key].kind) {
                (Constraint::Float { min, max }, Some(Kind::F(v)))
                    if v.is_finite() && range(*v, *min, *max) =>
                {
                    ParamValue::Float(*v)
                }
                (Constraint::Int { min, max }, Some(Kind::I(v))) if range(*v, *min, *max) => {
                    ParamValue::Int(*v)
                }
                (Constraint::Bool, Some(Kind::B(v))) => ParamValue::Bool(*v),
                (Constraint::Choice(options), Some(Kind::S(v))) if options.contains(v) => {
                    ParamValue::Choice(v.clone())
                }
                (Constraint::String { max_length }, Some(Kind::S(v))) if v.len() <= *max_length => {
                    ParamValue::String(v.clone())
                }
                _ => {
                    return Err(invalid(format!(
                    "stage param {:?} has the wrong kind or is outside its range/options/length",
                    d.key
                )))
                }
            };
            Ok((d.key.clone(), value))
        })
        .collect()
}
