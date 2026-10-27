package peakroi

import (
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

func TestDeclarationMatchesDevelopmentManifest(t *testing.T) {
	app, _, err := Declaration()
	if err != nil {
		t.Fatal(err)
	}
	generated, err := app.Manifest("test-entry")
	if err != nil {
		t.Fatal(err)
	}
	pinned, err := os.ReadFile("../../vizi-plugin.json")
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
