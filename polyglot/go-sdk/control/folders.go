package control

import (
	"errors"
	"fmt"
	"os"
)

// SettingsDirUnavailableError reports that the host did not supply a usable
// settings directory. Path is empty when VIZI_PLUGIN_SETTINGS_DIR is unset or
// empty. Err retains the underlying filesystem error, if any.
type SettingsDirUnavailableError struct {
	Path string
	Err  error
}

func (e *SettingsDirUnavailableError) Error() string {
	return fmt.Sprintf("plugin settings directory unavailable (VIZI_PLUGIN_SETTINGS_DIR=%q): %v", e.Path, e.Err)
}

func (e *SettingsDirUnavailableError) Unwrap() error { return e.Err }

// SettingsDir returns the directory named by VIZI_PLUGIN_SETTINGS_DIR, creating
// it on first use.
// It is usable without a Client or command context, including in reader and
// exporter callbacks. The host names the folder; this function creates it but
// never selects a fallback or caches the environment.
func SettingsDir() (string, error) {
	dir := os.Getenv("VIZI_PLUGIN_SETTINGS_DIR")
	if dir == "" {
		return "", &SettingsDirUnavailableError{Err: errors.New("environment variable is unset or empty")}
	}
	if err := os.MkdirAll(dir, 0755); err != nil {
		return "", &SettingsDirUnavailableError{Path: dir, Err: err}
	}
	info, err := os.Stat(dir)
	if err == nil && !info.IsDir() {
		err = errors.New("path is not a directory")
	}
	if err != nil {
		return "", &SettingsDirUnavailableError{Path: dir, Err: err}
	}
	return dir, nil
}

// FileStateDir returns this plugin's optional per-file state directory from a
// protocol.ViewState, protocol.OpenReader, or protocol.OpenExport. Empty or
// absent wire values (including nil messages) return nil, never a pointer to an
// empty path. The generated protobuf fields retain their raw string form.
//
// ViewState names the current dataset, OpenReader the file being opened, and
// OpenExport the SOURCE dataset, not the export destination. Use the value from
// the relevant message; files may change during a session. This function neither
// checks existence nor creates a directory: the plugin creates it on first write.
func FileStateDir(source interface{ GetFileStateDir() string }) *string {
	if source == nil {
		return nil
	}
	dir := source.GetFileStateDir()
	if dir == "" {
		return nil
	}
	return &dir
}
