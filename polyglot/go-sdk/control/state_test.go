package control

import (
	"context"
	"errors"
	"testing"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/violations"
	"google.golang.org/protobuf/proto"
)

type lifecycle struct {
	fail  bool
	epoch uint32
	tool  uint32
}

func (l *lifecycle) ToolActivated(tool uint32) error {
	if l.fail {
		return errors.New("refused")
	}
	l.tool = tool
	return nil
}
func (l *lifecycle) ToolRetired(tool uint32) error {
	if l.fail {
		return errors.New("refused")
	}
	l.tool = 0
	return nil
}
func (l *lifecycle) EpochAcknowledged(_ uint8, epoch uint32) error {
	if l.fail {
		return errors.New("refused")
	}
	l.epoch = epoch
	return nil
}

func stateClient(t *testing.T) *Client {
	ctx, cancel := context.WithCancelCause(context.Background())
	t.Cleanup(func() { cancel(ErrClosed) })
	return &Client{ctx: ctx, cancel: cancel, out: make(chan outbound, 32)}
}

func TestStateInstallsBeforeAckAndReacks(t *testing.T) {
	l := &lifecycle{}
	s := NewState(l, nil)
	c := stateClient(t)
	tool := &pb.HostMsg{Msg: &pb.HostMsg_ToolBinding{ToolBinding: &pb.ToolBinding{Tool: 3, Revision: 1, ToolId: "paint", Mode: pb.ToolMode_TOOL_MODE_GESTURE}}}
	l.fail = true
	if err := s.Apply(c, tool); err == nil {
		t.Fatal("failed lifecycle accepted")
	}
	select {
	case <-c.out:
		t.Fatal("failed activation acknowledged")
	default:
	}
	l.fail = false
	for i := 0; i < 2; i++ {
		if err := s.Apply(c, tool); err != nil {
			t.Fatal(err)
		}
		if (<-c.out).msg.GetToolBindingAck().GetTool() != 3 || l.tool != 3 {
			t.Fatal("ack before activation")
		}
	}
	tool.GetToolBinding().ToolId = "mutated"
	if s.Tool(3).ToolId != "paint" {
		t.Fatal("caller mutated state")
	}
	if err := s.Apply(c, &pb.HostMsg{Msg: &pb.HostMsg_ToolBinding{ToolBinding: &pb.ToolBinding{Tool: 3, Revision: 2, Retired: true}}}); err != nil {
		t.Fatal(err)
	}
	if (<-c.out).msg.GetToolBindingAck().Tool != 3 || s.Tool(3) != nil {
		t.Fatal("retirement without redundant tool fields failed")
	}
	epoch := &pb.HostMsg{Msg: &pb.HostMsg_EpochChange{EpochChange: &pb.EpochChange{Datasets: []*pb.DatasetState{{Gen: 7, Geometry: &pb.Geometry{Gen: 7}, TimeMappingRevision: 1}}, Panes: []*pb.PaneEpoch{{ViewId: 0, Epoch: 9, Gen: 7}}}}}
	if err := s.Apply(c, epoch); err == nil {
		t.Fatal("missing mapping acknowledged")
	}
	select {
	case <-c.out:
		t.Fatal("missing mapping ack")
	default:
	}
	for _, rev := range []uint32{1, 2} {
		if err := s.Apply(c, &pb.HostMsg{Msg: &pb.HostMsg_TimeMapping{TimeMapping: &pb.TimeMapping{Gen: 7, Revision: rev, T: []float64{0, 1, 3}}}}); err != nil {
			t.Fatal(err)
		}
		<-c.out
	}
	// Revision 1 remains an acknowledged arrangement after revision 2 arrives.
	for i := 0; i < 2; i++ {
		if err := s.Apply(c, epoch); err != nil {
			t.Fatal(err)
		}
		if (<-c.out).msg.GetEpochAck().Panes[0].Epoch != 9 || l.epoch != 9 {
			t.Fatal("ack before install")
		}
	}
	p := s.Pane(0)
	if p.Time.Revision != 1 {
		t.Fatal("wrong mapping")
	}
	p.Pane.Epoch = 100
	if s.Pane(0).Pane.Epoch != 9 {
		t.Fatal("snapshot aliases state")
	}
	epoch.GetEpochChange().Datasets[0].Geometry.SpacingX = 3
	if err := s.Apply(c, epoch); err == nil {
		t.Fatal("conflicting replay accepted")
	}
	select {
	case <-c.out:
		t.Fatal("conflict ack")
	default:
	}
}

// Geometry is received state, not an additional acknowledgement gate. A
// channel-name table likewise does not establish the set of available channels.
func TestEpochAcceptsGeometryAndSparseChannelNames(t *testing.T) {
	for _, tc := range []struct {
		name     string
		geometry *pb.Geometry
		high     []uint32
	}{
		{"absent_geometry", nil, nil},
		{"different_geometry_gen", &pb.Geometry{Gen: 99}, nil},
		{"sparse_channel_names", &pb.Geometry{Gen: 7, Channels: []*pb.ChannelInfo{{Index: 0, Name: "red"}, {Index: 1, Name: "green"}, {Index: 2, Name: "blue"}}}, []uint32{16}},
		{"unnamed_without_geometry", nil, []uint32{15, 16, 19}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			state := NewState(&lifecycle{}, nil)
			c := stateClient(t)
			msg := &pb.HostMsg{Msg: &pb.HostMsg_EpochChange{EpochChange: &pb.EpochChange{
				Datasets: []*pb.DatasetState{{Gen: 7, Geometry: tc.geometry}, {Gen: 8, Geometry: &pb.Geometry{Gen: 8}}},
				Panes:    []*pb.PaneEpoch{{ViewId: 0, Epoch: 9, Gen: 7, ChannelsHigh: tc.high}, {ViewId: 1, Epoch: 3, Gen: 8}},
			}}}
			for range 2 { // exact replay must also acknowledge both panes
				if err := state.Apply(c, msg); err != nil {
					t.Fatal("received state refused", err)
				}
				select {
				case out := <-c.out:
					want := &pb.EpochAck{Panes: []*pb.PaneEpoch{{ViewId: 0, Epoch: 9}, {ViewId: 1, Epoch: 3}}}
					if !proto.Equal(out.msg.GetEpochAck(), want) {
						t.Fatal("both panes must be acknowledged", out.msg)
					}
				default:
					t.Fatal("missing epoch acknowledgement")
				}
				for i, pane := range msg.GetEpochChange().Panes {
					got := state.Pane(uint8(pane.ViewId))
					if got == nil || !proto.Equal(got.Pane, pane) || !proto.Equal(got.Dataset, msg.GetEpochChange().Datasets[i]) {
						t.Fatal("received snapshot changed", got)
					}
				}
			}
		})
	}
}

func TestEpochMissingMappingRefusesOnlyDependentPane(t *testing.T) {
	reporter, err := violations.New(violations.Config{})
	if err != nil {
		t.Fatal(err)
	}
	defer reporter.Close()
	state := NewState(&lifecycle{}, reporter)
	c := stateClient(t)
	msg := &pb.HostMsg{Msg: &pb.HostMsg_EpochChange{EpochChange: &pb.EpochChange{
		Datasets: []*pb.DatasetState{{Gen: 7, TimeMappingRevision: 1}, {Gen: 8}},
		Panes:    []*pb.PaneEpoch{{ViewId: 0, Epoch: 9, Gen: 7}, {ViewId: 1, Epoch: 3, Gen: 8}},
	}}}
	if err := state.Apply(c, msg); err == nil {
		t.Fatal("missing mapping accepted")
	}
	select {
	case out := <-c.out:
		want := &pb.EpochAck{Panes: []*pb.PaneEpoch{{ViewId: 1, Epoch: 3}}}
		if !proto.Equal(out.msg.GetEpochAck(), want) {
			t.Fatal("wrong partial acknowledgement", out.msg)
		}
	default:
		t.Fatal("independent pane was not acknowledged")
	}
	if state.Pane(0) != nil || state.Pane(1) == nil || reporter.Stats()[violations.MissingArrangement].Observed != 1 {
		t.Fatal("missing mapping must refuse/report only its dependent pane")
	}
}

func TestZeroEpochRefusesOnlyItsPane(t *testing.T) {
	reporter, err := violations.New(violations.Config{})
	if err != nil {
		t.Fatal(err)
	}
	defer reporter.Close()
	l := &lifecycle{}
	state := NewState(l, reporter)
	c := stateClient(t)
	msg := &pb.HostMsg{Msg: &pb.HostMsg_EpochChange{EpochChange: &pb.EpochChange{
		Datasets: []*pb.DatasetState{{Gen: 7, Geometry: &pb.Geometry{Gen: 7}}},
		Panes:    []*pb.PaneEpoch{{ViewId: 0, Epoch: 0, Gen: 7}, {ViewId: 1, Epoch: 5, Gen: 7}},
	}}}
	for attempt := uint64(1); attempt <= 2; attempt++ {
		if err := state.Apply(c, msg); err == nil {
			t.Fatal("zero epoch was not diagnosed")
		}
		select {
		case out := <-c.out:
			want := &pb.EpochAck{Panes: []*pb.PaneEpoch{{ViewId: 1, Epoch: 5}}}
			if !proto.Equal(out.msg.GetEpochAck(), want) {
				t.Fatal("wrong partial acknowledgement", out.msg)
			}
		default:
			t.Fatal("valid pane was not acknowledged")
		}
		if state.Pane(0) != nil || state.Pane(1) == nil || l.epoch != 5 {
			t.Fatal("only the valid pane should be installed")
		}
		if got := reporter.Stats()[violations.ZeroID].Observed; got != attempt {
			t.Fatal("zero epoch must be reported once per receipt", got)
		}
	}
}

func TestLimitsUsePublishedExceptions(t *testing.T) {
	for _, n := range []int{65536, 1048576} {
		if err := ValidateSize(&pb.TimeMapping{T: make([]float64, n)}); err != nil {
			t.Fatal(err)
		}
	}
	if err := ValidateSize(&pb.TimeMapping{T: make([]float64, 1048577)}); err == nil {
		t.Fatal("oversized time mapping")
	}
	if err := ValidateSize(&pb.Geometry{Levels: make([]*pb.LevelScale, 65)}); err == nil {
		t.Fatal("oversized level table")
	}
	if err := ValidateSize(&pb.ExportChunk{Source: &pb.ExportChunk_Inline{Inline: make([]byte, MaxInlineBytes+1)}}); err == nil {
		t.Fatal("oversized inline")
	}
}

func TestReliableQueueOverflowFaultsAndPreservesAcceptedMessage(t *testing.T) {
	c := stateClient(t)
	c.out = make(chan outbound, 1)
	m := &pb.PluginMsg{Msg: &pb.PluginMsg_OverlayRemove{OverlayRemove: &pb.OverlayRemove{OverlayId: "a"}}}
	if err := c.Send(m); err != nil {
		t.Fatal(err)
	}
	m.GetOverlayRemove().OverlayId = "b"
	if err := c.Send(m); !errors.Is(err, ErrQueueFull) {
		t.Fatal(err)
	}
	if context.Cause(c.ctx) != ErrQueueFull {
		t.Fatal("overflow silently dropped")
	}
	if (<-c.out).msg.GetOverlayRemove().OverlayId != "a" {
		t.Fatal("accepted message mutated")
	}
}
