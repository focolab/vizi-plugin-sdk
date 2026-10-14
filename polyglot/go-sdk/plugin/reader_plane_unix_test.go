//go:build linux || darwin

package plugin

import (
	"bytes"
	"context"
	"encoding/binary"
	"hash/crc32"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/payload"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"golang.org/x/sys/unix"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/proto"
)

func TestVolumeOnlyReaderDerivesPlanesThroughSession(t *testing.T) {
	for _, registered := range []bool{false, true} {
		t.Run(map[bool]string{false: "raw", true: "registered"}[registered], func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
			defer cancel()
			typ := unix.SOCK_SEQPACKET
			if runtime.GOOS == "darwin" {
				typ = unix.SOCK_DGRAM
			}
			pair, err := unix.Socketpair(unix.AF_UNIX, typ, 0)
			if err != nil {
				t.Fatal(err)
			}
			defer unix.Close(pair[0])
			dir, err := os.MkdirTemp("", "vizi-plane-")
			if err != nil {
				t.Fatal(err)
			}
			defer os.RemoveAll(dir)
			path := filepath.Join(dir, "s")
			t.Setenv("VIZI_PLUGIN_DOORBELL_FD", strconv.Itoa(pair[1]))
			t.Setenv("VIZI_PLUGIN_UDS", path)
			var session atomic.Pointer[control.Session]
			var calls atomic.Int32
			volumeBytes := func(dtype pb.DType) []byte {
				width, _ := payload.SizeOf(dtype)
				data := make([]byte, 12*int(width))
				for i := range data {
					data[i] = byte(i + 1)
				}
				return data
			}
			handlers := ReaderHandlers{
				Open: func(_ context.Context, _ *control.Client, q *pb.OpenReader) (*pb.ReaderOpened, error) {
					dtype := pb.DType(pb.DType_value[q.Uri])
					if dtype == 0 {
						dtype = pb.DType_DTYPE_U8
					}
					return &pb.ReaderOpened{Handle: q.Uri, Dtype: dtype, ShapeTczyx: []uint32{3, 4, 3, 2, 2}}, nil
				},
				Volume: func(_ context.Context, _ *control.Client, q *pb.ReadVolume) (*pb.SegmentRef, error) {
					calls.Add(1)
					if q.T != 1 || q.C != 2 || q.Level != 0 {
						t.Error("plane request lost indices", q)
					}
					dtype := pb.DType(pb.DType_value[q.Handle])
					if dtype == 0 {
						dtype = pb.DType_DTYPE_U8
					}
					s := session.Load()
					key, err := s.WriteLoan(17, 0, volumeBytes(dtype))
					if err != nil {
						return nil, err
					}
					d := &pb.SegmentRef{SegmentId: 17, Offset: key.Offset, Len: key.Length, Dtype: dtype, ShapeZyx: []uint32{3, 2, 2}}
					if q.Handle == "bad-shape" {
						d.ShapeZyx = nil
					}
					switch q.Handle {
					case "bad-dtype":
						d.Dtype = pb.DType_DTYPE_U16
					case "bad-z":
						d.ShapeZyx[0] = 4
					case "bad-y":
						d.ShapeZyx[1] = 3
					case "bad-x":
						d.ShapeZyx[2] = 3
					}
					if q.Handle == "multi-use" {
						d.Uses = 2
					}
					return d, nil
				},
				Close: func(_ context.Context, _ *control.Client, q *pb.CloseReader) (*pb.Closed, error) {
					return &pb.Closed{Handle: q.Handle}, nil
				},
			}
			h := Handlers{Reader: handlers}
			if registered {
				h = Handlers{Readers: map[string]ReaderHandlers{"r": handlers}}
			}
			cfg, err := h.Configure(control.Config{PluginID: "plane.test"})
			if err != nil {
				t.Fatal(err)
			}
			started := make(chan struct{})
			done := make(chan error, 1)
			go func() {
				done <- control.ServeEnv(ctx, control.Plugin{Control: cfg, Started: func(s *control.Session) { session.Store(s); close(started) }})
			}()
			defer func() {
				cancel()
				select {
				case <-done:
				case <-time.After(time.Second):
					t.Error("session did not stop")
				}
			}()
			conn, err := grpc.NewClient("passthrough:///plane", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
				return (&net.Dialer{}).DialContext(ctx, "unix", path)
			}))
			if err != nil {
				t.Fatal(err)
			}
			defer conn.Close()
			stream, err := pb.NewPluginClient(conn).Session(ctx, grpc.WaitForReady(true))
			if err != nil {
				t.Fatal(err)
			}
			send := func(m *pb.HostMsg) {
				t.Helper()
				if err := stream.Send(m); err != nil {
					t.Fatal(err)
				}
			}
			recv := func() *pb.PluginMsg {
				t.Helper()
				m, err := stream.Recv()
				if err != nil {
					t.Fatal(err)
				}
				return m
			}
			send(&pb.HostMsg{Id: "Hello from plane test 1.0", Msg: &pb.HostMsg_Hello{Hello: &pb.Hello{ProtocolMajor: 1}}})
			if recv().GetReady() == nil {
				t.Fatal("not ready")
			}
			select {
			case <-started:
			case <-ctx.Done():
				t.Fatal(ctx.Err())
			}
			// G is granted after ReaderOpened, as in the host's launch contract.
			send(&pb.HostMsg{Id: "initial-open", Msg: &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{ReaderId: "r", Uri: "initial"}}})
			if recv().GetReaderOpened() == nil {
				t.Fatal("initial Open failed")
			}
			file, err := os.CreateTemp(dir, "g")
			if err != nil {
				t.Fatal(err)
			}
			defer file.Close()
			data := make([]byte, 256)
			binary.LittleEndian.PutUint64(data, 17)
			copy(data[12:28], "plane-proof-1234")
			binary.LittleEndian.PutUint32(data[28:32], crc32.ChecksumIEEE(data[:28]))
			if _, err := file.Write(data); err != nil {
				t.Fatal(err)
			}
			grant, err := doorbell.EncodeMemory(doorbell.Memory{SegmentID: 17, Size: 256, Permissions: doorbell.PluginRead | doorbell.PluginWrite}, 1)
			if err != nil {
				t.Fatal(err)
			}
			if err := unix.Sendmsg(pair[0], grant[:], unix.UnixRights(int(file.Fd())), nil, 0); err != nil {
				t.Fatal(err)
			}
			send(&pb.HostMsg{Msg: &pb.HostMsg_InstallSegment{InstallSegment: &pb.InstallSegment{SegmentId: 17, Purpose: "G", Regions: []*pb.Region{{Index: 0, Offset: 128, Extent: 128, Discipline: pb.RegionDiscipline_REGION_DISCIPLINE_LOANED, Producer: pb.RegionProducer_REGION_PRODUCER_PLUGIN}}}}})
			if recv().GetGrantAck().GetInstalled() == nil {
				t.Fatal("G not installed")
			}
			seg, err := session.Load().Memory.Registry.Segment(17)
			if err != nil {
				t.Fatal(err)
			}
			for _, name := range []string{"DTYPE_U8", "DTYPE_U16", "DTYPE_F32", "DTYPE_F64", "bad-shape", "bad-dtype", "bad-z", "bad-y", "bad-x", "multi-use"} {
				send(&pb.HostMsg{Id: "open", Msg: &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{ReaderId: "r", Uri: name}}})
				opened := recv().GetReaderOpened()
				if opened == nil {
					t.Fatal("no reader")
				}
				for _, z := range []uint32{1, 2, 3} {
					send(&pb.HostMsg{Id: "plane", Msg: &pb.HostMsg_ReadPlane{ReadPlane: &pb.ReadPlane{Handle: opened.Handle, T: 1, C: 2, Z: z}}})
					out := recv()
					if strings.HasPrefix(name, "bad-") || name == "multi-use" || z == 3 {
						if out.GetError() == nil {
							t.Fatal("bad plane accepted", out)
						}
						if strings.HasPrefix(name, "bad-") && (!registered || z < 3) && (out.GetError().Kind != pb.Error_INTERNAL || !strings.Contains(out.GetError().Message, "got shape_zyx=") || !strings.Contains(out.GetError().Message, "expected shape_zyx=[3 2 2] dtype=DTYPE_U8")) {
							t.Fatal("missing producer geometry diagnostic", out)
						}
						if z == 3 && out.GetError().Kind != pb.Error_OUT_OF_BOUNDS && !strings.HasPrefix(name, "bad-") && name != "multi-use" {
							t.Fatal(out)
						}
						if stats, _ := seg.LoanStats(0); stats.Outstanding != 0 {
							t.Fatal("failed plane leaked volume", stats)
						}
						continue
					}
					dtype := pb.DType(pb.DType_value[name])
					width, _ := payload.SizeOf(dtype)
					d := out.GetSegment()
					if d == nil || !proto.Equal(d, &pb.SegmentRef{SegmentId: 17, Offset: 128, Len: 4 * width, Dtype: dtype, ShapeZyx: []uint32{1, 2, 2}}) {
						t.Fatal(out)
					}
					actual := make([]byte, d.Len)
					if _, err := file.ReadAt(actual, int64(d.Offset)); err != nil {
						t.Fatal(err)
					}
					want := volumeBytes(dtype)[uint64(z)*d.Len : uint64(z+1)*d.Len]
					if !bytes.Equal(actual, want) {
						t.Fatal(actual, want)
					}
					send(&pb.HostMsg{Msg: &pb.HostMsg_ReleaseLoan{ReleaseLoan: &pb.ReleaseLoan{SegmentId: 17, Region: 0, Offset: d.Offset, Len: d.Len}}})
					send(&pb.HostMsg{Id: "barrier", Msg: &pb.HostMsg_GrantStateQuery{GrantStateQuery: &pb.GrantStateQuery{SegmentId: 17}}})
					if recv().GetGrantState() == nil {
						t.Fatal("missing barrier")
					}
					if stats, _ := seg.LoanStats(0); stats.Outstanding != 0 || stats.Cursor != 0 {
						t.Fatal(stats)
					}
				}
				before := calls.Load()
				send(&pb.HostMsg{Id: "level", Msg: &pb.HostMsg_ReadPlane{ReadPlane: &pb.ReadPlane{Handle: opened.Handle, Level: 1}}})
				if recv().GetError().GetKind() != pb.Error_OUT_OF_BOUNDS || calls.Load() != before {
					t.Fatal("level checked after volume callback")
				}
			}
			if stats, _ := seg.LoanUseStats(0); stats != (memory.LoanUseStats{}) {
				t.Fatal(stats)
			}
			if got := session.Load().Memory.CrossedReleases(); got != 0 {
				t.Fatal("ordinary host releases counted as crossings", got)
			}
			// Exercise the actual ServeEnv memory-reporting adapter. Retire a
			// sent loan, then deliver its late release quietly.
			send(&pb.HostMsg{Id: "open-retiring", Msg: &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{ReaderId: "r", Uri: "DTYPE_U8"}}})
			retiring := recv().GetReaderOpened()
			if retiring == nil {
				t.Fatal("missing retiring reader")
			}
			send(&pb.HostMsg{Id: "retiring-volume", Msg: &pb.HostMsg_ReadVolume{ReadVolume: &pb.ReadVolume{Handle: retiring.Handle, T: 1, C: 2}}})
			loan := recv().GetSegment()
			if loan == nil {
				t.Fatal("missing loan to retire")
			}
			send(&pb.HostMsg{Msg: &pb.HostMsg_RetireSegment{RetireSegment: &pb.RetireSegment{SegmentId: 17}}})
			if out := recv(); out.GetSegmentReleased().GetSegmentId() != 17 {
				t.Fatal(out)
			}
			send(&pb.HostMsg{Msg: &pb.HostMsg_ReleaseLoan{ReleaseLoan: &pb.ReleaseLoan{SegmentId: 17, Region: 0, Offset: loan.Offset, Len: loan.Len}}})
			send(&pb.HostMsg{Id: "retired-state", Msg: &pb.HostMsg_GrantStateQuery{GrantStateQuery: &pb.GrantStateQuery{SegmentId: 17}}})
			if out := recv(); out.Id != "retired-state" || out.GetGrantState().GetState() != pb.GrantStateKind_GRANT_STATE_RELEASED {
				t.Fatal("late release revived segment or ended session", out)
			}
			if got := session.Load().Memory.CrossedReleases(); got != 1 {
				t.Fatal("crossing release was not counted exactly once", got)
			}

		})
	}
}
