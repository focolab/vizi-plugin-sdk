//go:build linux || darwin

package plugin

import (
	"context"
	"encoding/binary"
	"errors"
	"hash/crc32"
	"math"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/geometry"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/payload"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"golang.org/x/sys/unix"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/proto"
)

// This drives the public authoring API through ServeEnv, real M capability
// transfer and real gRPC installs. The peer reads slot bytes independently.
func TestTwoLiveOverlaysThroughSession(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	socketType := unix.SOCK_SEQPACKET
	if runtime.GOOS == "darwin" {
		socketType = unix.SOCK_DGRAM
	}
	pair, err := unix.Socketpair(unix.AF_UNIX, socketType, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer unix.Close(pair[0])
	dir, err := os.MkdirTemp("", "vizi-live-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(dir)
	path := filepath.Join(dir, "plugin.sock")
	t.Setenv("VIZI_PLUGIN_DOORBELL_FD", strconv.Itoa(pair[1]))
	t.Setenv("VIZI_PLUGIN_UDS", path)
	events := make(chan doorbell.Event, 32)
	started := make(chan *control.Session, 1)
	done := make(chan error, 1)
	go func() {
		done <- control.ServeEnv(ctx, control.Plugin{Control: control.Config{PluginID: "live.test"}, Started: func(s *control.Session) { started <- s }, DoorbellHandler: func(_ *control.Session, e doorbell.Event) { events <- e }})
	}()
	conn, err := grpc.NewClient("passthrough:///live", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
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
	if ready := recv(); ready.GetReady() == nil || !strings.HasPrefix(ready.Id, "Ready; I am live.test;") || !strings.Contains(ready.Id, "vizi-go-sdk") {
		t.Fatal("missing Ready")
	}
	var session *control.Session
	select {
	case session = <-started:
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
	if session.Client.HostIdentity() != "Hello from test host 1.0" {
		t.Fatal("host build identity unavailable")
	}
	// Assigned triggers and actions must survive the actual protobuf stream.
	triggers := []*pb.Trigger{{Button: pb.MouseButton_MOUSE_BUTTON_MIDDLE, Mods: 1, Hold: pb.TriggerHold_TRIGGER_HOLD_MOMENTARY}, {Key: "Ctrl+J", Hold: pb.TriggerHold_TRIGGER_HOLD_TOGGLE}}
	for i, trigger := range triggers {
		tool := uint32(i + 1)
		binding := &pb.ToolBinding{Tool: tool, ToolId: "paint", Revision: 1, Mode: pb.ToolMode_TOOL_MODE_GESTURE, Triggers: []*pb.Trigger{trigger}, Actions: []*pb.ToolAction{{Action: 1, ActionId: "node", Key: "N"}, {Action: 2, ActionId: "finish"}}}
		send(&pb.HostMsg{Msg: &pb.HostMsg_ToolBinding{ToolBinding: binding}})
		if ack := recv().GetToolBindingAck(); ack.GetTool() != tool || ack.GetRevision() != 1 || !proto.Equal(session.State.Tool(tool), binding) {
			t.Fatal("modal binding lost", ack)
		}
	}
	const generation = uint64(3)<<32 | 7
	send(&pb.HostMsg{Msg: &pb.HostMsg_TimeMapping{TimeMapping: &pb.TimeMapping{Gen: generation, Revision: 1, T: []float64{0, 0.5}}}})
	if ack := recv().GetTimeMappingAck(); ack.GetGen() != generation || ack.GetRevision() != 1 {
		t.Fatal("mapping not acknowledged", ack)
	}
	for view := uint32(0); view < 2; view++ {
		dataset := &pb.DatasetState{Gen: generation, TimeMappingRevision: 1, Geometry: &pb.Geometry{Gen: generation, SpacingZ: 1, SpacingY: 1, SpacingX: 1, SpacingProvenance: pb.SpacingProvenance_SPACING_PROVENANCE_DISPLAY_FALLBACK, Levels: []*pb.LevelScale{{Z: 1, Y: 1, X: 1}, {Z: 2, Y: 2, X: 2}}}}
		dataset.Geometry.Crop = &pb.EdgeBox{Z0: -0.5, Z1: 9.5, Y0: -0.5, Y1: 19.5, X0: -0.5, X1: 29.5}
		send(&pb.HostMsg{Msg: &pb.HostMsg_EpochChange{EpochChange: &pb.EpochChange{Datasets: []*pb.DatasetState{dataset}, Panes: []*pb.PaneEpoch{{ViewId: view, Epoch: 9, Gen: generation}}}}})
		if ack := recv().GetEpochAck(); len(ack.GetPanes()) != 1 || ack.Panes[0].ViewId != view || ack.Panes[0].Epoch != 9 {
			t.Fatal("pane not acknowledged", ack)
		}
		if pane := session.State.Pane(uint8(view)); !proto.Equal(pane.Dataset, dataset) || pane.Time.Revision != 1 {
			t.Fatal("incomplete pane snapshot", pane)
		}
	}
	for i, id := range []string{"left", "right"} {
		segment := uint64(7)<<32 | uint64(i+1) // session epoch and index both start at 1
		file, err := os.CreateTemp(dir, "mapping")
		if err != nil {
			t.Fatal(err)
		}
		defer file.Close()
		data := make([]byte, 8192)
		binary.LittleEndian.PutUint64(data, segment)
		copy(data[12:28], "live-proof-12345")
		binary.LittleEndian.PutUint32(data[28:32], crc32.ChecksumIEEE(data[:28]))
		if _, err := file.Write(data); err != nil {
			t.Fatal(err)
		}
		region := &pb.Region{Index: 0, Offset: 32, Extent: 3 * (16 + 1024), Discipline: pb.RegionDiscipline_REGION_DISCIPLINE_PULSED, Producer: pb.RegionProducer_REGION_PRODUCER_PLUGIN, Buffers: 3, SlotBytes: 1024}
		tail := uint64(1016)
		region.EpochOffset = &tail
		pane := &memory.PaneEpoch{ViewID: uint8(i), Epoch: 9}
		arena := &pb.Region{Index: 1, Offset: 3200, Extent: 8192 - 3200, Discipline: pb.RegionDiscipline_REGION_DISCIPLINE_LOANED, Producer: pb.RegionProducer_REGION_PRODUCER_PLUGIN, SlotBytes: 64}
		install := &pb.HostMsg{Msg: &pb.HostMsg_InstallSegment{InstallSegment: &pb.InstallSegment{SegmentId: segment, Purpose: "O", Subject: id, Regions: []*pb.Region{region, arena}}}}
		grant, err := doorbell.EncodeMemory(doorbell.Memory{SegmentID: segment, Size: 8192, Permissions: doorbell.PluginRead | doorbell.PluginWrite | doorbell.HostRead}, uint32(segment))
		if err != nil {
			t.Fatal(err)
		}
		grantNow := func() {
			t.Helper()
			if err := unix.Sendmsg(pair[0], grant[:], unix.UnixRights(int(file.Fd())), nil, 0); err != nil {
				t.Fatal(err)
			}
		}
		if i == 0 {
			send(install)
			grantNow()
		} else {
			grantNow()
			send(install)
		}
		ack := recv().GetGrantAck()
		if ack.GetSegmentId() != segment || string(ack.GetInstalled().GetIdentityProof()) != "live-proof-12345" {
			t.Fatal("grant did not reach usable mapping", ack)
		}
		overlay, err := NewOverlay(session.Client, id)
		if err != nil {
			t.Fatal(err)
		}
		label, err := TextItem("label", "Picked voxel", geometry.ZYX{1, 5, 9}, TextOptions{SizePX: 16, ColorRGBA: &[4]uint8{255, 100, 0, 255}})
		if err != nil {
			t.Fatal(err)
		}
		mesh, err := MeshItem("sphere", Sphere, geometry.ZYX{2, 6, 10}, MeshOptions{Frame: "world", Size: 2.5})
		if err != nil {
			t.Fatal(err)
		}
		if err := overlay.Replace(label, mesh); err != nil {
			t.Fatal(err)
		}
		if u := recv().GetOverlayUpdate(); u.GetRevision() != 1 || u.GetOverlayId() != id || len(u.Items) != 2 || !proto.Equal(u.Items[0], label) || !proto.Equal(u.Items[1], mesh) {
			t.Fatal(u)
		}
		point := geometry.ZYX{float64(i + 1), 5, 9}
		marker, err := (GeometryEncoder{ByteOrder: payload.LittleEndian}).Markers("picked", []geometry.ZYX{point})
		if err != nil {
			t.Fatal(err)
		}
		sample, err := overlay.ReplaceLive(session, 0, pane, marker) // automatic subject lookup
		if err != nil || sample.Revision != 1 {
			t.Fatal(sample, err)
		}
		// Read the real D bytes so an empty/wrong mask cannot hide behind polling.
		fds := []unix.PollFd{{Fd: int32(pair[0]), Events: unix.POLLIN}}
		if n, err := unix.Poll(fds, 1000); err != nil || n != 1 {
			t.Fatal("live publication did not ring the doorbell", n, err)
		}
		var hint [113]byte
		n, _, err := unix.Recvfrom(pair[0], hint[:], unix.MSG_DONTWAIT)
		if err != nil || n != 112 || hint[0] != 'D' || hint[1] != 2 || binary.LittleEndian.Uint64(hint[8:16]) != segment || hint[16] != 1 || binary.LittleEndian.Uint32(hint[80:84]) != pane.Epoch || hint[84] != pane.ViewID || crc32.ChecksumIEEE(hint[:108]) != binary.LittleEndian.Uint32(hint[108:112]) {
			t.Fatal("bad live notification", hint, n, err)
		}
		for _, b := range hint[17:80] {
			if b != 0 {
				t.Fatal("notification named an unrelated region")
			}
		}
		// Slot rotation starts at 1. The independent peer checks both revision spaces.
		if _, err := file.ReadAt(data, 0); err != nil {
			t.Fatal(err)
		}
		if binary.LittleEndian.Uint64(data[48:56]) != 1 {
			t.Fatal("slot revision confused with overlay revision")
		}
		length := binary.LittleEndian.Uint64(data[56:64])
		if length != uint64(sample.Length) || length > 1016 {
			t.Fatal("bad published length", length)
		}
		start := uint64(32 + 3*16 + 1024)
		update := &pb.OverlayUpdate{}
		if err := proto.Unmarshal(data[start:start+length], update); err != nil {
			t.Fatal("payload has an envelope/header", err)
		}
		if update.OverlayId != id || update.Revision != 2 {
			t.Fatal("wrong lane or overlay revision", update)
		}
		if len(update.Items) != 1 || update.Items[0].ItemId != "picked" || update.Items[0].GetTransform().GetFrame() != "voxel" {
			t.Fatal("missing live marker", update)
		}
		positions := update.Items[0].GetMarker().GetPositionsZyx()
		if positions.GetDtype() != pb.DType_DTYPE_F64 || len(positions.GetShape()) != 2 || positions.Shape[0] != 1 || positions.Shape[1] != 3 || len(positions.GetInline()) != 24 {
			t.Fatal("invalid live geometry", positions)
		}
		for axis, want := range point {
			if got := math.Float64frombits(binary.LittleEndian.Uint64(positions.GetInline()[axis*8:])); got != want {
				t.Fatal("live coordinate changed", axis, got, want)
			}
		}
		if binary.LittleEndian.Uint32(data[start+1016:]) != uint32(i) || binary.LittleEndian.Uint32(data[start+1020:]) != 9 {
			t.Fatal("pane header not at slot tail")
		}
		if err := overlay.Clear(); err != nil {
			t.Fatal(err)
		}
		if u := recv().GetOverlayRemove(); u.GetRevision() != 3 || u.GetOverlayId() != id {
			t.Fatal(u)
		}
		// A sender-side contract refusal must not advance the slot's publication count.
		bad := &pb.OverlayUpdate{OverlayId: "wrong", Revision: 99}
		if _, err := session.PublishLiveOverlay(segment, bad, pane); err == nil {
			t.Fatal("wrong subject published")
		}
		bad.OverlayId = id
		bad.Items = []*pb.OverlayItem{{Geometry: &pb.OverlayItem_Marker{Marker: &pb.OverlayMarker{PositionsZyx: &pb.GeomBuffer{Source: &pb.GeomBuffer_Segment{Segment: &pb.SegmentRef{SegmentId: segment, Len: 4}}}}}}}
		if _, err := session.PublishLiveOverlay(segment, bad, pane); err == nil {
			t.Fatal("loan descriptor published live")
		}
		large, err := (GeometryEncoder{ByteOrder: payload.LittleEndian}).Markers("large", make([]geometry.ZYX, 100))
		if err != nil {
			t.Fatal(err)
		}
		bad.Items = []*pb.OverlayItem{large}
		if _, err := session.PublishLiveOverlay(segment, bad, pane); !errors.Is(err, memory.ErrBounds) || !strings.Contains(err.Error(), "1016 bytes") {
			t.Fatal("capacity refusal must name the granted capacity", err)
		}
		seg, err := session.Memory.Registry.Segment(segment)
		if err != nil {
			t.Fatal(err)
		}
		if stats, _ := seg.PublishingStats(0); stats.LastRevision != 1 {
			t.Fatal("invalid update touched lane", stats)
		}
		testLabelVolumeLoans(t, session, overlay, seg, file, send, recv)
		// The real ServeEnv wiring must count bulk loan admission too, not just
		// the explicit Memory.AdmitOutbound configurations used by peer tests.
		bulk := proto.Clone(marker).(*pb.OverlayItem)
		bytes := bulk.GetMarker().PositionsZyx.GetInline()
		if err := session.PublishLoan(segment, memory.PurposeOverlay, bytes, func(d *pb.SegmentRef) (*pb.PluginMsg, error) {
			bulk.GetMarker().PositionsZyx.Source = &pb.GeomBuffer_Segment{Segment: d}
			return &pb.PluginMsg{Msg: &pb.PluginMsg_OverlayUpdate{OverlayUpdate: &pb.OverlayUpdate{OverlayId: id, Revision: overlay.revision + 1, Items: []*pb.OverlayItem{bulk}}}}, nil
		}); err != nil {
			t.Fatal(err)
		}
		sent := recv()
		ref := sent.GetOverlayUpdate().GetItems()[0].GetMarker().GetPositionsZyx().GetSegment()
		if err := session.Client.Send(sent); !errors.Is(err, memory.ErrUsesExceeded) {
			t.Fatal("ServeEnv did not enforce uses", err)
		}
		if err := seg.ReleaseLoan(memory.Deposit{SegmentID: segment, Region: 1, Offset: ref.Offset, Length: ref.Len}); !errors.Is(err, memory.ErrLoanAdmitted) {
			t.Fatal(err)
		}
		send(&pb.HostMsg{Msg: &pb.HostMsg_RetireSegment{RetireSegment: &pb.RetireSegment{SegmentId: segment}}})
		if recv().GetSegmentReleased().GetSegmentId() != segment {
			t.Fatal("retirement not acknowledged")
		}
		if stats, _ := seg.LoanStats(1); stats.Outstanding != 0 {
			t.Fatal("retirement stranded loan", stats)
		}
		if stats, _ := seg.LoanUseStats(1); stats != (memory.LoanUseStats{}) {
			t.Fatal("retirement retained sender counts", stats)
		}
	}
	// E shapes independently encoded from the published offsets.
	// The sequence gap models a refused disposable DRAG, which burns its number.
	inputs := []doorbell.Input{
		{Generation: 7, Interaction: 1, Epoch: 9, Tool: 1, Channels: 1, Phase: doorbell.Press, ViewID: 1, Position: doorbell.Position{Kind: doorbell.RayAndPicked, Values: [12]float32{1, 2, 3, 10, 20, 30, 0.25, 0.5, 4, 5, 6, 0.75}}},
		{Generation: 7, Interaction: 2, Epoch: 9, Tool: 1, Channels: 1, Phase: doorbell.Fire, Action: 1, ViewID: 1, Position: doorbell.Position{Kind: doorbell.Voxel, Values: [12]float32{4, 5, 6}}},
		{Generation: 7, Interaction: 1, Sequence: 2, Epoch: 9, Tool: 1, Channels: 1, Phase: doorbell.Drag, ViewID: 1, Position: doorbell.Position{Kind: doorbell.Ray, Flags: doorbell.Miss, Values: [12]float32{1, 2, 3, 10, 20, 30, 0.25, 0.5}}},
		{Generation: 7, Interaction: 1, Sequence: 3, Epoch: 9, Tool: 1, Phase: doorbell.Release, ViewID: 1, Level: 3},
		{Generation: 7, Interaction: 3, Epoch: 9, Tool: 2, Channels: 1, Phase: doorbell.Press, ViewID: 0, Modifiers: doorbell.Ctrl, Position: doorbell.Position{Kind: doorbell.Voxel, Values: [12]float32{4, 5, 6}}},
	}
	nextEvent := func() doorbell.Event {
		t.Helper()
		select {
		case e := <-events:
			return e
		case <-ctx.Done():
			t.Fatal(ctx.Err())
			return doorbell.Event{}
		}
	}
	// Consume the two grant callbacks before checking input delivery.
	for range 2 {
		if e := nextEvent(); e.Frame == nil || e.Frame.Memory == nil {
			t.Fatal("missing grant callback", e)
		}
	}
	for i, input := range inputs {
		raw := hostInputRecord(input, uint32(i+3))
		if err := unix.Sendmsg(pair[0], raw[:], nil, nil, 0); err != nil {
			t.Fatal(err)
		}
		e := nextEvent()
		if e.Frame == nil || e.Frame.Disposition != doorbell.Decoded || e.Frame.Input == nil || *e.Frame.Input != input {
			t.Fatal("host input not delivered intact", e)
		}
		if input.Action != 0 {
			if id, ok := session.State.ActionID(*e.Frame.Input); !ok || id != "node" || e.Frame.Violation != nil || e.Frame.ImplicitlyClosed != 0 {
				t.Fatal("action FIRE did not resolve cleanly", id, ok, e.Frame)
			}
		}
		if (e.Frame.InputContextError != nil) != (input.Phase == doorbell.Release) {
			t.Fatal("incorrect context usability", i, e.Frame.InputContextError)
		}
		if input.Phase == doorbell.Release {
			report := recv().GetContractViolation()
			if report.GetWhere() != pb.ViolationSite_VIOLATION_SITE_UNUSABLE_CONTEXT || report.GetCount() != 1 {
				t.Fatal(report)
			}
		}
	}
	// The host leaves its socket open and silent. The notice alone must stop
	// the plugin, including on macOS, before its final report releases the host.
	notice := &pb.HostMsg{Msg: &pb.HostMsg_DoorbellShutdown{DoorbellShutdown: &pb.DoorbellShutdown{Reason: pb.DoorbellShutdownReason_DOORBELL_SHUTDOWN_FRAMING_FAULT, FailureClass: pb.DoorbellFailureClass_DOORBELL_FAILURE_BAD_CRC.Enum(), DeadlineMs: 2000, Detail: "host saw bad crc"}}}
	send(notice)
	finalMessage := recv()
	report := finalMessage.GetDoorbellFinalReport()
	for _, want := range []string{"FRAMING_FAULT", "BAD_CRC", "host saw bad crc", "Received:7"} {
		if report == nil || !strings.Contains(report.Detail, want) {
			t.Fatal("missing final report facts; received", finalMessage)
		}
	}
	if _, err := session.Doorbell.Notify(doorbell.Notification{SegmentID: 7<<32 | 1, Mask: doorbell.Mask{1}}); !errors.Is(err, doorbell.ErrClosed) {
		t.Fatal("reported before stopping doorbell", err)
	}
	if e := nextEvent(); e.Cancellation == nil || e.Cancellation.Opening.Interaction != 3 || e.Cancellation.Reason != "local_stop" {
		t.Fatal("open gesture not cancelled", e)
	}
	if e := nextEvent(); e.Terminal == nil {
		t.Fatal("missing ordered terminal", e)
	}
	// A repeated notice produces no second final report. A mapping ack proves
	// the reliable session remains usable after both notices.
	send(notice)
	send(&pb.HostMsg{Msg: &pb.HostMsg_TimeMapping{TimeMapping: &pb.TimeMapping{Gen: generation, Revision: 2, T: []float64{0, 1}}}})
	if ack := recv().GetTimeMappingAck(); ack.GetRevision() != 2 {
		t.Fatal("duplicate report or dead control session", ack)
	}
	send(&pb.HostMsg{Msg: &pb.HostMsg_Shutdown{Shutdown: &pb.Shutdown{DeadlineMs: proto.Uint32(500)}}})
	if recv().GetShutdownReport().GetOutcome() != pb.ShutdownReport_NO_HOOK {
		t.Fatal("missing shutdown report")
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

// hostInputRecord is an independent E encoder for this peer, not the SDK codec.
// Byte offsets and numeric op/version values come from doorbell_E_input §1.
// Values fills the 48-byte position union; record bytes [100..108) stay zero.
func hostInputRecord(e doorbell.Input, id uint32) [112]byte {
	var raw [112]byte
	raw[0], raw[1] = 'E', 2
	binary.LittleEndian.PutUint32(raw[4:], id)
	for i, value := range []uint32{e.Generation, e.Time, e.Interaction, e.Sequence, e.Epoch, e.Target.ID, e.Target.Part, e.Tool} {
		binary.LittleEndian.PutUint32(raw[8+4*i:], value)
	}
	binary.LittleEndian.PutUint16(raw[40:], e.Channels)
	copy(raw[42:50], []byte{byte(e.Phase), e.ViewID, e.Level, e.Modifiers, e.ClickCount, byte(e.Target.Kind), byte(e.Position.Kind), e.Position.Flags})
	binary.LittleEndian.PutUint16(raw[50:52], e.Action)
	for i, value := range e.Position.Values {
		binary.LittleEndian.PutUint32(raw[52+4*i:], math.Float32bits(value))
	}
	binary.LittleEndian.PutUint32(raw[108:], crc32.ChecksumIEEE(raw[:108]))
	return raw
}
