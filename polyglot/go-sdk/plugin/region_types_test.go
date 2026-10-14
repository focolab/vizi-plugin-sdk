package plugin

import (
	"context"
	"encoding/json"
	"errors"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/manifest"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

func TestCloseOnlySeriesChoiceReleasesListing(t *testing.T) {
	for _, fail := range []bool{false, true} {
		closed, readerClosed := 0, 0
		s := &seriesState{}
		h := ReaderHandlers{OpenSeries: func(_ context.Context, _ *control.Client, q *pb.OpenReader) (ReaderOpenResult, error) {
			if q.Series == nil {
				return ReaderOpenResult{Choice: &SeriesChoice{Entries: []SeriesEntry{{ID: "a"}, {ID: "b"}}, Close: func() error { closed++; return nil }}}, nil
			}
			if closed != 0 {
				t.Error("listing closed before fresh opener acquired its handle")
			}
			if fail {
				return ReaderOpenResult{}, errors.New("open failed")
			}
			return ReaderOpenResult{Opened: &pb.ReaderOpened{Handle: "independent"}}, nil
		}, Close: func(_ context.Context, _ *control.Client, q *pb.CloseReader) (*pb.Closed, error) {
			readerClosed++
			return &pb.Closed{Handle: q.Handle}, nil
		}}
		q := &pb.OpenReader{Uri: "file", ReaderId: "r"}
		if _, err := s.open(context.Background(), nil, h, q, nil); err != nil {
			t.Fatal(err)
		}
		held := s.pending
		q.Series = proto.String("b")
		out, err := s.open(context.Background(), nil, h, q, nil)
		if (err != nil) != fail || closed != 1 || readerClosed != 0 {
			t.Fatal(err, closed, readerClosed)
		}
		s.expire(nil, held)
		s.take(nil, nil)
		if closed != 1 {
			t.Fatal("listing closed twice")
		}
		if !fail {
			if out.GetReaderOpened().Handle != "independent" {
				t.Fatal(out)
			}
			if _, err := h.Close(context.Background(), nil, &pb.CloseReader{Handle: "independent"}); err != nil {
				t.Fatal(err)
			}
		}
	}
}

func TestRegionTypeDeclarationAndHooks(t *testing.T) {
	d := NewDefinition(Info{ID: "test.shapes", Name: "Shapes", Version: "1"})
	d.Needs(ContributeROIs)
	called, ended := false, false
	d.RegionType(RegionTypeSpec{ID: "tetra"}, func(context.Context, *control.Client, *pb.ShapeEdit, bool) (*pb.ShapeResult, error) {
		called = true
		return &pb.ShapeResult{Resolution: pb.Resolution_REJECTED, Message: "test"}, nil
	})
	d.RegionType(RegionTypeSpec{ID: "unimplemented"}, nil)
	d.OnShapeEditEnded(func(context.Context, *control.Client, *pb.ShapeEditEnded) error { ended = true; return nil })
	a, err := d.Build()
	if err != nil {
		t.Fatal(err)
	}
	data, err := a.Manifest("shapes")
	if err != nil {
		t.Fatal(err)
	}
	var m manifest.Manifest
	if err := json.Unmarshal(data, &m); err != nil {
		t.Fatal(err)
	}
	if len(m.Contributions.RegionTypes) != 2 || m.Contributions.RegionTypes[0].Name != "tetra" {
		t.Fatal(string(data))
	}
	p, err := a.Configure(control.Plugin{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := p.Control.Shapers["tetra"](context.Background(), nil, nil, true); err != nil {
		t.Fatal(err)
	}
	if err := p.Control.ShapeEditEnded(context.Background(), nil, &pb.ShapeEditEnded{}); err != nil {
		t.Fatal(err)
	}
	if !called || !ended {
		t.Fatal("hooks not wired")
	}
	// Configured maps are independent snapshots.
	delete(p.Control.Shapers, "tetra")
	second, err := a.Configure(control.Plugin{})
	if err != nil || second.Control.Shapers["tetra"] == nil {
		t.Fatal(err)
	}
	for _, duplicate := range []bool{false, true} {
		d := NewDefinition(Info{ID: "test", Name: "Test", Version: "1"})
		if duplicate {
			d.Needs(ContributeROIs)
			d.RegionType(RegionTypeSpec{ID: "tetra"}, nil)
		}
		d.RegionType(RegionTypeSpec{ID: "tetra"}, nil)
		if _, err := d.Build(); err == nil {
			t.Fatal("missing permission or duplicate accepted")
		}
	}
}
