package main

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"reflect"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/geometry"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/payload"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/plugin"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func TestBandEdges(t *testing.T) {
	for _, tc := range []struct {
		v, threshold float64
		want         byte
	}{
		{math.Nextafter(400, 0), 400, 0}, {400, 400, 1}, {599, 400, 1}, {600, 400, 2}, {800, 400, 3}, {1e300, 400, 3},
		{math.NaN(), 400, 0}, {math.Inf(-1), 400, 0}, {math.Inf(1), 400, 3}, {0, 0, 1}, {200, 0, 2}, {400, 0, 3},
	} {
		if got := band(tc.v, tc.threshold); got != tc.want {
			t.Errorf("band(%g,%g)=%d want %d", tc.v, tc.threshold, got, tc.want)
		}
	}
	tensor, _ := payload.Encode([]uint16{399, 400, 599, 600, 799, 800}, []uint32{1, 2, 3}, true)
	got, err := labelTensor(tensor, 400)
	if err != nil || !reflect.DeepEqual(got, []byte{0, 1, 1, 2, 2, 3}) {
		t.Fatal(got, err)
	}
}
func TestTightBox(t *testing.T) {
	labels := make([]byte, 3*4*5)
	labels[(1*4+2)*5+1] = 1
	labels[(2*4+3)*5+3] = 3
	c, err := tight(labels, []uint32{3, 4, 5})
	if err != nil {
		t.Fatal(err)
	}
	if c.PositionZYX != (geometry.ZYX{1, 2, 1}) || !reflect.DeepEqual(c.Labels.Shape(), []uint32{2, 2, 3}) {
		t.Fatal(c)
	}
	want := make([]byte, 12)
	want[0] = 1
	want[11] = 3
	if !reflect.DeepEqual(c.Labels.Bytes(), want) {
		t.Fatal(c.Labels.Bytes())
	}
	c, err = tight([]byte{1, 2, 3, 1}, []uint32{1, 2, 2})
	if err != nil || c.PositionZYX != (geometry.ZYX{}) || !reflect.DeepEqual(c.Labels.Bytes(), []byte{1, 2, 3, 1}) {
		t.Fatal(c, err)
	}
	c, err = tight(make([]byte, 4), []uint32{1, 2, 2})
	if err != nil || c != nil {
		t.Fatal("empty must be absent", c, err)
	}
}
func TestFootprintUnion(t *testing.T) {
	union := make([]byte, 5)
	for _, values := range [][]float64{{400, 0, 0, 0, math.NaN()}, {0, 600, 0, 0, math.NaN()}, {0, 0, 800, 0, math.NaN()}} {
		v, _ := payload.Encode(values, []uint32{1, 1, 5}, true)
		labels, err := labelTensor(v, 400)
		if err != nil {
			t.Fatal(err)
		}
		if err := unionInto(union, labels); err != nil {
			t.Fatal(err)
		}
	}
	if !reflect.DeepEqual(union, []byte{1, 1, 1, 0, 0}) {
		t.Fatal(union)
	}
}
func TestTables(t *testing.T) {
	lut, visible := bandTable([4]bool{2: true})
	rows := map[int][4]uint8{1: {60, 120, 255, 255}, 2: {255, 200, 0, 0}, 3: {255, 60, 60, 255}}
	for i, row := range lut {
		if row != rows[i] {
			t.Fatal(i, row)
		}
	}
	if !reflect.DeepEqual(visible, map[uint32]bool{1: true, 2: false, 3: true}) || !reflect.DeepEqual(bandNames, map[uint32]string{1: "dim", 2: "mid", 3: "bright"}) {
		t.Fatal(visible, bandNames)
	}
	footprint := footprintTable()
	for i, row := range footprint {
		want := [4]uint8{}
		if i == 1 {
			want = [4]uint8{200, 200, 200, 255}
		}
		if row != want {
			t.Fatal(i, row)
		}
	}
}
func TestCommandsAndSpelling(t *testing.T) {
	a, _ := newMovie()
	var patches []string
	values := map[string]*pb.Value{}
	a.patch = func(_ *control.Client, widget string, value *pb.Value) error {
		patches = append(patches, widget)
		values[widget] = value
		return nil
	}
	ctx := context.Background()
	for _, args := range []plugin.Args{{"value": plugin.Int(400)}, {"value": plugin.Float(400)}, {"value": plugin.String(" 400 ")}} {
		got, err := a.setThreshold(ctx, nil, args)
		if err != nil || got.Message != "threshold 400; 0 wanted volume(s) to serve again" {
			t.Fatal(got, err)
		}
	}
	got, err := a.setThreshold(ctx, nil, plugin.Args{"value": plugin.Float(400.25)})
	if err != nil || got.Message != "threshold 400.25; 0 wanted volume(s) to serve again" {
		t.Fatal(got, err)
	}
	for _, args := range []plugin.Args{{"value": plugin.Int(-1)}, {"value": plugin.String("high")}, {"value": plugin.String("NaN")}, {"value": plugin.String("+Inf")}, {"value": plugin.String("-1")}, {"value": plugin.Bool(true)}, {"value": plugin.Float(math.NaN())}, {"value": plugin.Float(math.Inf(1))}, {}} {
		if _, err := a.setThreshold(ctx, nil, args); err == nil {
			t.Fatal("bad threshold accepted", args)
		}
		if a.threshold != 400.25 || values["threshold"].GetS() != "400.25" || len(patches) != 4 {
			t.Fatal("refusal changed state")
		}
	}
	for _, visible := range []bool{false, true} {
		got, err := a.setVisible(ctx, nil, plugin.Args{"band": plugin.Int(2), "visible": plugin.Bool(visible)})
		state := "hidden"
		if visible {
			state = "visible"
		}
		if err != nil || got.Message != "band 2 (mid) "+state {
			t.Fatal(got, err)
		}
	}
	for band := int64(1); band <= 3; band++ {
		for _, visible := range []bool{false, true} {
			result, err := a.bandCommand(band)(ctx, nil, plugin.Args{"value": plugin.Bool(visible)})
			if err != nil || result.Message == "" || a.hidden[band] == visible || values[fmt.Sprintf("band_%d", band)].GetB() != visible {
				t.Fatal(result, err)
			}
		}
	}
	before := len(patches)
	if _, err := a.bandCommand(3)(ctx, nil, plugin.Args{"value": plugin.String("no")}); err == nil {
		t.Fatal("text checkbox accepted")
	}
	for _, args := range []plugin.Args{{"band": plugin.Int(7), "visible": plugin.Bool(false)}, {"band": plugin.Int(2), "visible": plugin.String("false")}, {"band": plugin.Float(2), "visible": plugin.Bool(false)}, {"band": plugin.Bool(true), "visible": plugin.Bool(false)}} {
		if _, err := a.setVisible(ctx, nil, args); err == nil {
			t.Fatal("bad visibility accepted", args)
		}
		if a.hidden != ([4]bool{}) || len(patches) != before {
			t.Fatal("refusal changed state")
		}
	}
	if len(a.sent) != 0 {
		t.Fatal("commands sent a table before first want")
	}
}
func TestManifest(t *testing.T) {
	data, err := os.ReadFile("vizi-plugin.json")
	if err != nil {
		t.Fatal(err)
	}
	var m struct {
		ManifestVersion int `json:"manifest_version"`
		ID              string
		Entry           struct{ Command []string }
		Permissions     []string
		Contributions   struct {
			Overlays []map[string]any
			Commands []struct{ ID string }
		}
	}
	if err := json.Unmarshal(data, &m); err != nil {
		t.Fatal(err)
	}
	if m.ManifestVersion != 1 || m.ID != "org.example.movie_seg" || !reflect.DeepEqual(m.Entry.Command, []string{"./movie-seg"}) || !reflect.DeepEqual(m.Permissions, []string{"data_access"}) {
		t.Fatal(m)
	}
	if len(m.Contributions.Overlays) != 2 || len(m.Contributions.Commands) != 5 {
		t.Fatal(m)
	}
	for i, o := range m.Contributions.Overlays {
		ids := []string{"bands", "footprint"}
		times := []string{"per_t", "static"}
		if o["id"] != ids[i] || o["chunked"] != true || o["time"] != times[i] || o["segment_mib"] != nil {
			t.Fatal(o)
		}
	}
	if m.Contributions.Commands[0].ID != "movie_seg.threshold" || m.Contributions.Commands[1].ID != "movie_seg.band_visible" {
		t.Fatal(m)
	}
}
