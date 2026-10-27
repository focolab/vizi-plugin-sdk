package plugin

import (
	"context"
	"net"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"
)

func TestSessionRejectsContradictoryExportBeforeCallbacks(t *testing.T) {
	for _, mode := range []string{"raw", "typed", "declared"} {
		for _, filtered := range []bool{false, true} {
			name := "missing-filter"
			if filtered {
				name = "channel-not-in-chunk"
			}
			t.Run(mode+"/"+name, func(t *testing.T) {
				var writes, finishes, aborts, releases atomic.Int32
				var prepared, segmentReleases atomic.Int32
				handlers := ExportHandlers{
					Open: func(_ context.Context, _ *control.Client, q *pb.OpenExport) (*pb.ExportOpened, error) {
						// Snapshot provenance before an author mutates the wire request.
						if q.Filter == nil {
							q.Filter = &pb.AppliedFilter{}
						} else {
							q.Filter = nil
						}
						return &pb.ExportOpened{Handle: "local"}, nil
					},
					Chunk: func(_ context.Context, _ *control.Client, q *pb.ExportChunk) (*pb.ChunkWritten, error) {
						writes.Add(1)
						return &pb.ChunkWritten{Handle: q.Handle}, nil
					},
					Finish: func(_ context.Context, _ *control.Client, q *pb.FinishExport) (*pb.ExportDone, error) {
						finishes.Add(1)
						return &pb.ExportDone{Handle: q.Handle}, nil
					},
					Abort: func(_ context.Context, _ *control.Client, q *pb.AbortExport) (*pb.ExportDone, error) {
						aborts.Add(1)
						return &pb.ExportDone{Handle: q.Handle}, nil
					},
				}
				cfg := control.Config{
					PluginID: "export.test", BuildIdentity: "export.test test build",
					Handshake: func(*pb.Hello) error { return nil },
					Prepare: func(context.Context, *control.Client, *pb.HostMsg) error {
						prepared.Add(1)
						return nil
					},
					Release: func(_ *control.Client, m *pb.HostMsg) error {
						if m.GetExportChunk() != nil {
							releases.Add(1)
							if m.GetExportChunk().GetSegment() != nil {
								segmentReleases.Add(1)
							}
						}
						return nil
					},
				}
				switch mode {
				case "raw":
					cfg.HandleRequest = func(ctx context.Context, c *control.Client, m *pb.HostMsg) (*pb.PluginMsg, error) {
						switch q := m.Msg.(type) {
						case *pb.HostMsg_OpenExport:
							v, err := handlers.Open(ctx, c, q.OpenExport)
							return &pb.PluginMsg{Msg: &pb.PluginMsg_ExportOpened{ExportOpened: v}}, err
						case *pb.HostMsg_ExportChunk:
							v, err := handlers.Chunk(ctx, c, q.ExportChunk)
							return &pb.PluginMsg{Msg: &pb.PluginMsg_ChunkWritten{ChunkWritten: v}}, err
						case *pb.HostMsg_FinishExport:
							v, err := handlers.Finish(ctx, c, q.FinishExport)
							return &pb.PluginMsg{Msg: &pb.PluginMsg_ExportDone{ExportDone: v}}, err
						case *pb.HostMsg_AbortExport:
							v, err := handlers.Abort(ctx, c, q.AbortExport)
							return &pb.PluginMsg{Msg: &pb.PluginMsg_ExportDone{ExportDone: v}}, err
						}
						panic("unexpected request")
					}
				case "typed":
					var err error
					cfg, err = (Handlers{Export: handlers}).Configure(cfg)
					if err != nil {
						t.Fatal(err)
					}
				case "declared":
					cfg = configureExporters(cfg, map[string]ExportHandlers{"writer": handlers})
				}
				exchange := exportTestSession(t, cfg)
				opened := &pb.OpenExport{ExporterId: "writer"}
				if filtered {
					opened.Filter = &pb.AppliedFilter{StageId: "gain"}
				}
				reply := exchange(&pb.HostMsg{Msg: &pb.HostMsg_OpenExport{OpenExport: opened}})
				if reply.GetExportOpened() == nil {
					t.Fatal(reply)
				}
				handle := reply.GetExportOpened().Handle
				good := &pb.ExportChunk{Handle: handle, Channels: []uint32{2}, Source: &pb.ExportChunk_Inline{Inline: []byte{1}}}
				if filtered {
					good.FilteredChannels = []uint32{2}
				}
				if reply := exchange(&pb.HostMsg{Msg: &pb.HostMsg_ExportChunk{ExportChunk: good}}); reply.GetChunkWritten() == nil {
					t.Fatal(reply)
				}
				// A selected filter still permits raw channels.
				good.FilteredChannels = nil
				if reply := exchange(&pb.HostMsg{Msg: &pb.HostMsg_ExportChunk{ExportChunk: good}}); reply.GetChunkWritten() == nil {
					t.Fatal(reply)
				}
				bad := &pb.ExportChunk{Handle: handle, Channels: []uint32{2}, FilteredChannels: []uint32{2}, Source: &pb.ExportChunk_Inline{Inline: []byte{9}}}
				detail := "OpenExport.filter"
				if filtered {
					bad.FilteredChannels = []uint32{3}
					bad.Source = &pb.ExportChunk_Segment{Segment: &pb.SegmentRef{SegmentId: 7}}
					detail = "channel 3 absent"
				}
				reject := func(reply *pb.PluginMsg, detail string) {
					t.Helper()
					if reply.GetError() == nil || reply.GetError().Kind != pb.Error_INVALID_ARGUMENT || !strings.Contains(reply.GetError().Message, detail) {
						t.Fatal(reply)
					}
				}
				reject(exchange(&pb.HostMsg{Msg: &pb.HostMsg_ExportChunk{ExportChunk: bad}}), detail)
				reject(exchange(&pb.HostMsg{Msg: &pb.HostMsg_ExportChunk{ExportChunk: good}}), "AbortExport")
				reject(exchange(&pb.HostMsg{Msg: &pb.HostMsg_FinishExport{FinishExport: &pb.FinishExport{Handle: handle}}}), "AbortExport")
				if reply := exchange(&pb.HostMsg{Msg: &pb.HostMsg_AbortExport{AbortExport: &pb.AbortExport{Handle: handle}}}); reply.GetExportDone() == nil {
					t.Fatal(reply)
				}
				if writes.Load() != 2 || finishes.Load() != 0 || aborts.Load() != 1 || releases.Load() != 4 {
					t.Fatalf("writes=%d finishes=%d aborts=%d releases=%d", writes.Load(), finishes.Load(), aborts.Load(), releases.Load())
				}
				var wantSegments int32
				if filtered {
					wantSegments = 1
				}
				if prepared.Load() != wantSegments || segmentReleases.Load() != wantSegments {
					t.Fatalf("prepared=%d segment releases=%d", prepared.Load(), segmentReleases.Load())
				}
				// Raw handles may be reused; reopening must clear rejected state.
				reply = exchange(&pb.HostMsg{Msg: &pb.HostMsg_OpenExport{OpenExport: &pb.OpenExport{ExporterId: "writer"}}})
				if reply.GetExportOpened() == nil {
					t.Fatal(reply)
				}
				handle = reply.GetExportOpened().Handle
				good.Handle = handle
				if reply := exchange(&pb.HostMsg{Msg: &pb.HostMsg_ExportChunk{ExportChunk: good}}); reply.GetChunkWritten() == nil {
					t.Fatal(reply)
				}
				if reply := exchange(&pb.HostMsg{Msg: &pb.HostMsg_FinishExport{FinishExport: &pb.FinishExport{Handle: handle}}}); reply.GetExportDone() == nil {
					t.Fatal(reply)
				}
			})
		}
	}
}

func exportTestSession(t *testing.T, cfg control.Config) func(*pb.HostMsg) *pb.PluginMsg {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	t.Cleanup(cancel)
	server, err := control.NewServer(cfg)
	if err != nil {
		t.Fatal(err)
	}
	listener := bufconn.Listen(1 << 20)
	grpcServer := server.GRPCServer()
	go grpcServer.Serve(listener)
	t.Cleanup(func() { grpcServer.Stop(); listener.Close() })
	conn, err := grpc.NewClient("passthrough:///test", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	stream, err := pb.NewPluginClient(conn).Session(ctx)
	if err != nil {
		t.Fatal(err)
	}
	exchange := func(m *pb.HostMsg) *pb.PluginMsg {
		t.Helper()
		if m.Id == "" {
			m.Id = freshID("request")
		}
		if err := stream.Send(m); err != nil {
			t.Fatal(err)
		}
		reply, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		if m.GetHello() == nil && reply.Id != m.Id {
			t.Fatalf("reply ID: %v", reply)
		}
		return reply
	}
	if reply := exchange(&pb.HostMsg{Id: "Hello from test host", Msg: &pb.HostMsg_Hello{Hello: &pb.Hello{ProtocolMajor: 1}}}); reply.GetReady() == nil {
		t.Fatal(reply)
	}
	return exchange
}
