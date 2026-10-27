package plugin

import (
	"strings"
	"testing"
)

func TestLabelOptionsInGeneratedManifest(t *testing.T) {
	options := LabelOptions{TextFormat: "rich"}
	rich := LabelWithOptions("rich", "<b>formatted</b>", options)
	options.TextFormat = "html" // The constructor owns its format.
	d := definition()
	d.Panel(PanelSpec{ID: "p", Title: "P", Root: Column("root",
		Label("plain", "<b>literal</b>"),
		LabelWithOptions("default", "<i>literal</i>", LabelOptions{}),
		LabelWithOptions("explicit", "<i>literal</i>", LabelOptions{TextFormat: "plain"}),
		ListRows(ListSpec{ID: "rows"}, rich),
	)}, nil)
	app := buildDefinition(t, d)
	data, err := app.Manifest("plugin")
	if err != nil {
		t.Fatal(err)
	}
	children := decodeManifest(t, data).Contributions.Panels[0].Root.Children
	if children[0].TextFormat != nil || children[1].TextFormat != nil || children[0].Text != "<b>literal</b>" {
		t.Fatal("default label changed", string(data))
	}
	if children[2].TextFormat == nil || *children[2].TextFormat != "plain" {
		t.Fatal("lost explicit plain format", string(data))
	}
	cell := children[3].Row[0]
	if cell.TextFormat == nil || *cell.TextFormat != "rich" || cell.Text != "<b>formatted</b>" {
		t.Fatal("lost rich row label", string(data))
	}
}

func TestLabelOptionsValidatedByBuild(t *testing.T) {
	for _, tc := range []struct {
		kind, format, diagnostic string
	}{
		{"label", "html", "label 'a' text_format must be one of ['plain', 'rich']"},
		{"image", "html", "image 'a' text_format must be one of ['plain', 'rich']"},
		{"image", "plain", "image 'a' does not take text_format; only a label does"},
		{"image", "rich", "image 'a' does not take text_format; only a label does"},
	} {
		t.Run(tc.kind+"/"+tc.format, func(t *testing.T) {
			d := definition()
			w := LabelWithOptions("a", "text", LabelOptions{TextFormat: tc.format})
			w.spec.Kind = tc.kind
			d.Panel(PanelSpec{ID: "p", Title: "P", Root: w}, nil)
			if _, err := d.Build(); err == nil || !strings.Contains(err.Error(), tc.diagnostic) {
				t.Fatalf("got %v; want %s", err, tc.diagnostic)
			}
		})
	}
}
