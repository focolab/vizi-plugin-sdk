package plugin

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/manifest"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func definition() *Definition {
	return NewDefinition(Info{ID: "org.example.declared", Name: "Declared", Version: "0.1"})
}
func noopCommand(context.Context, *control.Client, Args) (Result, error) { return Result{}, nil }
func buildDefinition(t *testing.T, d *Definition) *Application {
	t.Helper()
	a, err := d.Build()
	if err != nil {
		t.Fatal(err)
	}
	return a
}
func decodeManifest(t *testing.T, b []byte) manifest.Manifest {
	t.Helper()
	var m manifest.Manifest
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatal(err)
	}
	return m
}

func TestDefinitionGeneratesAndDispatchesSameInterface(t *testing.T) {
	d := definition()
	called, opened := false, false
	cmd := d.Command(CommandSpec{ID: "clear", Title: "Clear", Menu: ToolsMenu()}, func(context.Context, *control.Client, Args) (Result, error) {
		called = true
		return Result{Message: "cleared"}, nil
	})
	panel := d.Panel(PanelSpec{ID: "p", Title: "Controls", Root: Column("root", Button("clear-button", "Clear", cmd), Checkbox("check", "Flag", false, cmd), LineEdit("text", "", "", cmd), Dropdown("choice", []string{"a", "b"}, "a", cmd), Slider(SliderSpec{ID: "slider", Min: 0, Max: 10, Step: 1, Value: 0}, cmd), Image("image"))}, func(context.Context, *control.Client) error { opened = true; return nil })
	overlay := d.Overlay(OverlaySpec{ID: "marks", SegmentMiB: 4})
	d.Needs(DataAccess, DataAccess)
	a := buildDefinition(t, d)
	b, err := a.Manifest("./plugin")
	if err != nil {
		t.Fatal(err)
	}
	m := decodeManifest(t, b)
	if m.ManifestVersion != 1 {
		t.Fatalf("generated manifest_version = %d, want 1", m.ManifestVersion)
	}
	if m.Contributions.Panels[0].Root.Children[0].Command != cmd.ID() || len(m.Contributions.Commands) != 1 || len(m.Permissions) != 1 {
		t.Fatal(string(b))
	}
	cfg, err := a.Configure(control.Plugin{})
	if err != nil {
		t.Fatal(err)
	}
	out, err := cfg.Control.HandleRequest(context.Background(), nil, &pb.HostMsg{Msg: &pb.HostMsg_InvokeCommand{InvokeCommand: &pb.InvokeCommand{CommandId: m.Contributions.Commands[0].ID}}})
	if err != nil || !called || out.GetCommandDone().Message != "cleared" {
		t.Fatal(out, err)
	}
	err = cfg.Control.OnMessage(context.Background(), nil, &pb.HostMsg{Msg: &pb.HostMsg_PanelOpened{PanelOpened: &pb.PanelOpened{PanelId: m.Contributions.Panels[0].ID}}})
	if err != nil || !opened {
		t.Fatal(err, opened)
	}
	if cfg.Control.PluginID != m.ID || cfg.Control.BuildIdentity != m.ID+" "+m.Version {
		t.Fatal(cfg.Control)
	}
	if !reflect.DeepEqual(cfg.Memory.Contributions, map[string][]string{"O": {overlay.ID()}, "P": {panel.ID()}}) {
		t.Fatal(cfg.Memory.Contributions)
	}
}
func TestDefinitionSnapshotsAndSeals(t *testing.T) {
	d := definition()
	menu := "tools"
	icon := &PaneButton{Icon: "icons/go.svg"}
	data := []byte("<svg></svg>")
	d.Resource(icon.Icon, data)
	d.Command(CommandSpec{ID: "go", Pane: icon, Menu: &menu}, noopCommand)
	extensions := []string{"TIF"}
	fallback := []string{"...OME.H5"}
	d.Reader(ReaderSpec{ID: "reader", Extensions: extensions, FallbackExtensions: fallback}, ReaderHandlers{Open: func(context.Context, *control.Client, *pb.OpenReader) (*pb.ReaderOpened, error) {
		return &pb.ReaderOpened{Handle: "local"}, nil
	}})
	a := buildDefinition(t, d)
	before, _ := a.Manifest("./plugin")
	menu = "bad"
	icon.Icon = "missing.svg"
	data[0] = 'x'
	extensions[0] = "bad"
	fallback[0] = "bad"
	after, _ := a.Manifest("./plugin")
	if !bytes.Equal(before, after) {
		t.Fatal("built definition changed")
	}
	m := decodeManifest(t, before)
	if m.Contributions.Readers[0].Extensions[0] != ".tif" || m.Contributions.Readers[0].FallbackExtensions[0] != ".ome.h5" {
		t.Fatal(m)
	}
	again, err := d.Build()
	if err != nil || again != a {
		t.Fatal("Build is not idempotent")
	}
	defer func() {
		if recover() == nil {
			t.Error("registration after Build did not panic")
		}
	}()
	d.Command(CommandSpec{ID: "late"}, noopCommand)
}
func TestDefinitionRefusesUnusableInterfaces(t *testing.T) {
	for _, tc := range []struct {
		name  string
		setup func(*Definition)
	}{
		{"duplicate", func(d *Definition) {
			d.Command(CommandSpec{ID: "same"}, noopCommand)
			d.Command(CommandSpec{ID: "same"}, noopCommand)
		}},
		{"missing handler", func(d *Definition) { d.Command(CommandSpec{ID: "missing"}, nil) }},
		{"foreign command", func(d *Definition) {
			other := definition()
			c := other.Command(CommandSpec{ID: "go"}, noopCommand)
			d.Command(CommandSpec{ID: "go"}, noopCommand)
			d.Panel(PanelSpec{ID: "p", Title: "P", Root: Button("b", "Go", c)}, nil)
		}},
		{"zero command", func(d *Definition) {
			d.Panel(PanelSpec{ID: "p", Title: "P", Root: Button("b", "Go", CommandRef{})}, nil)
		}},
		{"missing icon", func(d *Definition) {
			d.Command(CommandSpec{ID: "go", Pane: &PaneButton{Icon: "missing.svg"}}, noopCommand)
		}},
		{"resource collision", func(d *Definition) {
			d.Resource("icon.svg", []byte("<svg/>"))
			d.Resource("ICON.svg", []byte("<svg/>"))
		}},
		{"reserved path", func(d *Definition) { d.Resource("vizi-plugin.json", []byte("{}")) }},
		{"settings file", func(d *Definition) { d.Resource("settings", nil) }},
		{"settings contents", func(d *Definition) { d.Resource("settings/preferences.json", []byte("{}")) }},
		{"settings case alias", func(d *Definition) { d.Resource("Settings/preferences.json", []byte("{}")) }},
		{"file directory collision", func(d *Definition) { d.Resource("icons", nil); d.Resource("icons/a.svg", []byte("<svg/>")) }},
		{"empty reader extensions", func(d *Definition) {
			d.Reader(ReaderSpec{ID: "r"}, ReaderHandlers{Open: func(context.Context, *control.Client, *pb.OpenReader) (*pb.ReaderOpened, error) { return nil, nil }})
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			d := definition()
			tc.setup(d)
			if _, err := d.Build(); err == nil {
				t.Fatal("accepted invalid definition")
			}
		})
	}
}
func TestDefinitionRejectsRuntimeInterfaceOverrides(t *testing.T) {
	a := buildDefinition(t, definition())
	for _, base := range []control.Plugin{{Control: control.Config{PluginID: "other"}}, {Control: control.Config{BuildIdentity: "other"}}, {Control: control.Config{HandleRequest: func(context.Context, *control.Client, *pb.HostMsg) (*pb.PluginMsg, error) { return nil, nil }}}} {
		if _, err := a.Configure(base); err == nil {
			t.Fatal("accepted second interface")
		}
	}
}
func TestDefinitionToolCancellationKeepsRetiredIdentity(t *testing.T) {
	d := definition()
	called := false
	d.Tool(ToolSpec{ID: "tool"}, ToolHandlers{Event: func(_ *control.Session, e doorbell.Event) { called = e.Cancellation != nil }})
	a := buildDefinition(t, d)
	cfg, err := a.Configure(control.Plugin{})
	if err != nil {
		t.Fatal(err)
	}
	b := &pb.ToolBinding{Tool: 7, ToolId: "tool"}
	if err = cfg.Control.ToolActivated(context.Background(), nil, b); err != nil {
		t.Fatal(err)
	}
	b.ToolId = "mutated"
	cfg.DoorbellHandler(&control.Session{State: control.NewState(nil, nil)}, doorbell.Event{Cancellation: &doorbell.Cancellation{Opening: doorbell.Input{Tool: 7}}})
	if !called {
		t.Fatal("opening-context cancellation lost tool identity")
	}
}
func TestDefinitionManifestCLIHasNoSessionSideEffects(t *testing.T) {
	d := definition()
	d.Command(CommandSpec{ID: "go"}, noopCommand)
	a := buildDefinition(t, d)
	dir := filepath.Join(t.TempDir(), "with spaces")
	var out bytes.Buffer
	err := a.Run(context.Background(), []string{"--manifest=" + dir}, RunOptions{Output: &out, Runtime: func() (control.Plugin, error) {
		t.Fatal("runtime factory called while writing manifest")
		return control.Plugin{}, nil
	}})
	if err != nil {
		t.Fatal(err)
	}
	b, err := os.ReadFile(filepath.Join(dir, "vizi-plugin.json"))
	if err != nil {
		t.Fatal(err)
	}
	if !filepath.IsAbs(decodeManifest(t, b).Entry.Command[0]) {
		t.Fatal("development command is not absolute")
	}
	for _, args := range [][]string{{"--manifest", "--install"}, {"--force"}, {"--manifest="}, {"--unknown"}} {
		if err := a.Run(context.Background(), args, RunOptions{Output: &out}); err == nil {
			t.Fatal(args)
		}
	}
	if !strings.Contains(out.String(), "vizi-plugin.json") {
		t.Fatal(out.String())
	}
}

func TestProgressAndSubscriptionDeclaration(t *testing.T) {
	d := definition()
	d.Subscribe(Epochs, "future_subscription", Epochs)
	d.Panel(PanelSpec{ID: "p", Title: "Work", Root: Progress("bar", .25, true)}, nil)
	a := buildDefinition(t, d)
	raw, err := a.Manifest("./plugin")
	if err != nil {
		t.Fatal(err)
	}
	m := decodeManifest(t, raw)
	if !reflect.DeepEqual(m.Subscriptions, []string{Epochs, "future_subscription"}) || len(m.Contributions.Commands) != 0 || len(m.Contributions.Tools) != 0 {
		t.Fatal(string(raw))
	}
	w := m.Contributions.Panels[0].Root
	if w.Kind != "progress" || w.Value != .25 || !w.Indeterminate || w.Command != "" {
		t.Fatal(w)
	}
}

func TestDefinitionRejectsInvalidChunkBinding(t *testing.T) {
	source := func(context.Context, *control.Session, *pb.ChunkWant) (*Chunk, error) { return nil, nil }
	bind := func(*ChunkOverlay) {}
	for _, name := range []string{"foreign", "zero", "inline", "no source", "nil callback", "duplicate"} {
		t.Run(name, func(t *testing.T) {
			d := definition()
			d.Command(CommandSpec{ID: "start", Title: "Start", Menu: ToolsMenu()}, noopCommand)
			switch name {
			case "foreign":
				ref := definition().ChunkOverlay(OverlaySpec{ID: "o", Time: "static"}, source)
				d.OnChunkOverlay(ref, bind)
			case "zero":
				d.OnChunkOverlay(OverlayRef{}, bind)
			case "inline":
				d.OnChunkOverlay(d.Overlay(OverlaySpec{ID: "o"}), bind)
			case "no source":
				d.OnChunkOverlay(d.Overlay(OverlaySpec{ID: "o", Chunked: true, Time: "static"}), bind)
			case "nil callback":
				d.OnChunkOverlay(d.ChunkOverlay(OverlaySpec{ID: "o", Time: "static"}, source), nil)
			case "duplicate":
				ref := d.ChunkOverlay(OverlaySpec{ID: "o", Time: "static"}, source)
				d.OnChunkOverlay(ref, bind)
				d.OnChunkOverlay(ref, bind)
			}
			if _, err := d.Build(); err == nil {
				t.Fatal("accepted invalid chunk binding")
			}
		})
	}
}
