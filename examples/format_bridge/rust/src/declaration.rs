//! The executable declares its interface here; vizi-plugin.json is a development snapshot.
use vizi::declare::*;

pub const ID: &str = "org.example.format_bridge";

pub fn builder() -> PluginBuilder {
    let mut plugin = PluginBuilder::new(ID, "Crate Format Bridge", env!("CARGO_PKG_VERSION"));
    plugin.permission(Permission::DataAccess);
    plugin.asset(AssetSpec {
        id: "calibration".into(),
        name: "Crate sensor calibration v1".into(),
        filename: "vizi-crate-cal.bin".into(),
        size: 32,
        sha256: "20845d7cc64ec17ab899a993c1138352828823e55ebc1959f257a28e528671d3".into(),
        source: AssetSource::Manual,
    });
    plugin
}

pub fn commands() -> [CommandSpec; 4] {
    [
        CommandSpec {
            id: "crate.set_verify".into(),
            title: "Verify CRC-32 on volume reads".into(),
            menu: None,
            description: None,
            default_keybinding: None,
            pane: None,
        },
        CommandSpec {
            id: "crate.describe".into(),
            title: "Describe active dataset".into(),
            menu: None,
            description: None,
            default_keybinding: None,
            pane: None,
        },
        CommandSpec {
            id: "crate.pick_calibration".into(),
            title: "Use a different calibration".into(),
            menu: None,
            description: None,
            default_keybinding: None,
            pane: None,
        },
        CommandSpec {
            id: "crate.check_file".into(),
            title: "Check a crate file…".into(),
            menu: Some(Menu::Tools),
            description: None,
            default_keybinding: Some("Ctrl+Shift+K".into()),
            pane: None,
        },
    ]
}

pub fn panels(
    plugin: &mut PluginBuilder,
    [crate_set_verify, crate_describe, crate_pick_calibration, _crate_check_file]: [CommandRef; 4],
) {
    plugin.panel(PanelSpec {
        id: "crate".into(),
        title: "Crate".into(),
        menu_label: Some("Crate bridge…".into()),
        view_targets: vec![],
        follow_targets: vec![],
        root: Widget::Column {
            id: "root".into(),
            children: vec![
                Widget::Label {
                    id: "head".into(),
                    text: "No crate open".into(),
                },
                Widget::Label {
                    id: "status".into(),
                    text: "Ready".into(),
                },
                Widget::Row {
                    id: "rvox".into(),
                    children: vec![
                        Widget::Label {
                            id: "lvox".into(),
                            text: "Voxel size".into(),
                        },
                        Widget::Label {
                            id: "vox".into(),
                            text: "(unknown)".into(),
                        },
                    ],
                },
                Widget::Row {
                    id: "rcal".into(),
                    children: vec![
                        Widget::Label {
                            id: "lcal".into(),
                            text: "Calibration".into(),
                        },
                        Widget::Label {
                            id: "cal".into(),
                            text: "(not provisioned)".into(),
                        },
                    ],
                },
                Widget::Checkbox {
                    id: "verify".into(),
                    moves_view: false,
                    command: crate_set_verify.clone(),
                    text: "Verify CRC-32 on volume reads".into(),
                    value: Some(true),
                },
                Widget::Button {
                    id: "describe".into(),
                    moves_view: false,
                    command: crate_describe.clone(),
                    text: "Describe active dataset".into(),
                },
                Widget::Button {
                    id: "pickcal".into(),
                    moves_view: false,
                    command: crate_pick_calibration.clone(),
                    text: "Use a different calibration…".into(),
                },
            ],
        },
    });
}

pub fn readers() -> [ReaderSpec; 1] {
    [ReaderSpec {
        id: "crate".into(),
        name: "Crate volume container".into(),
        extensions: vec![".crate".into()],
    }]
}

pub fn exporters() -> [ExporterSpec; 1] {
    [ExporterSpec {
        id: "crate".into(),
        name: "Crate volume container".into(),
        extension: ".crate".into(),
    }]
}

const CALIBRATION: &[u8] = include_bytes!("../vizi-crate-cal.bin");

/// Ship the manual asset for Plugin Manager's Import action, without bypassing
/// the host's digest verification or changing runtime provisioning semantics.
pub fn package_calibration(directory: &std::path::Path) -> Result<(), String> {
    use std::{fs, io::Write};
    let path = directory.join("vizi-crate-cal.bin");
    let failure = |operation: &str, error: std::io::Error| -> String {
        format!("{operation} {}: {error}\nTo fix: Check permissions and available space at this path, or choose a writable directory with --install=DIR or --manifest=DIR.", path.display())
    };
    let check_existing = || -> Result<(), String> {
        let metadata =
            fs::symlink_metadata(&path).map_err(|e| failure("inspecting calibration file", e))?;
        if !metadata.file_type().is_file()
            || fs::read(&path).map_err(|e| failure("reading calibration file", e))? != CALIBRATION
        {
            return Err(format!("{} already exists and is not the bundled calibration file.\nTo fix: Move it aside and retry", path.display()));
        }
        Ok(())
    };
    match fs::symlink_metadata(&path) {
        Ok(_) => return check_existing(),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => return Err(format!("checking {}: {e}", path.display())),
    }
    let mut temporary = tempfile::NamedTempFile::new_in(directory)
        .map_err(|e| failure("preparing calibration file", e))?;
    temporary
        .write_all(CALIBRATION)
        .map_err(|e| failure("writing and syncing calibration file", e))?;
    temporary
        .as_file()
        .sync_all()
        .map_err(|e| failure("writing and syncing calibration file", e))?;
    match temporary.persist_noclobber(&path) {
        Ok(_) => Ok(()),
        Err(e) if e.error.kind() == std::io::ErrorKind::AlreadyExists => check_existing(),
        Err(e) => Err(failure("publishing calibration file", e.error)),
    }
}

#[cfg(test)]
mod packaging_tests {
    use super::*;
    #[test]
    fn calibration_is_packaged_repeatably_without_overwriting_other_bytes() {
        let directory = tempfile::tempdir().unwrap();
        package_calibration(directory.path()).unwrap();
        package_calibration(directory.path()).unwrap();
        let path = directory.path().join("vizi-crate-cal.bin");
        assert_eq!(std::fs::read(&path).unwrap(), CALIBRATION);
        std::fs::write(&path, b"different calibration").unwrap();
        assert!(package_calibration(directory.path()).is_err());
        assert_eq!(std::fs::read(path).unwrap(), b"different calibration");
    }
    #[cfg(unix)]
    #[test]
    fn calibration_packaging_rejects_symlinks() {
        let directory = tempfile::tempdir().unwrap();
        let target = directory.path().join("target");
        std::fs::write(&target, CALIBRATION).unwrap();
        std::os::unix::fs::symlink(&target, directory.path().join("vizi-crate-cal.bin")).unwrap();
        assert!(package_calibration(directory.path()).is_err());
        assert_eq!(std::fs::read(target).unwrap(), CALIBRATION);
    }
}
