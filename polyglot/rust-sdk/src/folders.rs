//! Host-provided storage conventions (control plane §2.3).
//! Settings are created on first access; create per-file state on first write.
use crate::proto;
use std::path::{Path, PathBuf};

/// The host could not provide a usable settings directory.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SettingsDirUnavailableError {
    /// The environment variable is absent or empty.
    NotProvided,
    /// The launch environment did not supply an absolute path.
    NotAbsolute(PathBuf),
    /// The directory cannot be created or accessed, or the path is not a directory.
    NotDirectory(PathBuf),
}
impl std::fmt::Display for SettingsDirUnavailableError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NotProvided => write!(
                f,
                "no settings folder: VIZI_PLUGIN_SETTINGS_DIR is unset or empty"
            ),
            Self::NotAbsolute(path) => write!(
                f,
                "VIZI_PLUGIN_SETTINGS_DIR must be absolute: {}",
                path.display()
            ),
            Self::NotDirectory(path) => write!(
                f,
                "VIZI_PLUGIN_SETTINGS_DIR does not name an accessible directory: {}",
                path.display()
            ),
        }
    }
}
impl std::error::Error for SettingsDirUnavailableError {}

/// Return the host-named settings directory, creating it on first use.
///
/// Reads `VIZI_PLUGIN_SETTINGS_DIR` on every call, without needing a command
/// context. Fails if absent, empty, relative, or the directory cannot be
/// created or accessed. Never chooses a fallback. An existing directory does
/// not guarantee that a subsequent write will succeed.
pub fn settings_dir() -> Result<PathBuf, SettingsDirUnavailableError> {
    let value = std::env::var_os("VIZI_PLUGIN_SETTINGS_DIR")
        .filter(|v| !v.is_empty())
        .ok_or(SettingsDirUnavailableError::NotProvided)?;
    let path = PathBuf::from(value);
    if !path.is_absolute() {
        return Err(SettingsDirUnavailableError::NotAbsolute(path));
    }
    if std::fs::create_dir_all(&path).is_err() || !path.is_dir() {
        return Err(SettingsDirUnavailableError::NotDirectory(path));
    }
    Ok(path)
}

fn file_state_dir(value: &str) -> Option<&Path> {
    (!value.is_empty()).then(|| Path::new(value))
}

impl proto::ViewState {
    /// State folder for the open dataset, or None when it has no local path.
    /// The host names it; the plugin creates it only on first write.
    pub fn file_state_dir(&self) -> Option<&Path> {
        file_state_dir(&self.file_state_dir)
    }
}
impl proto::OpenReader {
    /// State folder for the file being opened, or None for a non-local URI.
    /// The host names it; the plugin creates it only on first write.
    pub fn file_state_dir(&self) -> Option<&Path> {
        file_state_dir(&self.file_state_dir)
    }
}
impl proto::OpenExport {
    /// State folder for the export's SOURCE, not its output locator.
    /// None means the source has no local path. Never creates the folder.
    pub fn file_state_dir(&self) -> Option<&Path> {
        file_state_dir(&self.file_state_dir)
    }
}
