package payload

import (
	"context"
	"net"
	"slices"
	"testing"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/proto"
)

func testFilter() *pb.AppliedFilter {
	return &pb.AppliedFilter{PluginId: "org.example.filter", PluginVersion: "1.2.3", StageId: "align",
		Params: map[string]*pb.Value{"options": {Kind: &pb.Value_S{S: " --Run=É  "}}, "n": {Kind: &pb.Value_I{I: 9007199254740993}}}, Channels: []uint32{2, 7}}
}

func TestReadProvenanceIsPerReplyAndOwned(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	attached := make(chan *control.Client, 1)
	server, err := control.NewServer(control.Config{PluginID: "provenance.test", Handshake: func(*pb.Hello) error { return nil },
		Attach:  func(c *control.Client) error { attached <- c; return nil },
		Prepare: func(context.Context, *control.Client, *pb.HostMsg) error { return nil },
		Release: func(_ *control.Client, m *pb.HostMsg) error {
			// Disposing the wire reply must not mutate the author's owned metadata.
			if f := m.GetDataVolume().GetFilter(); f != nil {
				f.Params["options"].Kind = &pb.Value_S{S: "released"}
				f.Channels[0] = 999
			}
			return nil
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	l := bufconn.Listen(1 << 20)
	defer l.Close()
	g := server.GRPCServer()
	go g.Serve(l)
	defer g.Stop()
	conn, err := grpc.NewClient("passthrough:///provenance", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return l.Dial() }))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	stream, err := pb.NewPluginClient(conn).Session(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if err := stream.Send(&pb.HostMsg{Id: "Hello from provenance test", Msg: &pb.HostMsg_Hello{Hello: &pb.Hello{ProtocolMajor: 1}}}); err != nil {
		t.Fatal(err)
	}
	if first, err := stream.Recv(); err != nil || first.GetReady() == nil {
		t.Fatal(first, err)
	}
	var client *control.Client
	select {
	case client = <-attached:
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
	// View provenance is surfaced unchanged, but cannot determine read provenance.
	viewDone := make(chan *pb.ViewState, 1)
	go func() { v, _ := client.GetViewState(ctx); viewDone <- v }()
	q, err := stream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	if err := stream.Send(&pb.HostMsg{Id: q.Id, Msg: &pb.HostMsg_ViewState{ViewState: &pb.ViewState{Filter: testFilter()}}}); err != nil {
		t.Fatal(err)
	}
	select {
	case v := <-viewDone:
		if !proto.Equal(v.GetFilter(), testFilter()) {
			t.Fatal(v)
		}
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
	for _, tc := range []struct {
		name     string
		c, level uint32
		raw      bool
		filter   *pb.AppliedFilter
	}{
		{"filtered", 2, 0, false, testFilter()},
		{"explicit raw despite view filter", 2, 0, true, nil},
		{"failed timepoint fallback", 2, 0, false, nil},
		{"other channel", 4, 0, false, nil},
		{"pyramid", 2, 1, false, nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			type result struct {
				v   *Volume
				err error
			}
			done := make(chan result, 1)
			go func() {
				v, err := ReadVolume(ctx, client, copyFunc(func(_ *pb.SegmentRef, b []byte) error { copy(b, []byte{9, 10}); return nil }), &pb.ReadDataVolume{C: tc.c, Level: tc.level, Raw: tc.raw}, CopyOptions{MaxBytes: 2, ByteOrder: LittleEndian})
				done <- result{v, err}
			}()
			q, err := stream.Recv()
			if err != nil {
				t.Fatal(err)
			}
			if request := q.GetReadDataVolume(); request == nil || request.Raw != tc.raw || request.C != tc.c || request.Level != tc.level {
				t.Fatalf("read request changed on the wire: %v", q)
			}
			v := &pb.DataVolume{Segment: &pb.SegmentRef{SegmentId: 1, Offset: 64, Len: 2, ShapeZyx: []uint32{1, 1, 2}, Dtype: pb.DType_DTYPE_U8}, T: 3, C: tc.c, Level: tc.level, OpenGeneration: 7, Filter: tc.filter}
			if err := stream.Send(&pb.HostMsg{Id: q.Id, Msg: &pb.HostMsg_DataVolume{DataVolume: v}}); err != nil {
				t.Fatal(err)
			}
			select {
			case got := <-done:
				if got.err != nil || got.v == nil || !proto.Equal(got.v.Filter, tc.filter) || got.v.T != 3 || got.v.C != tc.c || got.v.Level != tc.level || got.v.OpenGeneration != 7 {
					t.Fatal(got.v, got.err)
				}
				if !slices.Equal(got.v.Tensor.Bytes(), []byte{9, 10}) {
					t.Fatal("pixels changed")
				}
			case <-ctx.Done():
				t.Fatal(ctx.Err())
			}
		})
	}
}

func TestExportVolumeProvenance(t *testing.T) {
	for _, segment := range []bool{false, true} {
		for _, mode := range []string{"mixed", "raw fallback", "no filter"} {
			t.Run(mode+map[bool]string{false: " inline", true: " segment"}[segment], func(t *testing.T) {
				opened := &pb.OpenExport{ShapeTczyx: []uint32{1, 3, 1, 1, 2}, Dtype: pb.DType_DTYPE_U8, LittleEndian: true, Filter: testFilter()}
				chunk := &pb.ExportChunk{T: 9, Channels: []uint32{7, 4, 2}, FilteredChannels: []uint32{2, 7}, Source: &pb.ExportChunk_Inline{Inline: []byte{1, 2, 3, 4, 5, 6}}}
				if mode != "mixed" {
					chunk.FilteredChannels = nil
				}
				if mode == "no filter" {
					opened.Filter = nil
				}
				if segment {
					chunk.Source = &pb.ExportChunk_Segment{Segment: &pb.SegmentRef{SegmentId: 1, Offset: 64, Len: 6, ShapeZyx: []uint32{1, 1, 2}, Dtype: pb.DType_DTYPE_U8}}
				}
				// Exercise the additive field numbers through protobuf before the API.
				for _, msg := range []proto.Message{opened, chunk} {
					b, err := proto.Marshal(msg)
					if err != nil {
						t.Fatal(err)
					}
					proto.Reset(msg)
					if err := proto.Unmarshal(b, msg); err != nil {
						t.Fatal(err)
					}
				}
				vs, err := CopyExportVolumes(copyFunc(func(_ *pb.SegmentRef, b []byte) error { copy(b, []byte{1, 2, 3, 4, 5, 6}); return nil }), opened, chunk, 6)
				if err != nil || len(vs) != 3 {
					t.Fatal(vs, err)
				}
				for i, v := range vs {
					var want *pb.AppliedFilter
					if mode == "mixed" && i != 1 {
						want = testFilter()
					}
					if !proto.Equal(v.Filter, want) || v.T != 9 || v.C != chunk.Channels[i] || v.Level != 0 || v.OpenGeneration != 0 || !slices.Equal(v.Tensor.Shape(), []uint32{1, 1, 2}) || !slices.Equal(v.Tensor.Bytes(), []byte{byte(2*i + 1), byte(2*i + 2)}) {
						t.Fatal(i, v)
					}
				}
				if mode == "mixed" {
					opened.Filter.Params["options"].Kind = &pb.Value_S{S: "mutated open"}
					vs[0].Filter.Channels[0] = 99
					vs[0].Filter.Params["options"].Kind = &pb.Value_S{S: "mutated volume"}
					if !proto.Equal(vs[2].Filter, testFilter()) {
						t.Fatal("filter snapshots alias")
					}
				}
			})
		}
	}
	opened := &pb.OpenExport{ShapeTczyx: []uint32{1, 1, 1, 1, 1}, Dtype: pb.DType_DTYPE_U8}
	chunk := &pb.ExportChunk{Channels: []uint32{2}, FilteredChannels: []uint32{2}, Source: &pb.ExportChunk_Inline{Inline: []byte{1}}}
	if _, err := CopyExportVolumes(nil, opened, chunk, 1); err == nil {
		t.Fatal("claimed filter without export filter")
	}
	opened.Filter = testFilter()
	chunk.FilteredChannels = []uint32{7}
	if _, err := CopyExportVolumes(nil, opened, chunk, 1); err == nil {
		t.Fatal("claimed channel outside chunk")
	}
}
