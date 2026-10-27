package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"image/png"
	"math"
	"os"
	"reflect"
	"testing"
	"testing/synctest"
)

func TestMeanRoundingWithoutIntermediateOverflow(t *testing.T) {
	for _, tc := range []struct {
		sum, count uint64
		want       string
	}{
		{17, 16, "1.063"}, {0, 0, ""}, {0, 9, "0.000"}, {1, 3, "0.333"}, {2, 3, "0.667"},
		{math.MaxUint64, 1, "18446744073709551615.000"},
		{math.MaxUint64, math.MaxUint64, "1.000"},
	} {
		if got := meanText(measurement{sum: tc.sum, count: tc.count}); got != tc.want {
			t.Fatalf("%d/%d: %s != %s", tc.sum, tc.count, got, tc.want)
		}
	}
}
func TestSwatchIndexAndPNG(t *testing.T) {
	for _, tc := range []struct {
		sum, count uint64
		want       int
	}{{1, 0, 0}, {511, 2, 0}, {512, 2, 1}, {65535, 1, 255}, {math.MaxUint64, 1, 255}} {
		if got := swatchIndex(measurement{sum: tc.sum, count: tc.count}); got != tc.want {
			t.Fatal(tc, got)
		}
	}
	data, err := swatchPNG([]byte{7, 29, 203})
	if err != nil {
		t.Fatal(err)
	}
	img, err := png.Decode(bytes.NewReader(data))
	if err != nil {
		t.Fatal(err)
	}
	if img.Bounds().Dx() != 8 || img.Bounds().Dy() != 8 {
		t.Fatal(img.Bounds())
	}
	for y := range 8 {
		for x := range 8 {
			r, g, b, a := img.At(x, y).RGBA()
			if r != 7*257 || g != 29*257 || b != 203*257 || a != 65535 {
				t.Fatal(r, g, b, a)
			}
		}
	}
}
func TestRowOrderUsesDisplayNameThenID(t *testing.T) {
	rows := map[string]row{"b": {id: "b"}, "2": {id: "2", name: "a"}, "1": {id: "1", name: "a"}, "z": {id: "z"}, "é": {id: "é"}}
	var got []string
	for _, r := range ordered(rows) {
		got = append(got, r.id)
	}
	if !reflect.DeepEqual(got, []string{"1", "2", "b", "z", "é"}) {
		t.Fatal(got)
	}
}
func TestCSVRawNamesQuotingAndLF(t *testing.T) {
	rows := []row{{id: "r1", name: "comma, and \"quote\"", measurement: measurement{count: 16, sum: 17}}, {id: "r2", name: "line\nbreak"}, {id: "r3"}}
	data, err := tableCSV(rows, 3, 1)
	if err != nil {
		t.Fatal(err)
	}
	want := "region_id,name,t,channel,voxels,mean\nr1,\"comma, and \"\"quote\"\"\",3,1,16,1.063\nr2,\"line\nbreak\",3,1,0,\nr3,,3,1,0,\n"
	if string(data) != want {
		t.Fatalf("%q != %q", data, want)
	}
}

func TestCSVExactQuotingAndUnmeasuredRows(t *testing.T) {
	rows := []row{
		{id: "space", name: " leading space"},
		{id: "dot", name: `\.`},
		{id: "cr", name: "line\rbreak"},
		{id: "float", name: "still named", unmeasured: true, measurement: measurement{count: 7, sum: 21}},
	}
	data, err := tableCSV(rows, 0, 0)
	want := "region_id,name,t,channel,voxels,mean\nspace, leading space,0,0,0,\ndot,\\.,0,0,0,\ncr,\"line\rbreak\",0,0,0,\nfloat,still named,0,0,,\n"
	if err != nil || string(data) != want {
		t.Fatalf("%q, %v; want %q", data, err, want)
	}
}
func TestBestFrameExactComparisonSkipsEmptyAndKeepsEarliestTie(t *testing.T) {
	for _, tc := range []struct {
		frames []measurement
		want   uint32
		found  bool
	}{
		{nil, 0, false},
		{[]measurement{{}, {count: 1, sum: 7}, {count: 2, sum: 14}, {count: 0, sum: 99}}, 1, true},
		{[]measurement{{count: math.MaxUint64, sum: math.MaxUint64 - 1}, {count: math.MaxUint64 - 1, sum: math.MaxUint64}}, 1, true},
	} {
		got, found := bestFrame(tc.frames)
		if found != tc.found || found && got != tc.want {
			t.Fatal(got, found, tc)
		}
	}
}
func TestInstalledManifestMatchesDeclaredInterface(t *testing.T) {
	a := &application{}
	app, err := declaration(a)
	if err != nil {
		t.Fatal(err)
	}
	generated, err := app.Manifest("./region-quant")
	if err != nil {
		t.Fatal(err)
	}
	pinned, err := os.ReadFile("vizi-plugin.json")
	if err != nil {
		t.Fatal(err)
	}
	var g, p any
	if err := json.Unmarshal(generated, &g); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(pinned, &p); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(g, p) {
		t.Fatal("manifest drift: regenerate with ./region-quant --manifest")
	}
}

func TestStateLockCancellationDoesNotStealOwnership(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		a := &application{ready: make(chan struct{}), gate: make(chan struct{}, 1)}
		close(a.ready)
		if err := a.lock(context.Background()); err != nil {
			t.Fatal(err)
		}
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		done := make(chan error, 1)
		go func() { done <- a.lock(ctx) }()
		synctest.Wait() // the contender is blocked behind the current owner
		select {
		case err := <-done:
			t.Fatalf("waiter did not wait: %v", err)
		default:
		}
		cancel()
		if err := <-done; !errors.Is(err, context.Canceled) {
			t.Fatal(err)
		}
		if len(a.gate) != 1 {
			t.Fatal("canceled waiter lost owner's lock")
		}
		a.unlock()
		if err := a.lock(context.Background()); err != nil {
			t.Fatal(err)
		}
		a.unlock()
	})
}

func TestTourResumeUsesDisplayNameThenID(t *testing.T) {
	last := row{id: "b", name: "same"}
	for _, tc := range []struct {
		r    row
		want bool
	}{
		{row{id: "a", name: "same"}, false}, {last, false}, {row{id: "c", name: "same"}, true},
		{row{id: "z", name: "before"}, false}, {row{id: "a", name: "later"}, false},
		{row{id: "a", name: "zebra"}, true}, {row{id: "same"}, true},
	} {
		if got := afterTourRow(tc.r, last); got != tc.want {
			t.Fatalf("resume after %#v: %#v = %v", last, tc.r, got)
		}
	}
}
