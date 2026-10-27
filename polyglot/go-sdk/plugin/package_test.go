package plugin

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func TestDefinitionInstallAndUpdatePreserveOldBinaryAndUserFiles(t *testing.T) {
	for _, goos := range []string{"linux", "darwin", "windows"} {
		t.Run(goos, func(t *testing.T) {
			d := definition()
			d.Command(CommandSpec{ID: "go", Pane: &PaneButton{Icon: "icons/go.svg"}}, noopCommand)
			d.Resource("icons/go.svg", []byte("<svg/>"))
			a := buildDefinition(t, d)
			root := filepath.Join(t.TempDir(), "plugins with spaces")
			dir, err := a.installBinary(InstallOptions{Directory: root}, []byte("first executable"), goos)
			if err != nil {
				t.Fatal(err)
			}
			file := filepath.Join(dir, "vizi-plugin.json")
			b, err := os.ReadFile(file)
			if err != nil {
				t.Fatal(err)
			}
			first := decodeManifest(t, b)
			if err = first.ValidateFiles(os.DirFS(dir)); err != nil {
				t.Fatal(err)
			}
			path1 := filepath.Join(dir, filepath.FromSlash(first.Entry.Command[0]))
			if goos == "windows" && filepath.Ext(path1) != ".exe" {
				t.Fatal(path1)
			}
			if err = os.WriteFile(filepath.Join(dir, "notes.txt"), []byte("keep"), 0644); err != nil {
				t.Fatal(err)
			}
			settings := filepath.Join(dir, "settings")
			if _, err := os.Stat(settings); !os.IsNotExist(err) {
				t.Fatalf("install created settings: %v", err)
			}
			// Simulate the host-created folder and the plugin's saved settings.
			if err := os.Mkdir(settings, 0755); err != nil {
				t.Fatal(err)
			}
			preferences := filepath.Join(settings, "preferences.json")
			if err := os.WriteFile(preferences, []byte(`{"keep":true}`), 0644); err != nil {
				t.Fatal(err)
			}
			_, err = a.installBinary(InstallOptions{Directory: root}, []byte("second executable"), goos)
			if err != nil {
				t.Fatal(err)
			}
			b, err = os.ReadFile(file)
			if err != nil {
				t.Fatal(err)
			}
			second := decodeManifest(t, b)
			if first.Entry.Command[0] == second.Entry.Command[0] {
				t.Fatal("update overwrote running executable")
			}
			old, err := os.ReadFile(path1)
			if err != nil || string(old) != "first executable" {
				t.Fatal(string(old), err)
			}
			if notes, err := os.ReadFile(filepath.Join(dir, "notes.txt")); err != nil || string(notes) != "keep" {
				t.Fatal("removed unrelated file", err)
			}
			if saved, err := os.ReadFile(preferences); err != nil || string(saved) != `{"keep":true}` {
				t.Fatal("changed saved settings", err)
			}
			stat, err := os.Stat(file)
			if err != nil {
				t.Fatal(err)
			}
			_, err = a.installBinary(InstallOptions{Directory: root}, []byte("second executable"), goos)
			if err != nil {
				t.Fatal(err)
			}
			stat2, _ := os.Stat(file)
			if !stat.ModTime().Equal(stat2.ModTime()) {
				t.Fatal("unchanged install rewrote manifest")
			}
			if _, err = os.Stat(filepath.Join(dir, ".vizi-install-lock")); !os.IsNotExist(err) {
				t.Fatal("left lock", err)
			}
		})
	}
}
func TestDefinitionInstallRefusesUnmarkedDirectoryAndSymlink(t *testing.T) {
	a := buildDefinition(t, definition())
	root := t.TempDir()
	dir := filepath.Join(root, a.m.ID)
	if err := os.Mkdir(dir, 0755); err != nil {
		t.Fatal(err)
	}
	old := []byte("user manifest")
	file := filepath.Join(dir, "vizi-plugin.json")
	if err := os.WriteFile(file, old, 0644); err != nil {
		t.Fatal(err)
	}
	if _, err := a.installBinary(InstallOptions{Directory: root}, []byte("binary"), runtime.GOOS); err == nil {
		t.Fatal("overwrote unmarked directory")
	}
	if b, _ := os.ReadFile(file); !bytes.Equal(b, old) {
		t.Fatal("changed old manifest")
	}
	if _, err := a.installBinary(InstallOptions{Directory: root, Force: true}, []byte("binary"), runtime.GOOS); err != nil {
		t.Fatal(err)
	}
	// Refuse a package whose binary directory points elsewhere, even with Force.
	root = t.TempDir()
	dir = filepath.Join(root, a.m.ID)
	if err := os.Mkdir(dir, 0755); err != nil {
		t.Fatal(err)
	}
	outside := t.TempDir()
	if err := os.Symlink(outside, filepath.Join(dir, "bin")); err != nil {
		t.Skipf("symlinks unavailable: %v", err)
	}
	if _, err := a.installBinary(InstallOptions{Directory: root, Force: true}, []byte("binary"), runtime.GOOS); err == nil {
		t.Fatal("followed directory symlink")
	}
	entries, err := os.ReadDir(outside)
	if err != nil || len(entries) != 0 {
		t.Fatal(entries, err)
	}
}
func TestDefinitionFailedPublishPreservesManifest(t *testing.T) {
	d := definition()
	d.Resource("support.txt", []byte("resource"))
	a := buildDefinition(t, d)
	root := t.TempDir()
	dir, err := a.installBinary(InstallOptions{Directory: root}, []byte("one"), runtime.GOOS)
	if err != nil {
		t.Fatal(err)
	}
	file := filepath.Join(dir, "vizi-plugin.json")
	old, _ := os.ReadFile(file)
	if err = os.Remove(filepath.Join(dir, "support.txt")); err != nil {
		t.Fatal(err)
	}
	if err = os.Mkdir(filepath.Join(dir, "support.txt"), 0755); err != nil {
		t.Fatal(err)
	}
	if _, err = a.installBinary(InstallOptions{Directory: root}, []byte("two"), runtime.GOOS); err == nil {
		t.Fatal("accepted directory as resource")
	}
	if b, _ := os.ReadFile(file); !bytes.Equal(b, old) {
		t.Fatal("published incomplete installation")
	}
}

// OpenRoot can legitimately retain a relative name even on newer Go versions.
// This fixture catches accidental use of that name on every supported toolchain.
func TestPackageDiagnosticsUseExplicitDirectory(t *testing.T) {
	scratch := t.TempDir()
	t.Chdir(scratch)
	dir := filepath.Join(scratch, "package")
	if err := os.Mkdir(dir, 0755); err != nil {
		t.Fatal(err)
	}
	root, err := os.OpenRoot("package")
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	if filepath.IsAbs(root.Name()) {
		t.Fatal("fixture must have a relative root name")
	}
	if err := root.WriteFile("bin", []byte("conflict"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := root.Mkdir("resource", 0755); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name, path string
		run        func() error
	}{
		{"parent", filepath.Join(dir, "bin"), func() error { return packageParents(root, dir, "bin/plugin") }},
		{"file", filepath.Join(dir, "resource"), func() error { return writePackageFile(root, dir, "resource", []byte("data"), 0600) }},
		{"resources", filepath.Join(dir, "resource"), func() error { return writeResources(root, dir, map[string][]byte{"resource": []byte("data")}) }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			err := tc.run()
			var failure *packageFailure
			if !errors.As(err, &failure) {
				t.Fatalf("missing contextual failure: %v", err)
			}
			if failure.path != tc.path {
				t.Fatalf("diagnostic path = %q, want %q", failure.path, tc.path)
			}
		})
	}
}
