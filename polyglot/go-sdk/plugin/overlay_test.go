package plugin

import (
	"bytes"
	"errors"
	"math"
	"sync"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/geometry"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/payload"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

type senderFunc func(*pb.PluginMsg) error

func (f senderFunc) Send(m *pb.PluginMsg) error { return f(m) }

func TestPolylineTopologies(t *testing.T) {
	e := GeometryEncoder{ByteOrder: payload.LittleEndian}
	for _, topology := range []LineTopology{"", Polyline, Loop} {
		item, err := e.Lines("line", []geometry.ZYX{{0, 0, 0}, {1, 1, 1}, {2, 2, 2}}, topology)
		if err != nil || item.GetLineSet().GetTopology() != string(topology) {
			t.Fatal(item, err)
		}
	}
	for _, topology := range []LineTopology{"", Polyline, Segments, Loop} {
		if _, err := e.Lines("line", []geometry.ZYX{{0, 0, 0}}, topology); err == nil {
			t.Fatal("one-point line accepted")
		}
	}
	if _, err := e.Lines("line", []geometry.ZYX{{}, {}, {}}, "unknown"); err == nil {
		t.Fatal("unknown topology")
	}
}

func TestInlineGeometryUsesExplicitFrameShapeAndOrder(t *testing.T) {
	encoder := GeometryEncoder{ByteOrder: payload.LittleEndian}
	item, err := encoder.Markers("m", []geometry.ZYX{{1, 2, 3}})
	if err != nil {
		t.Fatal(err)
	}
	b := item.GetMarker().PositionsZyx
	if item.Transform.Frame != "voxel" || len(b.Shape) != 2 || b.Shape[0] != 1 || b.Shape[1] != 3 || b.Dtype != pb.DType_DTYPE_F64 {
		t.Fatal(item)
	}
	// Explicit golden for the Z coordinate, independent of the encoder.
	if !bytes.Equal(b.GetInline()[:8], []byte{0, 0, 0, 0, 0, 0, 0xf0, 0x3f}) {
		t.Fatal(b)
	}
	if _, err := (GeometryEncoder{}).Markers("m", []geometry.ZYX{{1, 2, 3}}); err == nil {
		t.Fatal("default byte order guessed")
	}
	if _, err := encoder.Lines("l", []geometry.ZYX{{}, {}, {}}, Segments); err == nil {
		t.Fatal("unpaired segments accepted")
	}
	if _, err := encoder.Markers("m", []geometry.ZYX{{math.NaN(), 0, 0}}); err == nil {
		t.Fatal("nonfinite coordinates accepted")
	}
	labels, err := payload.Encode([]uint16{1, 255}, []uint32{1, 1, 2}, true)
	if err != nil {
		t.Fatal(err)
	}
	lut := [256][4]uint8{}
	lut[1] = [4]uint8{255, 0, 0, 128}
	label, err := encoder.Labels("labels", labels, lut)
	if err != nil {
		t.Fatal(err)
	}
	v := label.GetLabelVolume()
	if v.T != nil || len(v.Colormap.GetInline()) != 1024 || v.Colormap.GetInline()[7] != 128 {
		t.Fatal(v)
	}
}

func TestOverlaySerializesRevisionAndKeepsRefusalsVisible(t *testing.T) {
	var accepted []*pb.PluginMsg
	attempts := 0
	sender := senderFunc(func(m *pb.PluginMsg) error {
		attempts++
		if attempts == 1 {
			return control.ErrQueueFull
		}
		accepted = append(accepted, m)
		return nil
	})
	overlay, err := NewOverlay(sender, "results")
	if err != nil {
		t.Fatal(err)
	}
	encoder := GeometryEncoder{payload.LittleEndian}
	item, err := encoder.Markers("m", []geometry.ZYX{{1, 2, 3}})
	if err != nil {
		t.Fatal(err)
	}
	if err := overlay.Replace(item); !errors.Is(err, control.ErrQueueFull) {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := overlay.Replace(item); err != nil {
				t.Error(err)
			}
		}()
	}
	wg.Wait()
	item.ItemId = "caller mutation"
	if err := overlay.Clear(); err != nil {
		t.Fatal(err)
	}
	if len(accepted) != 21 {
		t.Fatal(len(accepted))
	}
	for i, m := range accepted[:20] {
		if m.GetOverlayUpdate().Revision != uint64(i+2) || m.GetOverlayUpdate().Items[0].ItemId != "m" {
			t.Fatal(m)
		}
	}
	if accepted[20].GetOverlayRemove().Revision != 22 {
		t.Fatal(accepted[20])
	}
	overlay.revision = math.MaxUint64
	if err := overlay.Clear(); err == nil || attempts != 22 {
		t.Fatal(err, attempts)
	}
}

func TestOverlayRejectsLoansAndUnknownFieldsBeforeSend(t *testing.T) {
	count := 0
	overlay, _ := NewOverlay(senderFunc(func(*pb.PluginMsg) error { count++; return nil }), "o")
	item, err := (GeometryEncoder{payload.LittleEndian}).Markers("m", []geometry.ZYX{{}})
	if err != nil {
		t.Fatal(err)
	}
	loan := proto.Clone(item).(*pb.OverlayItem)
	loan.GetMarker().PositionsZyx.Source = &pb.GeomBuffer_Segment{Segment: &pb.SegmentRef{SegmentId: 1, Len: 24, ShapeZyx: []uint32{1, 1, 3}, Dtype: pb.DType_DTYPE_F64}}
	if err := overlay.Replace(loan); err == nil {
		t.Fatal("accepted a loan")
	}
	malformed := proto.Clone(item).(*pb.OverlayItem)
	malformed.GetMarker().PositionsZyx.Source = &pb.GeomBuffer_Inline{Inline: []byte{1}}
	if err := overlay.Replace(malformed); err == nil {
		t.Fatal("accepted inline geometry with a mismatched byte length")
	}
	future := proto.Clone(item).(*pb.OverlayItem)
	future.ProtoReflect().SetUnknown([]byte{0xa0, 0x06, 0x01})
	if err := overlay.Replace(future); err == nil {
		t.Fatal("discarded unknown ownership fields")
	}
	if err := overlay.Replace(item, item); err == nil {
		t.Fatal("duplicate item IDs accepted")
	}
	if count != 0 {
		t.Fatal("sent invalid geometry")
	}
	if err := overlay.Replace(); err != nil || count != 1 {
		t.Fatal("empty replacement should be valid", err)
	}
}
