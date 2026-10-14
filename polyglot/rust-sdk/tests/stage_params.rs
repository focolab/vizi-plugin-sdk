use serde_json::{json, Value};
use std::sync::Arc;
use vizi_doorbell::{
    control::{Context, Result},
    declare::{PluginBuilder, StageSpec},
    manifest::Manifest,
    proto as pb,
    stage::{Outcome, ParamSpec, ParamType, Params, Slot, Stage},
};
fn manifest(params: Value) -> String {
    json!({"manifest_version":1,"id":"params.test","name":"Params","version":"1","engines":{"vizi":"1.0"},"entry":{"command":["plugin"]},"contributions":{"stages":[{"id":"edit","params":params}]}}).to_string()
}
#[test]
fn parameter_declarations_check_types_fields_defaults_and_exact_integer_bounds() {
    for (params, valid) in [
        (json!([]), true),
        (
            json!([{"key":"x","type":"float","value":2,"min":2.0,"max":2}]),
            true,
        ),
        (
            json!([{"key":"x","type":"int","value":i64::MIN,"min":i64::MIN,"max":i64::MAX}]),
            true,
        ),
        (
            json!([{"key":"x","type":"int","value":9007199254740993_i64,"min":9007199254740992_i64,"max":9007199254740993_i64}]),
            true,
        ),
        // Declarations must fit Value.i's sint64 width too.
        (
            json!([{"key":"x","type":"int","value":i64::MAX,"max":u64::MAX}]),
            false,
        ),
        (
            json!([{"key":"x","type":"int","value":9223372036854775808_u64}]),
            false,
        ),
        (
            json!([{"key":"x","type":"int","value":0,"min":9223372036854775808_u64}]),
            false,
        ),
        (
            json!([{"key":"x","type":"int","value":i64::MAX,"min":i64::MIN,"max":i64::MAX}]),
            true,
        ),
        (
            json!([{"key":"x","type":"choice","value":"","options":["","X","x"]}]),
            true,
        ),
        (json!([{"key":"x","type":"string","value":""}]), true),
        (
            json!([{"key":"x","type":"string","value":"é","max_length":2}]),
            true,
        ),
        (
            json!([{"key":"x","type":"string","value":"e\u{301}","max_length":2}]),
            false,
        ),
        (
            json!([{"key":"x","type":"string","value":"é","max_length":1}]),
            false,
        ),
        (
            json!([{"key":"x","type":"string","value":"é".repeat(2048)}]),
            true,
        ),
        (
            json!([{"key":"x","type":"string","value":"x".repeat(4097)}]),
            false,
        ),
        (json!([{"key":"x","type":"string","value":false}]), false),
        (
            json!([{"key":"x","type":"string","value":"","max_length":0}]),
            false,
        ),
        (
            json!([{"key":"x","type":"string","value":"","max_length":4097}]),
            false,
        ),
        (
            json!([{"key":"x","type":"string","value":"","max_length":-1}]),
            false,
        ),
        (
            json!([{"key":"x","type":"string","value":"","max_length":2.0}]),
            false,
        ),
        (
            json!([{"key":"x","type":"string","value":"","max_length":true}]),
            false,
        ),
        (
            json!([{"key":"x","type":"string","value":"","max_length":null}]),
            false,
        ),
        (
            json!([{"key":"x","type":"string","value":"","min":0}]),
            false,
        ),
        (
            json!([{"key":"x","type":"string","value":"","max":2}]),
            false,
        ),
        (
            json!([{"key":"x","type":"string","value":"","options":[""]}]),
            false,
        ),
        (
            json!([{"key":"x","type":"choice","value":"","options":[""],"max_length":2}]),
            false,
        ),
        (json!(null), false),
        (json!({}), false),
        (json!([true]), false),
        (
            json!([{"key":"x","name":1,"type":"bool","value":true}]),
            false,
        ),
        (json!([{"key":"x","type":"int","value":1.0}]), false),
        (json!([{"key":"x","type":"float","value":true}]), false),
        (json!([{"key":"x","type":"float","value":null}]), false),
        (
            json!([{"key":"x","type":"float","value":2,"min":null}]),
            false,
        ),
        (json!([{"key":"x","type":"int","value":2,"max":1.5}]), false),
        (
            json!([{"key":"x","type":"int","value":9007199254740993_i64,"max":9007199254740992_i64}]),
            false,
        ),
        (
            json!([{"key":"x","type":"float","value":2,"options":[]}]),
            false,
        ),
        (
            json!([{"key":"x","type":"bool","value":true,"max":1}]),
            false,
        ),
        (
            json!([{"key":"x","type":"choice","value":"x","options":["x"],"min":0}]),
            false,
        ),
        (
            json!([{"key":"x","type":"bool","value":true,"future":1}]),
            false,
        ),
        (
            json!([{"key":"x","type":"choice","value":true,"options":[true]}]),
            false,
        ),
        (
            json!([{"key":"x","type":"choice","value":"X","options":["x"]}]),
            false,
        ),
    ] {
        let source = manifest(params);
        assert_eq!(Manifest::parse(&source).is_ok(), valid, "{source}");
    }
}
struct Noop;
impl Stage for Noop {
    fn plan(&self, _: &Context, _: &pb::StageShape, _: &Params) -> Result<pb::StagePlanned> {
        unreachable!()
    }
    fn process(&self, _: &Context, _: &pb::StageArm, _: &mut Slot) -> Result<Outcome> {
        unreachable!()
    }
}
#[test]
fn typed_parameter_declarations_use_the_manifest_validation_at_build() {
    for kind in [
        ParamType::Float {
            value: f64::NAN,
            min: None,
            max: None,
        },
        ParamType::Float {
            value: 1.,
            min: Some(f64::INFINITY),
            max: None,
        },
        ParamType::Int {
            value: 2,
            min: Some(3),
            max: None,
        },
        ParamType::Choice {
            value: "missing".into(),
            options: vec!["x".into()],
        },
        ParamType::String {
            value: "é".into(),
            max_length: Some(1),
        },
        ParamType::String {
            value: "".into(),
            max_length: Some(0),
        },
        ParamType::String {
            value: "".into(),
            max_length: Some(4097),
        },
        ParamType::String {
            value: "x".repeat(4097),
            max_length: None,
        },
    ] {
        let mut builder = PluginBuilder::new("test.params", "Params", "1");
        let mut stage = StageSpec::new("edit");
        stage.params.push(ParamSpec::new("setting", kind));
        builder.stage(stage, Arc::new(Noop));
        assert!(builder.build().is_err());
    }
}

#[test]
fn typed_strings_serialize_limits_without_normalizing_text() {
    for max_length in [None, Some(2)] {
        let mut builder = PluginBuilder::new("test.params", "Params", "1");
        let mut stage = StageSpec::new("edit");
        stage.params.push(ParamSpec::new(
            "label",
            ParamType::String {
                value: "é".into(),
                max_length,
            },
        ));
        builder.stage(stage, Arc::new(Noop));
        let plugin = builder.build().unwrap();
        let entry = &plugin.manifest().value()["contributions"]["stages"][0]["params"][0];
        assert_eq!(entry["type"], "string");
        assert_eq!(entry["value"], "é");
        assert_eq!(
            entry.get("max_length").and_then(Value::as_u64),
            max_length.map(|n| n as u64)
        );
    }
    assert_eq!(vizi_doorbell::stage::STRING_PARAM_MAX_BYTES, 4096);
}
