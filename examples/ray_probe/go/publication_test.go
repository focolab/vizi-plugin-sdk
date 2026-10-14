package main

import (
	"bytes"
	"encoding/json"
	"encoding/xml"
	"image/png"
	"math"
	"os"
	"slices"
	"testing"
)

func TestEllipsoid(t *testing.T) {
	centre := point{17.5, 21.5, 41.5}
	r := ellipsoid(centre, 3)
	if r.TStart != nil || r.TEnd != nil {
		t.Fatal("ROI must be timeless")
	}
	if r.TypeName != "ellipsoid" || len(r.Vertices) != 4 || len(r.Edges) != 12 || r.Compile.NumDisplay != 18 || !slices.Equal(r.Compile.Controls, []int32{0, 1, 2, 3}) {
		t.Fatalf("structure: %v", r)
	}
	controls := make([]point, 4)
	for i, v := range r.Vertices {
		controls[i] = point{v.Position.Z, v.Position.Y, v.Position.X}
		want := centre
		if i > 0 {
			want[i-1] += 3
		}
		if controls[i] != want {
			t.Fatalf("control %d %v", i, controls[i])
		}
	}
	display := make([]point, 18)
	for _, v := range r.Compile.Triplets {
		if v.DisplayVertex < 0 || v.DisplayVertex >= 18 || v.Control < 0 || v.Control >= 4 {
			t.Fatal("bad compile index")
		}
		for a := range 3 {
			display[v.DisplayVertex][a] += v.Coeff * controls[v.Control][a]
		}
	}
	for i := range 6 {
		want := centre
		sign := 1.
		if i%2 == 1 {
			sign = -1
		}
		want[i/2] += sign * 3
		if display[i] != want {
			t.Fatalf("tip %d %v", i, display[i])
		}
	}
	for _, e := range r.Edges {
		if e.Source < 0 || e.Source >= 6 || e.Target < 0 || e.Target >= 6 || e.Control < 6 || e.Control >= 18 || math.Abs(e.Weight-math.Sqrt2/2) > 1e-15 {
			t.Fatalf("arc: %v", e)
		} // Each rational midpoint is on the sphere, not on the control hull.
		squared := 0.
		for a := range 3 {
			p := (display[e.Source][a] + 2*e.Weight*display[e.Control][a] + display[e.Target][a]) / (2 + 2*e.Weight)
			squared += (p - centre[a]) * (p - centre[a])
		}
		if math.Abs(squared-9) > 1e-10 {
			t.Fatalf("arc midpoint squared radius %g", squared)
		}
	}
}

func TestPlotAndLabels(t *testing.T) {
	for _, values := range [][]float64{{math.MaxFloat64, math.MaxFloat64}, {-math.MaxFloat64, math.MaxFloat64}, {0, 0}} {
		if _, err := plotProfile(values, []float64{0, 1}, 1, "distance (um)"); err != nil {
			t.Fatal(err)
		}
	}
	indices := sampleIndices(2049)
	if len(indices) != 1024 || indices[0] != 0 || indices[1023] != 2048 {
		t.Fatal("decimation loses endpoints")
	}
	data, err := plotProfile([]float64{math.Inf(-1), 1, math.NaN(), 3, math.Inf(1)}, []float64{0, 2, 4, 6, 8}, 4, "distance (um)")
	if err != nil {
		t.Fatal(err)
	}
	im, err := png.Decode(bytes.NewReader(data))
	if err != nil || im.Bounds().Dx() != 480 || im.Bounds().Dy() != 240 {
		t.Fatalf("PNG: %v", err)
	}
	r, g, b, _ := im.At(455, 100).RGBA()
	if r != 240*257 || g != 110*257 || b != 0 {
		t.Fatal("coarse maximum is not marked")
	}
	seconds := 2.5
	p := pick{number: 2, position: point{1.5, 2, 3}, value: 9, t: 2, c: 0, seconds: &seconds}
	if got := p.text(point{2, .5, .5}, "um"); got != "Pick 2: (1.5, 2, 3) = (3, 1, 1.5) um, value 9, c=0, t=2 (2.5 s)" {
		t.Fatal(got)
	}
	if countText(0) != "No picks" || countText(1) != "1 pick" || countText(2) != "2 picks" {
		t.Fatal("count wording")
	}
}

// Go has no manifest validator. The shared driver performs the full
// check; these local checks also parse every SVG as XML in the SVG namespace.
func TestManifestAndIcons(t *testing.T) {
	data, err := os.ReadFile("vizi-plugin.json")
	if err != nil {
		t.Fatal(err)
	}
	var m struct {
		Version int               `json:"manifest_version"`
		Engines map[string]string `json:"engines"`
		Entry   struct {
			Command []string `json:"command"`
		} `json:"entry"`
	}
	if err = json.Unmarshal(data, &m); err != nil {
		t.Fatal(err)
	}
	if m.Version != 1 || m.Engines["vizi"] != "1.0" || !slices.Equal(m.Entry.Command, []string{"./ray-probe"}) {
		t.Fatalf("manifest: %+v", m)
	}
	for _, name := range []string{"probe", "undo", "clear"} {
		data, err := os.ReadFile("icons/" + name + ".svg")
		if err != nil {
			t.Fatal(err)
		}
		var svg struct {
			XMLName xml.Name
			ViewBox string     `xml:"viewBox,attr"`
			Paths   []struct{} `xml:"path"`
		}
		if err = xml.Unmarshal(data, &svg); err != nil || svg.XMLName.Local != "svg" || svg.XMLName.Space != "http://www.w3.org/2000/svg" || svg.ViewBox == "" || len(svg.Paths) == 0 {
			t.Fatalf("icon %s: %+v %v", name, svg, err)
		}
	}
}
