//go:build linux || darwin

package plugin

import (
	"bytes"
	"context"
	"encoding/binary"
	"hash/crc32"
	"image"
	"image/png"
	"math"
	"net"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strconv"
	"testing"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"golang.org/x/sys/unix"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

// Exercise the real launch path: epoch arrangements must not depend on any
// tool declaration, binding or permission. Also drive the declared progress
// panel through its dedicated wire operation, and a row image through a P loan.
func TestEpochSubscriptionWithoutToolsAndProgress(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	sockType := unix.SOCK_SEQPACKET
	if runtime.GOOS == "darwin" {
		sockType = unix.SOCK_DGRAM
	}
	pair, err := unix.Socketpair(unix.AF_UNIX, sockType, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer unix.Close(pair[0])
	dir, err := os.MkdirTemp("", "vizi-epochs-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(dir)
	path := filepath.Join(dir, "s")
	t.Setenv("VIZI_PLUGIN_DOORBELL_FD", strconv.Itoa(pair[1]))
	t.Setenv("VIZI_PLUGIN_UDS", path)
	d := definition()
	d.Subscribe(Epochs, "future_subscription", Epochs)
	panel := d.Panel(PanelSpec{ID: "p", Title: "Work", Root: Progress("bar", .25, true)}, nil)
	images := d.Panel(PanelSpec{ID: "images", Title: "Images", Root: ListRows(ListSpec{ID: "rows"}, Image("preview"))}, nil)
	app := buildDefinition(t, d)
	raw, err := app.Manifest("./plugin")
	if err != nil {
		t.Fatal(err)
	}
	m := decodeManifest(t, raw)
	if !reflect.DeepEqual(m.Subscriptions, []string{Epochs, "future_subscription"}) || len(m.Contributions.Tools) != 0 || len(m.Contributions.Commands) != 0 || len(m.Permissions) != 0 {
		t.Fatal(string(raw))
	}
	if w := m.Contributions.Panels[0].Root; w.Kind != "progress" || w.Value != .25 || !w.Indeterminate || w.Command != "" {
		t.Fatal(w)
	}
	started := make(chan *control.Session, 1)
	cfg, err := app.Configure(control.Plugin{Started: func(s *control.Session) { started <- s }})
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { done <- control.ServeEnv(ctx, cfg) }()
	conn, err := grpc.NewClient("passthrough:///epochs", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
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
	send(&pb.HostMsg{Id: "Hello from test host 1.0", Msg: &pb.HostMsg_Hello{Hello: &pb.Hello{ProtocolMajor: 1}}})
	if recv().GetReady() == nil {
		t.Fatal("no Ready")
	}
	var session *control.Session
	select {
	case session = <-started:
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
	epoch := &pb.EpochChange{Datasets: []*pb.DatasetState{{Gen: 7, Geometry: &pb.Geometry{Gen: 7}}}, Panes: []*pb.PaneEpoch{{ViewId: 0, Epoch: 1, Gen: 7}}}
	for i := 0; i < 2; i++ { // idempotent replay still acknowledges
		send(&pb.HostMsg{Msg: &pb.HostMsg_EpochChange{EpochChange: epoch}})
		ack := recv().GetEpochAck()
		if ack == nil || len(ack.Panes) != 1 || ack.Panes[0].Epoch != 1 {
			t.Fatal(ack)
		}
		if got := session.State.Pane(0); got == nil || got.Dataset.Gen != 7 {
			t.Fatal(got)
		}
	}
	for _, tc := range []struct {
		value         float64
		indeterminate bool
		want          float64
	}{{.6, false, .6}, {-1, false, 0}, {2, false, 1}, {math.NaN(), true, 0}, {math.NaN(), false, 0}, {math.Inf(1), false, 1}, {math.Inf(-1), false, 0}, {math.Inf(1), true, 0}, {-100, true, 0}, {.8, false, .8}} {
		if err := panel.Bind(session.Client).SetProgress("bar", tc.value, tc.indeterminate); err != nil {
			t.Fatal(err)
		}
		patch := recv().GetPanelPatch()
		if patch == nil || patch.PanelId != panel.ID() || len(patch.Ops) != 1 || patch.Ops[0].WidgetId != "bar" {
			t.Fatal(patch)
		}
		progress := patch.Ops[0].GetSetProgress()
		if progress == nil || progress.Value != tc.want || progress.Indeterminate != tc.indeterminate {
			t.Fatal(progress)
		}
	}
	// Row-addressed PNGs use the same real grant, admission and release path.
	segment := uint64(7)<<32 | 1
	file, err := os.CreateTemp(dir, "panel-image")
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	data := make([]byte, 8192)
	binary.LittleEndian.PutUint64(data, segment)
	copy(data[12:28], "list-proof-12345")
	binary.LittleEndian.PutUint32(data[28:32], crc32.ChecksumIEEE(data[:28]))
	if _, err := file.Write(data); err != nil {
		t.Fatal(err)
	}
	send(&pb.HostMsg{Msg: &pb.HostMsg_InstallSegment{InstallSegment: &pb.InstallSegment{
		SegmentId: segment, Purpose: "P", Subject: images.ID(), Regions: []*pb.Region{{
			Index: 0, Offset: 32, Extent: 8192 - 32, SlotBytes: 64,
			Discipline: pb.RegionDiscipline_REGION_DISCIPLINE_LOANED, Producer: pb.RegionProducer_REGION_PRODUCER_PLUGIN,
		}},
	}}})
	grant, err := doorbell.EncodeMemory(doorbell.Memory{SegmentID: segment, Size: 8192, Permissions: doorbell.PluginRead | doorbell.PluginWrite | doorbell.HostRead}, uint32(segment))
	if err != nil {
		t.Fatal(err)
	}
	if err := unix.Sendmsg(pair[0], grant[:], unix.UnixRights(int(file.Fd())), nil, 0); err != nil {
		t.Fatal(err)
	}
	if ack := recv().GetGrantAck(); ack.GetSegmentId() != segment || ack.GetInstalled() == nil {
		t.Fatal(ack)
	}
	if err := images.Bind(session.Client).SetRows("rows", "result"); err != nil {
		t.Fatal(err)
	}
	if patch := recv().GetPanelPatch(); patch == nil || len(patch.Ops) != 1 || len(patch.Ops[0].GetSetRows().GetKeys()) != 1 {
		t.Fatal(patch)
	}
	var encoded bytes.Buffer
	if err := png.Encode(&encoded, image.NewRGBA(image.Rect(0, 0, 1, 1))); err != nil {
		t.Fatal(err)
	}
	if err := images.Bind(session.Client).Row("result").SetPNG(session, 0, "preview", encoded.Bytes()); err != nil {
		t.Fatal(err)
	}
	patch := recv().GetPanelPatch()
	if patch == nil || patch.PanelId != images.ID() || len(patch.Ops) != 1 || patch.Ops[0].WidgetId != "preview" || patch.Ops[0].RowKey != "result" {
		t.Fatal(patch)
	}
	ref := patch.Ops[0].GetSetImage()
	if ref == nil || ref.SegmentId != segment || ref.Len != uint64(encoded.Len()) {
		t.Fatal(ref)
	}
	got := make([]byte, ref.Len)
	if _, err := file.ReadAt(got, int64(ref.Offset)); err != nil || !bytes.Equal(got, encoded.Bytes()) {
		t.Fatal("published image differs", err)
	}
	seg, err := session.Memory.Registry.FindContribution(memory.PurposePanel, images.ID())
	if err != nil {
		t.Fatal(err)
	}
	if stats, err := seg.LoanStats(0); err != nil || stats.Outstanding != 1 {
		t.Fatal(stats, err)
	}
	send(&pb.HostMsg{Msg: &pb.HostMsg_ReleaseLoan{ReleaseLoan: &pb.ReleaseLoan{SegmentId: segment, Region: 0, Offset: ref.Offset, Len: ref.Len}}})
	// Arrangement reply is a receive-order barrier, not a timing assumption.
	send(&pb.HostMsg{Id: "after-release", Msg: &pb.HostMsg_GrantStateQuery{GrantStateQuery: &pb.GrantStateQuery{SegmentId: segment}}})
	if reply := recv(); reply.Id != "after-release" || reply.GetGrantState() == nil {
		t.Fatal(reply)
	}
	if stats, err := seg.LoanStats(0); err != nil || stats.Outstanding != 0 {
		t.Fatal(stats, err)
	}
	if err := stream.CloseSend(); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
}
