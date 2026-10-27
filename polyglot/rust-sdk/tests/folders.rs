use std::{path::Path, process::Command};
use vizi_doorbell::{proto as pb, settings_dir, SettingsDirUnavailableError};

// Child processes isolate launch environments from concurrent SDK tests.
#[test]
fn settings_directory_child() {
    let Ok(case) = std::env::var("VIZI_TEST_SETTINGS_CASE") else {
        return;
    };
    let result = settings_dir();
    match case.as_str() {
        "valid" | "missing" => {
            assert_eq!(
                result.unwrap(),
                std::path::PathBuf::from(std::env::var_os("VIZI_PLUGIN_SETTINGS_DIR").unwrap())
            );
            assert!(settings_dir().unwrap().is_dir());
        }
        "unset" | "empty" => assert_eq!(result, Err(SettingsDirUnavailableError::NotProvided)),
        "relative" => assert!(matches!(
            result,
            Err(SettingsDirUnavailableError::NotAbsolute(_))
        )),
        "file" | "parent-file" => {
            assert!(matches!(
                result,
                Err(SettingsDirUnavailableError::NotDirectory(_))
            ))
        }
        _ => panic!("unexpected case {case}"),
    }
}

#[test]
fn settings_directory_creates_the_host_named_folder_on_first_use() {
    let temp = tempfile::tempdir().unwrap();
    let folder = temp.path().join("host settings ü");
    std::fs::create_dir(&folder).unwrap();
    let file = temp.path().join("file");
    std::fs::write(&file, b"not a directory").unwrap();
    let missing = temp.path().join("missing").join("settings");
    let parent_file = file.join("settings");
    assert!(!missing.exists());
    for (case, value) in [
        ("valid", Some(folder.as_path())),
        ("unset", None),
        ("empty", Some(Path::new(""))),
        ("relative", Some(Path::new("relative-settings"))),
        ("missing", Some(missing.as_path())),
        ("file", Some(file.as_path())),
        ("parent-file", Some(parent_file.as_path())),
    ] {
        let mut cmd = Command::new(std::env::current_exe().unwrap());
        cmd.args(["--exact", "settings_directory_child", "--nocapture"])
            .env("VIZI_TEST_SETTINGS_CASE", case)
            .env_remove("VIZI_PLUGIN_SETTINGS_DIR")
            .current_dir(temp.path());
        if let Some(value) = value {
            cmd.env("VIZI_PLUGIN_SETTINGS_DIR", value);
        }
        let out = cmd.output().unwrap();
        assert!(out.status.success(), "{case}: {out:?}");
    }
    assert!(missing.is_dir());
    assert!(!temp.path().join("relative-settings").exists());
    assert_eq!(std::fs::read_dir(&folder).unwrap().count(), 0);
}

#[test]
fn absent_wire_fields_are_none_and_named_state_is_not_created() {
    use prost::Message;
    // Old peers omit these new fields entirely. Explicit empty strings have
    // the same protobuf representation; neither must become Path::new("").
    assert!(pb::ViewState::decode(&[][..])
        .unwrap()
        .file_state_dir()
        .is_none());
    assert!(pb::OpenReader::decode(&[][..])
        .unwrap()
        .file_state_dir()
        .is_none());
    assert!(pb::OpenExport::decode(&[][..])
        .unwrap()
        .file_state_dir()
        .is_none());
    let temp = tempfile::tempdir().unwrap();
    let path = temp
        .path()
        .join("companion ü")
        .join("plugins")
        .join("test.plugin");
    let value = path.to_str().unwrap().to_owned();
    let view = pb::ViewState {
        file_state_dir: value.clone(),
        ..Default::default()
    };
    let reader = pb::OpenReader {
        file_state_dir: value.clone(),
        ..Default::default()
    };
    let export = pb::OpenExport {
        file_state_dir: value,
        locator: "different-output.raw".into(),
        ..Default::default()
    };
    assert_eq!(view.file_state_dir(), Some(path.as_path()));
    assert_eq!(reader.file_state_dir(), Some(path.as_path()));
    assert_eq!(export.file_state_dir(), Some(path.as_path()));
    assert_eq!(std::fs::read_dir(temp.path()).unwrap().count(), 0);
}
