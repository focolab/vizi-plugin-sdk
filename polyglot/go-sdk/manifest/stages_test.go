package manifest

import (
	"encoding/json"
	"testing"
)

func TestStageContributions(t *testing.T) {
	for _, version := range []int{0, 1} {
		m := valid()
		m.ManifestVersion = version
		m.Contributions.Stages = []Stage{{ID: "go"}, {ID: "other", Name: "Displayed stage"}}
		b, err := m.JSON()
		if err != nil {
			t.Fatal(err)
		}
		var doc map[string]any
		if err := json.Unmarshal(b, &doc); err != nil {
			t.Fatal(err)
		}
		stages := doc["contributions"].(map[string]any)["stages"].([]any)
		if len(stages) != 2 || stages[0].(map[string]any)["id"] != "go" || stages[1].(map[string]any)["name"] != "Displayed stage" {
			t.Fatal(doc)
		}
		if _, supplied := stages[0].(map[string]any)["name"]; supplied {
			t.Fatal("omitted name must use host's ID default")
		}
	}
	for _, stages := range [][]Stage{{{ID: ""}}, {{ID: " \t"}}, {{ID: "a"}, {ID: "a"}}} {
		m := valid()
		m.Contributions.Stages = stages
		if _, err := m.JSON(); err == nil {
			t.Fatalf("accepted %+v", stages)
		}
	}
}
