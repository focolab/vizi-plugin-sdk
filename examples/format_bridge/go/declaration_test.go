package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

func TestDeclarationMatchesDevelopmentManifest(t *testing.T) {
	app, _, err := newApplication().declarationRuntime()
	if err != nil {
		t.Fatal(err)
	}
	generated, err := app.Manifest("test-entry")
	if err != nil {
		t.Fatal(err)
	}
	pinned, err := os.ReadFile("vizi-plugin.json")
	if err != nil {
		t.Fatal(err)
	}
	var got, want map[string]any
	if err := json.Unmarshal(generated, &got); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(pinned, &want); err != nil {
		t.Fatal(err)
	}
	delete(got, "entry")
	delete(want, "entry")
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("manifest differs outside entry:\ngot %s\nwant %s", generated, pinned)
	}
}

func TestManifestPackagesResources(t *testing.T) {
	app, _, err := newApplication().declarationRuntime()
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	if _, err := app.WriteManifest(dir, "test-entry"); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"vizi-crate-cal.bin"} {
		got, err := os.ReadFile(filepath.Join(dir, name))
		if err != nil {
			t.Fatal(err)
		}
		want, err := os.ReadFile(filepath.Join("..", "fixtures", name))
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(got, want) {
			t.Fatalf("packaged %s differs from source fixture", name)
		}
	}
}
