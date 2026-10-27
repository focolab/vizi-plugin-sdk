//go:build linux || darwin

package plugin

import (
	"context"
	"encoding/binary"
	"errors"
	"hash/crc32"
	"os"
	"runtime"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/payload"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"golang.org/x/sys/unix"
)

func TestDeclaredExporterCopiesArrivalClaimWhileRetiring(t *testing.T) {
	r, err := memory.New(memory.Config{})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { r.Close() })
	m := control.NewMemory(r)
	c, stream := listTestSession(t, control.Config{PluginID: "export.test"})
	f, err := os.CreateTemp(t.TempDir(), "export")
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	data := make([]byte, 256)
	binary.LittleEndian.PutUint64(data, 17)
	copy(data[12:28], "identity-proof!!")
	binary.LittleEndian.PutUint32(data[28:32], crc32.ChecksumIEEE(data[:28]))
	copy(data[64:68], []byte{1, 3, 5, 7})
	if _, err = f.Write(data); err != nil {
		t.Fatal(err)
	}
	if err = m.Install(c, &pb.InstallSegment{SegmentId: 17, Purpose: "X", Regions: []*pb.Region{{Offset: 64, Extent: 192, Discipline: pb.RegionDiscipline_REGION_DISCIPLINE_LOANED, Producer: pb.RegionProducer_REGION_PRODUCER_HOST}}}); err != nil {
		t.Fatal(err)
	}
	kind := unix.SOCK_SEQPACKET
	if runtime.GOOS == "darwin" {
		kind = unix.SOCK_DGRAM
	}
	pair, err := unix.Socketpair(unix.AF_UNIX, kind, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer unix.Close(pair[0])
	granted := make(chan error, 1)
	db, err := doorbell.Adopt(pair[1], doorbell.Config{Handler: func(e doorbell.Event) {
		if e.Frame != nil {
			granted <- m.Grant(c, e.Frame)
		}
	}})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { db.Stop(); db.Wait(c.Context()) }()
	raw, err := doorbell.EncodeMemory(doorbell.Memory{SegmentID: 17, Size: 256, Permissions: doorbell.PluginRead}, 1)
	if err != nil {
		t.Fatal(err)
	}
	if err = unix.Sendmsg(pair[0], raw[:], unix.UnixRights(int(f.Fd())), nil, 0); err != nil {
		t.Fatal(err)
	}
	select {
	case err = <-granted:
		if err != nil {
			t.Fatal(err)
		}
	case <-c.Context().Done():
		t.Fatal(c.Context().Err())
	}
	if ack, err := stream.Recv(); err != nil || ack.GetGrantAck().GetInstalled() == nil {
		t.Fatal(ack, err)
	}
	opened := &pb.OpenExport{ExporterId: "e", ShapeTczyx: []uint32{1, 1, 1, 1, 4}, Dtype: pb.DType_DTYPE_U8, LittleEndian: true}
	d := definition()
	d.Exporter(ExporterSpec{ID: "e", Extension: "raw"}, ExportHandlers{
		Open: func(context.Context, *control.Client, *pb.OpenExport) (*pb.ExportOpened, error) {
			return &pb.ExportOpened{Handle: "local"}, nil
		},
		Chunk: func(_ context.Context, _ *control.Client, q *pb.ExportChunk) (*pb.ChunkWritten, error) {
			if q.Handle != "local" {
				t.Fatal(q.Handle)
			}
			v, err := payload.CopyExportChunk(m, opened, q, 4)
			if err != nil {
				return nil, err
			}
			if string(v.Bytes()) != string([]byte{1, 3, 5, 7}) {
				t.Fatal(v.Bytes())
			}
			return &pb.ChunkWritten{Handle: q.Handle, TotalBytes: 4}, nil
		},
		Finish: func(context.Context, *control.Client, *pb.FinishExport) (*pb.ExportDone, error) {
			return &pb.ExportDone{}, nil
		},
		Abort: func(context.Context, *control.Client, *pb.AbortExport) (*pb.ExportDone, error) {
			return &pb.ExportDone{}, nil
		},
	})
	cfg, err := buildDefinition(t, d).Configure(control.Plugin{})
	if err != nil {
		t.Fatal(err)
	}
	out, err := cfg.Control.HandleRequest(c.Context(), c, &pb.HostMsg{Msg: &pb.HostMsg_OpenExport{OpenExport: opened}})
	if err != nil {
		t.Fatal(err)
	}
	wire := out.GetExportOpened().Handle
	in := &pb.HostMsg{Msg: &pb.HostMsg_ExportChunk{ExportChunk: &pb.ExportChunk{Handle: wire, Channels: []uint32{0}, Source: &pb.ExportChunk_Segment{Segment: &pb.SegmentRef{SegmentId: 17, Offset: 64, Len: 4, ShapeZyx: []uint32{1, 1, 4}, Dtype: pb.DType_DTYPE_U8}}}}}
	if err = m.Admit(c, in); err != nil {
		t.Fatal(err)
	}
	if _, err = r.Retire(17); !errors.Is(err, memory.ErrLoanActive) {
		t.Fatal(err)
	}
	out, err = cfg.Control.HandleRequest(c.Context(), c, in)
	if err != nil || out.GetChunkWritten().GetHandle() != wire {
		t.Fatal(out, err)
	}
	if in.GetExportChunk().Handle != wire {
		t.Fatal("router mutated original handle")
	}
	if err = m.Release(c, in); err != nil {
		t.Fatal(err)
	}
	if update, err := r.SettleRetirement(17); err != nil || len(update.Released) != 1 {
		t.Fatal(update, err)
	}
}
