package payload

import (
	"context"
	"errors"
	"net"
	"sync/atomic"
	"testing"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"
)

func TestInclusionCountAndRelease(t *testing.T) {
	for _, name := range []string{"mask", "hidden", "empty", "count mismatch", "count without mask", "nonbinary", "copy failure"} {
		t.Run(name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			attached := make(chan *control.Client, 1)
			var releases atomic.Int32
			server, err := control.NewServer(control.Config{PluginID: "inclusion.test", Handshake: func(*pb.Hello) error { return nil }, Attach: func(c *control.Client) error { attached <- c; return nil },
				Prepare: func(context.Context, *control.Client, *pb.HostMsg) error { return nil },
				Release: func(_ *control.Client, m *pb.HostMsg) error {
					if m.GetRoiInclusion().GetSegment() != nil {
						releases.Add(1)
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
			conn, err := grpc.NewClient("passthrough:///inclusion", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return l.Dial() }))
			if err != nil {
				t.Fatal(err)
			}
			defer conn.Close()
			stream, err := pb.NewPluginClient(conn).Session(ctx)
			if err != nil {
				t.Fatal(err)
			}
			if err := stream.Send(&pb.HostMsg{Id: "Hello from inclusion test", Msg: &pb.HostMsg_Hello{Hello: &pb.Hello{ProtocolMajor: 1}}}); err != nil {
				t.Fatal(err)
			}
			if q, err := stream.Recv(); err != nil || q.GetReady() == nil {
				t.Fatal(q, err)
			}
			c := <-attached
			type result struct {
				mask *InclusionMask
				err  error
			}
			done := make(chan result, 1)
			copyFailure := errors.New("copy failed")
			copier := copyFunc(func(_ *pb.SegmentRef, dst []byte) error {
				if name == "copy failure" {
					return copyFailure
				}
				copy(dst, []byte{1, 0, 1, 0})
				if name == "nonbinary" {
					dst[0] = 2
				}
				return nil
			})
			go func() {
				m, err := ReadInclusion(ctx, c, copier, &pb.ReadRoiInclusion{RegionId: "a", T: 3}, CopyOptions{MaxBytes: 4, ByteOrder: LittleEndian})
				done <- result{m, err}
			}()
			q, err := stream.Recv()
			if err != nil || q.GetReadRoiInclusion() == nil {
				t.Fatal(q, err)
			}
			body := &pb.RoiInclusion{RegionId: "a", T: 3, OpenGeneration: 7, OriginZyx: []uint32{4, 5, 6}, Present: true, VoxelCount: 2,
				Segment: &pb.SegmentRef{SegmentId: 1, Len: 4, ShapeZyx: []uint32{1, 2, 2}, Dtype: pb.DType_DTYPE_U8}}
			switch name {
			case "hidden", "empty":
				body.Present = name == "empty"
				body.Segment = nil
				body.VoxelCount = 0
			case "count without mask":
				body.Segment = nil
			case "count mismatch":
				body.VoxelCount = 3
			}
			if err := stream.Send(&pb.HostMsg{Id: q.Id, Msg: &pb.HostMsg_RoiInclusion{RoiInclusion: body}}); err != nil {
				t.Fatal(err)
			}
			var got result
			select {
			case got = <-done:
			case <-ctx.Done():
				t.Fatal(ctx.Err())
			}
			if body.Segment != nil && releases.Load() != 1 || body.Segment == nil && releases.Load() != 0 {
				t.Fatal("incorrect releases", releases.Load())
			}
			switch name {
			case "mask", "hidden", "empty":
				if got.err != nil || got.mask == nil {
					t.Fatal(got.err)
				}
				if got.mask.VoxelCount != body.VoxelCount || got.mask.Present != body.Present || got.mask.T != 3 || got.mask.OpenGeneration != 7 || got.mask.RegionID != "a" || got.mask.OriginZYX != [3]uint32{4, 5, 6} {
					t.Fatal(got.mask)
				}
				if (got.mask.Tensor == nil) != (body.Segment == nil) {
					t.Fatal("mask presence mismatch")
				}
			case "copy failure":
				if got.mask != nil || !errors.Is(got.err, copyFailure) {
					t.Fatal(got)
				}
			default:
				var he *control.HostError
				if got.mask != nil || !errors.As(got.err, &he) || he.Kind != pb.Error_PROTOCOL {
					t.Fatal(got)
				}
			}
		})
	}
}
