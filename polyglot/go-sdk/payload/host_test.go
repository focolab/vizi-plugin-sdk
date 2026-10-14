package payload

import (
	"context"
	"errors"
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

func TestOwnedReadsReleaseEveryDisposition(t *testing.T) {
	for _, kind := range []string{"volume", "mask"} {
		t.Run(kind, func(t *testing.T) {
			cases := []string{"success", "copy error", "bad shape", "budget", "byte order", "release error", "copy and release error", "bad shape and release error"}
			if kind == "mask" {
				cases = append(cases, "bad origin", "bad origin and release error")
			}
			for _, name := range cases {
				t.Run(name, func(t *testing.T) {
					copyFails := strings.HasPrefix(name, "copy")
					releaseFails := strings.Contains(name, "release")
					ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
					defer cancel()
					attached := make(chan *control.Client, 1)
					var releases atomic.Int32
					releaseErr := errors.New("release failed")
					copyErr := errors.New("copy failed")
					cfg := control.Config{BuildIdentity: "copy.test test build", PluginID: "copy.test", Handshake: func(*pb.Hello) error { return nil }, Attach: func(c *control.Client) error { attached <- c; return nil },
						Prepare: func(context.Context, *control.Client, *pb.HostMsg) error { return nil },
						Release: func(_ *control.Client, m *pb.HostMsg) error {
							if m.GetDataVolume() != nil || m.GetMaskVoxels() != nil {
								releases.Add(1)
							}
							if releaseFails {
								return releaseErr
							}
							return nil
						},
					}
					server, err := control.NewServer(cfg)
					if err != nil {
						t.Fatal(err)
					}
					listener := bufconn.Listen(1 << 20)
					grpcServer := server.GRPCServer()
					go grpcServer.Serve(listener)
					defer grpcServer.Stop()
					defer listener.Close()
					conn, err := grpc.NewClient("passthrough:///test", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }))
					if err != nil {
						t.Fatal(err)
					}
					defer conn.Close()
					stream, err := pb.NewPluginClient(conn).Session(ctx)
					if err != nil {
						t.Fatal(err)
					}
					if err := stream.Send(&pb.HostMsg{Id: "Hello from test host 1.0", Msg: &pb.HostMsg_Hello{Hello: &pb.Hello{ProtocolMajor: 1, ProtocolMinor: 0}}}); err != nil {
						t.Fatal(err)
					}
					if _, err := stream.Recv(); err != nil {
						t.Fatal(err)
					}
					var client *control.Client
					select {
					case client = <-attached:
					case <-ctx.Done():
						t.Fatal(ctx.Err())
					}
					options := CopyOptions{MaxBytes: 4, ByteOrder: LittleEndian}
					if name == "budget" {
						options.MaxBytes = 3
					}
					if name == "byte order" {
						options.ByteOrder = 0
					}
					copier := copyFunc(func(_ *pb.SegmentRef, b []byte) error {
						if copyFails {
							return copyErr
						}
						copy(b, []byte{1, 0, 2, 0})
						return nil
					})
					type answer struct {
						v   *Volume
						m   *Mask
						err error
					}
					done := make(chan answer, 1)
					go func() {
						if kind == "mask" {
							m, err := ReadMask(ctx, client, copier, &pb.ReadMaskVoxels{RegionId: "mask-7"}, options)
							done <- answer{m: m, err: err}
							return
						}
						v, err := ReadVolume(ctx, client, copier, &pb.ReadDataVolume{T: 5, C: 2}, options)
						done <- answer{v: v, err: err}
					}()
					request, err := stream.Recv()
					if err != nil {
						t.Fatal(err)
					}
					if (kind == "volume" && request.GetReadDataVolume() == nil) || (kind == "mask" && request.GetReadMaskVoxels() == nil) {
						t.Fatal(request)
					}
					d := &pb.SegmentRef{SegmentId: 7, Offset: 32, Len: 4, ShapeZyx: []uint32{1, 1, 2}, Dtype: pb.DType_DTYPE_U16}
					if strings.HasPrefix(name, "bad shape") {
						d.ShapeZyx = nil
					}
					reply := &pb.HostMsg{Id: request.Id, Msg: &pb.HostMsg_DataVolume{DataVolume: &pb.DataVolume{Segment: d, T: 5, C: 2, Level: 1, OpenGeneration: 19}}}
					if kind == "mask" {
						origin := []uint32{4, 5, 6}
						if strings.HasPrefix(name, "bad origin") {
							origin = nil
						}
						reply.Msg = &pb.HostMsg_MaskVoxels{MaskVoxels: &pb.MaskVoxels{Segment: d, OriginZyx: origin, RegionId: "mask-7", OpenGeneration: 19}}
					}
					if err := stream.Send(reply); err != nil {
						t.Fatal(err)
					}
					var got answer
					select {
					case got = <-done:
					case <-ctx.Done():
						t.Fatal(ctx.Err())
					}
					if releases.Load() != 1 {
						t.Fatal("reply use was not released exactly once", releases.Load())
					}
					if name == "success" && got.err != nil {
						t.Fatal(got.err)
					}
					if name != "success" && got.err == nil {
						t.Fatal("failure was lost")
					}
					if name == "success" || name == "release error" {
						var tensor *Tensor
						if kind == "volume" {
							if got.v == nil || got.v.T != 5 || got.v.C != 2 || got.v.Level != 1 || got.v.OpenGeneration != 19 {
								t.Fatal(got)
							}
							tensor = got.v.Tensor
						} else {
							if got.m == nil || got.m.OriginZYX != ([3]uint32{4, 5, 6}) || got.m.RegionID != "mask-7" || got.m.OpenGeneration != 19 {
								t.Fatal(got)
							}
							tensor = got.m.Tensor
						}
						values, err := Decode[uint16](tensor)
						if err != nil || len(values) != 2 || values[0] != 1 || values[1] != 2 {
							t.Fatal(values, err)
						}
					} else if got.v != nil || got.m != nil {
						t.Fatal("failed read returned data", got)
					}
					if releaseFails && !errors.Is(got.err, releaseErr) {
						t.Fatal(got.err)
					}
					if copyFails && !errors.Is(got.err, copyErr) {
						t.Fatal(got.err)
					}
				})
			}
		})
	}
}
