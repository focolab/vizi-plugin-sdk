package manifest

import (
	"math"
	"strings"
	"testing"
	"testing/fstest"
)

func valid() Manifest {
	return Manifest{ManifestVersion: 1, ID: "org.example.test", Name: "Test", Version: "1", Engines: Engines{Vizi: "1.0"}, Entry: Entry{Command: []string{"./plugin"}}, Contributions: Contributions{Commands: []Command{{ID: "go", Title: "Go", Menu: MenuTools()}}}}
}
func TestOverlayNeedsStarter(t *testing.T) {
	const want = "overlays are drawn only while the plugin runs, and nothing declared here starts it; declare a panel (or a command or tool in the Tools menu or on the pane, a reader, an exporter or a stage)"
	for _, overlay := range []Overlay{{ID: "draw"}, {ID: "chunks", Chunked: true, Time: "static"}} {
		t.Run(overlay.ID, func(t *testing.T) {
			for _, tc := range []struct {
				name   string
				change func(*Contributions)
				starts bool
			}{
				{"nothing", func(c *Contributions) { c.Commands = nil }, false},
				{"hidden command", func(c *Contributions) {}, false},
				{"pane command only", func(c *Contributions) { c.Commands[0].Pane = &PaneButton{Icon: "icons/go.svg"} }, true},
				{"menu command", func(c *Contributions) { c.Commands[0].Menu = MenuTools() }, true},
				{"hidden command with keybinding", func(c *Contributions) { c.Commands[0].DefaultKeybinding = "Ctrl+G" }, false},
				{"later command with keybinding", func(c *Contributions) {
					c.Commands = append(c.Commands, Command{ID: "start", Title: "Start", DefaultKeybinding: "Ctrl+G"})
				}, false},
				{"panel", func(c *Contributions) {
					c.Panels = []Panel{{ID: "p", Title: "P", Root: Widget{Kind: "label", ID: "status"}}}
				}, true},
				{"unlisted tool", func(c *Contributions) { c.Tools = []Tool{{ID: "t"}} }, false},
				{"tool key only", func(c *Contributions) { c.Tools = []Tool{{ID: "t", Triggers: []Trigger{{Key: "P"}}}} }, false},
				{"menu tool", func(c *Contributions) { c.Tools = []Tool{{ID: "t", Menu: MenuTools()}} }, true},
				{"pane tool", func(c *Contributions) { c.Tools = []Tool{{ID: "t", Pane: &PaneButton{Icon: "icons/go.svg"}}} }, true},
				{"reader", func(c *Contributions) { c.Readers = []Reader{{ID: "r", Extensions: []string{".raw"}}} }, true},
				{"exporter", func(c *Contributions) { c.Exporters = []Exporter{{ID: "e", Extension: ".raw"}} }, true},
				{"stage", func(c *Contributions) { c.Stages = []Stage{{ID: "s"}} }, true},
			} {
				t.Run(tc.name, func(t *testing.T) {
					m := valid()
					m.Contributions.Commands[0].Menu = nil
					m.Contributions.Overlays = []Overlay{overlay}
					tc.change(&m.Contributions)
					_, err := m.JSON()
					if tc.starts {
						if err != nil {
							t.Fatal(err)
						}
					} else if err == nil || err.Error() != want {
						t.Fatalf("got %v; want %q", err, want)
					}
					m.Contributions.Overlays = nil
					if _, err := m.JSON(); err != nil {
						t.Fatalf("starter rule applied without overlays: %v", err)
					}
				})
			}
		})
	}
}

func TestGeneratedManifestValidation(t *testing.T) {
	for _, tc := range []struct {
		name   string
		change func(*Manifest)
	}{
		{"version", func(m *Manifest) { m.ManifestVersion = 2 }},
		{"engine", func(m *Manifest) { m.Engines.Vizi = "wat" }},
		{"missing executable", func(m *Manifest) { m.Entry.Command = nil }},
		{"duplicate command", func(m *Manifest) {
			m.Contributions.Commands = append(m.Contributions.Commands, m.Contributions.Commands[0])
		}},
		{"undeclared command", func(m *Manifest) {
			m.Contributions.Panels = []Panel{{ID: "p", Title: "P", Root: Widget{Kind: "button", ID: "b", Command: "absent"}}}
		}},
		{"duplicate widget", func(m *Manifest) {
			m.Contributions.Panels = []Panel{{ID: "p", Title: "P", Root: Widget{Kind: "row", ID: "root", Children: []Widget{{Kind: "image", ID: "i"}, {Kind: "label", ID: "i"}}}}}
		}},
		{"depth", func(m *Manifest) {
			w := Widget{Kind: "label", ID: "leaf"}
			for i := 0; i < 8; i++ {
				w = Widget{Kind: "row", ID: strings.Repeat("r", i+1), Children: []Widget{w}}
			}
			m.Contributions.Panels = []Panel{{ID: "p", Title: "P", Root: w}}
		}},
		{"count", func(m *Manifest) {
			w := Widget{Kind: "row", ID: "root"}
			for i := 0; i < 256; i++ {
				w.Children = append(w.Children, Widget{Kind: "label", ID: strings.Repeat("w", i+1)})
			}
			m.Contributions.Panels = []Panel{{ID: "p", Title: "P", Root: w}}
		}},
		{"nonfinite", func(m *Manifest) {
			m.Contributions.Panels = []Panel{{ID: "p", Title: "P", Root: Widget{Kind: "slider", ID: "s", Command: "go", Value: math.NaN()}}}
		}},
		{"dropdown", func(m *Manifest) {
			m.Contributions.Panels = []Panel{{ID: "p", Title: "P", Root: Widget{Kind: "dropdown", ID: "d", Command: "go", Options: []string{"x"}, Value: "y"}}}
		}},
		{"chunk memory", func(m *Manifest) { m.Contributions.Overlays = []Overlay{{ID: "o", Chunked: true, SegmentMiB: 32}} }},
		{"draw time", func(m *Manifest) { m.Contributions.Overlays = []Overlay{{ID: "o", Time: "per_t"}} }},
		{"bare left", func(m *Manifest) { m.Contributions.Tools = []Tool{{ID: "t", Triggers: []Trigger{{Button: "left"}}}} }},
		{"key modifiers", func(m *Manifest) {
			m.Contributions.Tools = []Tool{{ID: "t", Triggers: []Trigger{{Key: "P", Modifiers: []string{"ctrl"}}}}}
		}},
		{"duplicate trigger", func(m *Manifest) {
			m.Contributions.Tools = []Tool{{ID: "t", Triggers: []Trigger{{Key: "P"}, {Key: "p", Hold: "momentary"}}}}
		}},
		{"duplicate action key", func(m *Manifest) {
			m.Contributions.Tools = []Tool{{ID: "t", Actions: []Action{{ID: "a", Key: "P"}, {ID: "b", Key: "p"}}}}
		}},
		{"action trigger collision", func(m *Manifest) {
			m.Contributions.Tools = []Tool{{ID: "t", Triggers: []Trigger{{Key: "P"}}, Actions: []Action{{ID: "a", Key: "p"}}}}
		}},
		{"unreachable action", func(m *Manifest) { m.Contributions.Tools = []Tool{{ID: "t", Actions: []Action{{ID: "a"}}}} }},
		{"pane traversal", func(m *Manifest) { m.Contributions.Commands[0].Pane = &PaneButton{Icon: "../x.svg"} }},
		{"sticky command", func(m *Manifest) { m.Contributions.Commands[0].Pane = &PaneButton{Icon: "x.svg", Hold: "toggle"} }},
		{"v0 pane", func(m *Manifest) {
			m.ManifestVersion = 0
			m.Contributions.Commands[0].Pane = &PaneButton{Icon: "x.svg"}
		}},
		{"asset", func(m *Manifest) {
			m.Assets = []Asset{{ID: "weights", Filename: "w.bin", Size: 1, SHA256: strings.Repeat("0", 64), Source: AssetSource{Kind: "https", URL: "https://user:pass@example.org/x"}}}
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			m := valid()
			tc.change(&m)
			if _, err := m.JSON(); err == nil {
				t.Fatal("invalid manifest accepted")
			}
		})
	}
}
func TestGeneratedManifestAllContributionKinds(t *testing.T) {
	m := valid()
	m.Contributions.Readers = []Reader{{ID: "r", Extensions: []string{".tif"}}}
	m.Contributions.Exporters = []Exporter{{ID: "e", Extension: ".raw"}}
	m.Contributions.Overlays = []Overlay{{ID: "draw", SegmentMiB: 32}, {ID: "chunks", Chunked: true, Time: "static"}}
	m.Contributions.Panels = []Panel{{ID: "p", Title: "P", Root: Widget{Kind: "button", ID: "b", Command: "go"}}}
	m.Contributions.Tools = []Tool{{ID: "t", Triggers: []Trigger{{Key: "P"}, {Button: "left", Modifiers: []string{"meta"}}}, Actions: []Action{{ID: "a", Key: "N"}}, Pane: &PaneButton{Icon: "icons/t.svg"}}}
	m.Assets = []Asset{{ID: "weights", Filename: "w.bin", Size: 1, SHA256: strings.Repeat("a", 64), Source: AssetSource{Kind: "manual"}}}
	m.Permissions = []string{"data_access", "future_permission"}
	if _, err := m.JSON(); err != nil {
		t.Fatal(err)
	}
	if err := m.ValidateFiles(fstest.MapFS{"icons/t.svg": {Data: []byte("<svg/>")}}); err != nil {
		t.Fatal(err)
	}
	for _, data := range [][]byte{[]byte("bad"), []byte("<SVG/>"), []byte(strings.Repeat(" ", 65537))} {
		if err := m.ValidateFiles(fstest.MapFS{"icons/t.svg": {Data: data}}); err == nil {
			t.Fatal("bad icon accepted")
		}
	}
}
func TestPortablePackagePaths(t *testing.T) {
	for _, s := range []string{"../escape", "/absolute", "C:/foo", "a\\b", "NUL", "con.txt", "a/../b", "a.", "a ", "a:b", ".", "bin//x"} {
		if LocalPath(s) {
			t.Errorf("accepted %q", s)
		}
	}
	for _, s := range []string{"icons/a.svg", "org.example.plugin", "with spaces/icon.png"} {
		if !LocalPath(s) {
			t.Errorf("refused %q", s)
		}
	}
}

func TestSliderRemembersADeclaredStep(t *testing.T) {
	doc := func(slider string) []byte {
		return []byte(`{"manifest_version":1,"id":"org.example.x","name":"X","version":"1","engines":{"vizi":"1.0"},` +
			`"entry":{"command":["x"]},"contributions":{"commands":[{"id":"c","title":"C"}],` +
			`"panels":[{"id":"p","title":"P","root":` + slider + `}]}}`)
	}
	declared, err := Parse(doc(`{"kind":"slider","id":"s","command":"c","float":true,"step":1.0}`))
	if err != nil {
		t.Fatal(err)
	}
	bare, err := Parse(doc(`{"kind":"slider","id":"s","command":"c","float":true}`))
	if err != nil {
		t.Fatal(err)
	}
	if w := declared.Contributions.Panels[0].Root; !w.HasStep() || *w.Step != 1.0 {
		t.Fatalf("a declared step of 1.0 is not remembered: %+v", w)
	}
	if w := bare.Contributions.Panels[0].Root; w.HasStep() {
		t.Fatalf("a slider with no step reports one: %+v", w)
	}
	out, err := declared.JSON()
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(out), `"step":1`) && !strings.Contains(string(out), `"step": 1`) {
		t.Fatalf("the declared step is not written back: %s", out)
	}
}

func TestIntegerSliderStepIsWhole(t *testing.T) {
	doc := func(slider string) []byte {
		return []byte(`{"manifest_version":1,"id":"org.example.x","name":"X","version":"1","engines":{"vizi":"1.0"},` +
			`"entry":{"command":["x"]},"contributions":{"commands":[{"id":"c","title":"C"}],` +
			`"panels":[{"id":"p","title":"P","root":` + slider + `}]}}`)
	}
	_, err := Parse(doc(`{"kind":"slider","id":"level","command":"c","max":100,"step":2.5}`))
	want := `slider "level" step 2.5 is not a whole number; an integer slider moves by whole steps (or declare it float)`
	if err == nil || !strings.HasSuffix(err.Error(), want) {
		t.Fatalf("got %v; want %q", err, want)
	}
	for _, ok := range []string{
		`{"kind":"slider","id":"level","command":"c","max":100,"step":5}`,
		`{"kind":"slider","id":"level","command":"c","max":100,"step":5.0}`,
		`{"kind":"slider","id":"level","command":"c","max":100,"step":2.5,"float":true}`,
	} {
		if _, err := Parse(doc(ok)); err != nil {
			t.Errorf("%s refused: %v", ok, err)
		}
	}
}

// segment_mib is an int in [1, 512]: a boolean is not a number, nor is a
// string, null or a fraction, and each is refused with the reference's text.
func TestSegmentMiBIsAnInt(t *testing.T) {
	doc := func(mib string) []byte {
		return []byte(`{"manifest_version":1,"id":"org.example.x","name":"X","version":"1","engines":{"vizi":"1.0"},` +
			`"entry":{"command":["x"]},"contributions":{"panels":[{"id":"p","title":"P","root":{"kind":"label","id":"l"}}],` +
			`"overlays":[{"id":"o"` + mib + `}]}}`)
	}
	for _, bad := range []string{`true`, `false`, `"32"`, `null`, `32.0`, `3.2e1`, `0`, `513`, `-1`} {
		_, err := Parse(doc(`,"segment_mib":` + bad))
		if err == nil || err.Error() != "segment_mib must be an int in [1, 512]" {
			t.Errorf("segment_mib %s: %v", bad, err)
		}
	}
	for _, ok := range []string{``, `,"segment_mib":1`, `,"segment_mib":512`} {
		if _, err := Parse(doc(ok)); err != nil {
			t.Errorf("%q refused: %v", ok, err)
		}
	}
}
