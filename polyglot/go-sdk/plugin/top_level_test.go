package plugin

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
)

// The writer states the SDK release that built the plugin, and min_vizi: the
// author's, or vizi 1.0.0 (the lowest protocol-1.0 release) when none is set.
func TestManifestWritesSDKVersionAndMinVizi(t *testing.T) {
	top := func(info Info) map[string]any {
		t.Helper()
		d := NewDefinition(info)
		d.Command(CommandSpec{ID: "run", Title: "Run", Menu: ToolsMenu()}, func(context.Context, *control.Client, Args) (Result, error) { return Result{}, nil })
		app, err := d.Build()
		if err != nil {
			t.Fatal(err)
		}
		b, err := app.Manifest("./plugin")
		if err != nil {
			t.Fatal(err)
		}
		var doc map[string]any
		if err := json.Unmarshal(b, &doc); err != nil {
			t.Fatal(err)
		}
		return doc
	}
	doc := top(Info{ID: "org.example.top", Name: "Top", Version: "1"})
	if doc["sdk_version"] != control.SDKVersion || doc["manifest_version"] != 1.0 {
		t.Fatal(doc)
	}
	if doc["min_vizi"] != "1.0.0" {
		t.Fatalf("min_vizi without one set: %v", doc["min_vizi"])
	}
	if doc = top(Info{ID: "org.example.top", Name: "Top", Version: "1", MinVizi: "0.9.7"}); doc["min_vizi"] != "0.9.7" {
		t.Fatal(doc)
	}
	for info, want := range map[Info]string{
		{ID: "org.example.top", Name: "Top", Version: "1", MinVizi: "latest"}: "min_vizi 'latest' is not a PEP 440 version",
		{ID: "Top", Name: "Top", Version: "1"}:                                "id 'Top' is not a plugin id",
	} {
		d := NewDefinition(info)
		d.Command(CommandSpec{ID: "run", Title: "Run", Menu: ToolsMenu()}, func(context.Context, *control.Client, Args) (Result, error) { return Result{}, nil })
		if _, err := d.Build(); err == nil || !strings.Contains(err.Error(), want) {
			t.Fatalf("%+v: %v", info, err)
		}
	}
}
