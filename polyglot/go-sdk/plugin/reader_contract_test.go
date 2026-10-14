package plugin

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func TestReaderReplacementBoundsAndShutdown(t *testing.T) {
	var events []string
	readers := map[string]ReaderHandlers{}
	for _, id := range []string{"a", "b"} {
		readers[id] = ReaderHandlers{
			Open: func(_ context.Context, _ *control.Client, q *pb.OpenReader) (*pb.ReaderOpened, error) {
				events = append(events, "open "+id)
				if q.Uri == "fail" {
					return nil, errors.New("open failed")
				}
				return &pb.ReaderOpened{Handle: "local", Dtype: pb.DType_DTYPE_U16, ShapeTczyx: []uint32{2, 3, 4, 5, 6}}, nil
			},
			Close: func(_ context.Context, _ *control.Client, q *pb.CloseReader) (*pb.Closed, error) {
				if q.Handle != "local" {
					t.Fatal(q)
				}
				events = append(events, "close "+id)
				return &pb.Closed{Handle: q.Handle}, nil
			},
			Volume: func(context.Context, *control.Client, *pb.ReadVolume) (*pb.SegmentRef, error) {
				events = append(events, "volume "+id)
				return &pb.SegmentRef{Dtype: pb.DType_DTYPE_U16, ShapeZyx: []uint32{4, 5, 6}}, nil
			},
			Plane: func(context.Context, *control.Client, *pb.ReadPlane) (*pb.SegmentRef, error) {
				events = append(events, "plane "+id)
				return &pb.SegmentRef{Dtype: pb.DType_DTYPE_U16, ShapeZyx: []uint32{1, 5, 6}}, nil
			},
		}
	}
	cfg, err := (Handlers{Readers: readers}).Configure(control.Config{Shutdown: func(context.Context, *control.Client, *pb.Shutdown) error {
		events = append(events, "shutdown")
		return nil
	}})
	if err != nil {
		t.Fatal(err)
	}
	call := func(q *pb.HostMsg) (*pb.PluginMsg, error) { return cfg.HandleRequest(context.Background(), nil, q) }
	open := func(id, uri string) string {
		out, err := call(&pb.HostMsg{Msg: &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{ReaderId: id, Uri: uri}}})
		if err != nil {
			t.Fatal(err)
		}
		return out.GetReaderOpened().GetHandle()
	}
	a := open("a", "ok")
	for _, q := range []*pb.HostMsg{
		{Msg: &pb.HostMsg_ReadVolume{ReadVolume: &pb.ReadVolume{Handle: a, Level: 1}}},
		{Msg: &pb.HostMsg_ReadVolume{ReadVolume: &pb.ReadVolume{Handle: a, T: 2}}},
		{Msg: &pb.HostMsg_ReadVolume{ReadVolume: &pb.ReadVolume{Handle: a, C: 3}}},
		{Msg: &pb.HostMsg_ReadPlane{ReadPlane: &pb.ReadPlane{Handle: a, Level: 1}}},
		{Msg: &pb.HostMsg_ReadPlane{ReadPlane: &pb.ReadPlane{Handle: a, T: 2}}},
		{Msg: &pb.HostMsg_ReadPlane{ReadPlane: &pb.ReadPlane{Handle: a, C: 3}}},
		{Msg: &pb.HostMsg_ReadPlane{ReadPlane: &pb.ReadPlane{Handle: a, Z: 4}}},
	} {
		_, err := call(q)
		var hostErr *control.HostError
		if !errors.As(err, &hostErr) || hostErr.Kind != pb.Error_OUT_OF_BOUNDS {
			t.Fatal(q, err)
		}
	}
	if !reflect.DeepEqual(events, []string{"open a"}) {
		t.Fatal(events)
	}
	for _, q := range []*pb.HostMsg{
		{Msg: &pb.HostMsg_ReadVolume{ReadVolume: &pb.ReadVolume{Handle: a, T: 1, C: 2}}},
		{Msg: &pb.HostMsg_ReadPlane{ReadPlane: &pb.ReadPlane{Handle: a, T: 1, C: 2, Z: 3}}},
	} {
		if _, err := call(q); err != nil {
			t.Fatal(err)
		}
	}
	b := open("b", "next")
	for _, q := range []*pb.HostMsg{
		{Msg: &pb.HostMsg_ReadVolume{ReadVolume: &pb.ReadVolume{Handle: a}}},
		{Msg: &pb.HostMsg_ReadPlane{ReadPlane: &pb.ReadPlane{Handle: a}}},
		{Msg: &pb.HostMsg_Metadata{Metadata: &pb.Metadata{Handle: a}}},
	} {
		_, err := call(q)
		var hostErr *control.HostError
		if !errors.As(err, &hostErr) || hostErr.Kind != pb.Error_NOT_FOUND || !strings.Contains(hostErr.Message, a) {
			t.Fatal(q, err)
		}
	}
	for i := 0; i < 2; i++ {
		out, err := call(&pb.HostMsg{Msg: &pb.HostMsg_CloseReader{CloseReader: &pb.CloseReader{Handle: a}}})
		if err != nil || out.GetClosed().GetHandle() != a {
			t.Fatal(out, err)
		}
	}
	if _, err := call(&pb.HostMsg{Msg: &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{ReaderId: "a", Uri: "fail"}}}); err == nil {
		t.Fatal("open failure lost")
	}
	if _, err := call(&pb.HostMsg{Msg: &pb.HostMsg_ReadVolume{ReadVolume: &pb.ReadVolume{Handle: b}}}); err != nil {
		t.Fatal("failed replacement lost old reader", err)
	}
	open("a", "ok")
	if err := cfg.Shutdown(context.Background(), nil, &pb.Shutdown{}); err != nil {
		t.Fatal(err)
	}
	want := []string{"open a", "volume a", "plane a", "open b", "close a", "open a", "volume b", "open a", "close b", "close a", "shutdown"}
	if !reflect.DeepEqual(events, want) {
		t.Fatal(events)
	}
}

func TestRawReaderRejectsNonzeroLevelsBeforeCallbacks(t *testing.T) {
	h := Handlers{Reader: ReaderHandlers{
		Volume: func(context.Context, *control.Client, *pb.ReadVolume) (*pb.SegmentRef, error) {
			t.Fatal("volume callback reached")
			return nil, nil
		},
		Plane: func(context.Context, *control.Client, *pb.ReadPlane) (*pb.SegmentRef, error) {
			t.Fatal("plane callback reached")
			return nil, nil
		},
	}}
	cfg, err := h.Configure(control.Config{})
	if err != nil {
		t.Fatal(err)
	}
	for _, q := range []*pb.HostMsg{
		{Msg: &pb.HostMsg_ReadVolume{ReadVolume: &pb.ReadVolume{Level: 1}}},
		{Msg: &pb.HostMsg_ReadPlane{ReadPlane: &pb.ReadPlane{Level: 1}}},
	} {
		_, err := cfg.HandleRequest(context.Background(), nil, q)
		var hostErr *control.HostError
		if !errors.As(err, &hostErr) || hostErr.Kind != pb.Error_OUT_OF_BOUNDS {
			t.Fatal(err)
		}
	}
}

func TestReaderRepliesMatchOpenedGeometry(t *testing.T) {
	for _, registered := range []bool{false, true} {
		for _, plane := range []bool{false, true} {
			t.Run(fmt.Sprintf("registered=%t/plane=%t", registered, plane), func(t *testing.T) {
				opened := &pb.ReaderOpened{Handle: "local", Dtype: pb.DType_DTYPE_U16, ShapeTczyx: []uint32{2, 3, 4, 5, 6}}
				var reply *pb.SegmentRef
				var authorErr error
				handlers := ReaderHandlers{
					Open: func(context.Context, *control.Client, *pb.OpenReader) (*pb.ReaderOpened, error) { return opened, nil },
					Volume: func(context.Context, *control.Client, *pb.ReadVolume) (*pb.SegmentRef, error) {
						return reply, authorErr
					},
					Plane: func(context.Context, *control.Client, *pb.ReadPlane) (*pb.SegmentRef, error) { return reply, authorErr },
				}
				h := Handlers{Reader: handlers}
				if registered {
					h = Handlers{Readers: map[string]ReaderHandlers{"r": handlers}}
				}
				cfg, err := h.Configure(control.Config{})
				if err != nil {
					t.Fatal(err)
				}
				call := func(q *pb.HostMsg) (*pb.PluginMsg, error) { return cfg.HandleRequest(context.Background(), nil, q) }
				out, err := call(&pb.HostMsg{Msg: &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{ReaderId: "r"}}})
				if err != nil {
					t.Fatal(err)
				}
				handle := out.GetReaderOpened().Handle
				// Neither the callback's object nor the caller's reply may change the
				// recorded geometry after Open completes.
				opened.Dtype = pb.DType_DTYPE_F32
				opened.ShapeTczyx[4] = 99
				out.GetReaderOpened().Dtype = pb.DType_DTYPE_F32
				out.GetReaderOpened().ShapeTczyx[4] = 99
				request := &pb.HostMsg{Msg: &pb.HostMsg_ReadVolume{ReadVolume: &pb.ReadVolume{Handle: handle}}}
				want := []uint32{4, 5, 6}
				if plane {
					want[0] = 1
					request.Msg = &pb.HostMsg_ReadPlane{ReadPlane: &pb.ReadPlane{Handle: handle}}
				}
				for _, tc := range []struct {
					name  string
					shape []uint32
					dtype pb.DType
					ok    bool
				}{
					{"valid", want, pb.DType_DTYPE_U16, true},
					{"dtype", want, pb.DType_DTYPE_U8, false},
					{"missing shape", nil, pb.DType_DTYPE_U16, false},
					{"rank", []uint32{5, 6}, pb.DType_DTYPE_U16, false},
					{"z", []uint32{2, 5, 6}, pb.DType_DTYPE_U16, false},
					{"y", []uint32{want[0], 6, 6}, pb.DType_DTYPE_U16, false},
					{"x", []uint32{want[0], 5, 7}, pb.DType_DTYPE_U16, false},
				} {
					t.Run(tc.name, func(t *testing.T) {
						reply = &pb.SegmentRef{ShapeZyx: tc.shape, Dtype: tc.dtype}
						out, err := call(request)
						if tc.ok {
							if err != nil || out.GetSegment() != reply {
								t.Fatal(out, err)
							}
							return
						}
						if err == nil || out != nil {
							t.Fatal("bad reader geometry published", out, err)
						}
						for _, text := range []string{"got shape_zyx=" + fmt.Sprint(tc.shape), "dtype=" + tc.dtype.String(), "expected shape_zyx=" + fmt.Sprint(want), "dtype=DTYPE_U16", "ReaderOpened shape_tczyx=[2 3 4 5 6]"} {
							if !strings.Contains(err.Error(), text) {
								t.Fatalf("error missing %q: %v", text, err)
							}
						}
					})
				}
				authorErr = errors.New("author read failure")
				if _, err = call(request); err != authorErr {
					t.Fatal("author error replaced", err)
				}
				// A successful replacement updates the declaration used for later reads.
				authorErr = nil
				out, err = call(&pb.HostMsg{Msg: &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{ReaderId: "r", Uri: "replacement"}}})
				if err != nil {
					t.Fatal(err)
				}
				if plane {
					request.GetReadPlane().Handle = out.GetReaderOpened().Handle
				} else {
					request.GetReadVolume().Handle = out.GetReaderOpened().Handle
				}
				reply = &pb.SegmentRef{ShapeZyx: []uint32{want[0], 5, 99}, Dtype: pb.DType_DTYPE_F32}
				if _, err = call(request); err != nil {
					t.Fatal("replacement geometry lost", err)
				}
			})
		}
	}
}
