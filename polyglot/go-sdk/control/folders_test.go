package control

import (
	"errors"
	"os"
	"path/filepath"
	"testing"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func TestSettingsDir(t *testing.T) {
	root := t.TempDir()
	missing := filepath.Join(root, "not created")
	file := filepath.Join(root, "ordinary file")
	if err := os.WriteFile(file, []byte("unchanged"), 0600); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name, path   string
		unset, valid bool
	}{
		{name: "unset", unset: true},
		{name: "empty"},
		{name: "missing", path: missing, valid: true},
		{name: "blocked parent", path: filepath.Join(file, "child")},
		{name: "file", path: file},
		{name: "directory", path: root, valid: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("VIZI_PLUGIN_SETTINGS_DIR", tc.path)
			if tc.unset {
				if err := os.Unsetenv("VIZI_PLUGIN_SETTINGS_DIR"); err != nil {
					t.Fatal(err)
				}
			}
			got, err := SettingsDir()
			if tc.valid {
				if err != nil || got != tc.path {
					t.Fatalf("%q: %v", got, err)
				}
				return
			}
			var unavailable *SettingsDirUnavailableError
			if got != "" || !errors.As(err, &unavailable) || unavailable.Path != tc.path {
				t.Fatalf("%q: %v", got, err)
			}
			if tc.path != "" && unavailable.Err == nil {
				t.Fatal("filesystem cause lost")
			}
		})
	}
	if st, err := os.Stat(missing); err != nil || !st.IsDir() {
		t.Fatalf("settings folder not created: %v", err)
	}
	t.Setenv("VIZI_PLUGIN_SETTINGS_DIR", missing)
	if again, err := SettingsDir(); err != nil || again != missing {
		t.Fatal("first-use creation not idempotent", again, err)
	}
	if data, err := os.ReadFile(file); err != nil || string(data) != "unchanged" {
		t.Fatalf("accessor changed file: %q, %v", data, err)
	}
}

func TestFileStateDirNilAndEmpty(t *testing.T) {
	for _, source := range []interface{ GetFileStateDir() string }{
		nil, (*pb.ViewState)(nil), (*pb.OpenReader)(nil), (*pb.OpenExport)(nil),
		&pb.ViewState{}, &pb.OpenReader{}, &pb.OpenExport{},
	} {
		if got := FileStateDir(source); got != nil {
			t.Fatalf("%T: absent folder became %q", source, *got)
		}
	}
}

func TestViewStateFolderFollowsDatasetOverWire(t *testing.T) {
	p := openPeer(t, Config{}, nil)
	first, second := filepath.Join(t.TempDir(), "file one"), filepath.Join(t.TempDir(), "file two")
	for _, path := range []string{first, "", second} {
		done := make(chan *pb.ViewState, 1)
		failed := make(chan error, 1)
		go func() {
			view, err := p.client.GetViewState(p.ctx)
			done <- view
			failed <- err
		}()
		request := p.recv(t)
		p.send(t, &pb.HostMsg{Id: request.Id, Msg: &pb.HostMsg_ViewState{ViewState: &pb.ViewState{FileStateDir: path}}})
		view := <-done
		if err := <-failed; err != nil {
			t.Fatal(err)
		}
		folder := FileStateDir(view)
		if path == "" {
			if folder != nil {
				t.Fatal("remote dataset must have no folder")
			}
		} else {
			if folder == nil || *folder != path {
				t.Fatalf("view folder: %v, want %q", folder, path)
			}
			*folder = "caller-owned copy"
			if view.FileStateDir != path {
				t.Fatal("accessor exposes mutable message storage")
			}
		}
	}
	for _, path := range []string{first, second} {
		if _, err := os.Stat(path); !os.IsNotExist(err) {
			t.Fatalf("view created state directory: %v", err)
		}
	}
}
