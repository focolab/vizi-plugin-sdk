//! The executable declares its interface here; vizi-plugin.json is a development snapshot.
use vizi::declare::*;

pub const ID: &str = "org.example.stage_chain";

pub fn builder() -> PluginBuilder {
    PluginBuilder::new(ID, "Stage Chain", env!("CARGO_PKG_VERSION"))
}

pub fn stages() -> [StageSpec; 4] {
    [
        StageSpec {
            id: "gain".into(),
            name: "Gain".into(),
            params: vec![
                vizi::stage::ParamSpec {
                    key: "factor".into(),
                    name: "Factor".into(),
                    kind: vizi::stage::ParamType::Float {
                        value: 1.0,
                        min: Some(0.0),
                        max: Some(16.0),
                    },
                },
                vizi::stage::ParamSpec {
                    key: "channel".into(),
                    name: "Channel".into(),
                    kind: vizi::stage::ParamType::Int {
                        value: 0,
                        min: Some(0),
                        max: Some(65535),
                    },
                },
                vizi::stage::ParamSpec {
                    key: "stall_ms".into(),
                    name: "Stall (ms)".into(),
                    kind: vizi::stage::ParamType::Int {
                        value: 0,
                        min: Some(0),
                        max: Some(60000),
                    },
                },
            ],
        },
        StageSpec {
            id: "diff".into(),
            name: "Absolute difference".into(),
            params: vec![
                vizi::stage::ParamSpec {
                    key: "a".into(),
                    name: "a".into(),
                    kind: vizi::stage::ParamType::Int {
                        value: 0,
                        min: Some(0),
                        max: Some(65535),
                    },
                },
                vizi::stage::ParamSpec {
                    key: "b".into(),
                    name: "b".into(),
                    kind: vizi::stage::ParamType::Int {
                        value: 1,
                        min: Some(0),
                        max: Some(65535),
                    },
                },
            ],
        },
        StageSpec {
            id: "widen".into(),
            name: "Widen to f32".into(),
            params: vec![vizi::stage::ParamSpec {
                key: "crop".into(),
                name: "Crop (voxels, y and x)".into(),
                kind: vizi::stage::ParamType::Int {
                    value: 0,
                    min: Some(0),
                    max: Some(1024),
                },
            }],
        },
        StageSpec {
            id: "drift".into(),
            name: "Drift (pose only)".into(),
            params: vec![vizi::stage::ParamSpec {
                key: "channel".into(),
                name: "channel".into(),
                kind: vizi::stage::ParamType::Int {
                    value: 0,
                    min: Some(0),
                    max: Some(65535),
                },
            }],
        },
    ]
}
