package control

import (
	"testing"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

// withUnknown appends a varint field this build does not know to m.
func withUnknown[M proto.Message](m M) M {
	c := proto.Clone(m).(M)
	r := c.ProtoReflect()
	r.SetUnknown(append(r.GetUnknown(), protowire.AppendVarint(protowire.AppendTag(nil, 9999, protowire.VarintType), 1)...))
	return c
}

func TestKnownOnlyStripsEveryDepth(t *testing.T) {
	a := &pb.PlanStage{StageId: "s", Input: withUnknown(&pb.StageShape{Dtype: pb.DType_DTYPE_U8, X: 1}), Params: map[string]*pb.Value{"k": withUnknown(&pb.Value{Kind: &pb.Value_I{I: 3}})}}
	a = withUnknown(a)
	b := &pb.PlanStage{StageId: "s", Input: &pb.StageShape{Dtype: pb.DType_DTYPE_U8, X: 1}, Params: map[string]*pb.Value{"k": {Kind: &pb.Value_I{I: 3}}}}
	if proto.Equal(a, b) {
		t.Fatal("the fixture carries no unknown fields")
	}
	if !sameKnown(a, b) || stagePlanKey(a) != stagePlanKey(b) {
		t.Fatal("unknown fields took part in identity")
	}
	if len(a.ProtoReflect().GetUnknown()) == 0 || len(a.Input.ProtoReflect().GetUnknown()) == 0 {
		t.Fatal("the comparison mutated its operand")
	}
	b.Input.X = 2
	if sameKnown(a, b) || stagePlanKey(a) == stagePlanKey(b) {
		t.Fatal("a known difference was ignored")
	}
	discardUnknown((*pb.StageShape)(nil).ProtoReflect()) // an invalid message is left alone
}

// §5.5 rule 4: a restatement identical but for a field this build does not
// know is the same content, acknowledged again and never a mismatch.
func TestRestatementComparesKnownFieldsOnly(t *testing.T) {
	l := &lifecycle{}
	s := NewState(l, nil)
	c := stateClient(t)
	apply := func(m *pb.HostMsg) *pb.PluginMsg {
		t.Helper()
		if err := s.Apply(c, m); err != nil {
			t.Fatal(err)
		}
		select {
		case o := <-c.out:
			return o.msg
		default:
			t.Fatal("not acknowledged")
			return nil
		}
	}
	tool := &pb.ToolBinding{Tool: 3, Revision: 1, ToolId: "paint", Mode: pb.ToolMode_TOOL_MODE_GESTURE, Triggers: []*pb.Trigger{{Key: "P"}}}
	apply(&pb.HostMsg{Msg: &pb.HostMsg_ToolBinding{ToolBinding: tool}})
	restated := withUnknown(tool)
	restated.Triggers[0] = withUnknown(restated.Triggers[0])
	if ack := apply(&pb.HostMsg{Msg: &pb.HostMsg_ToolBinding{ToolBinding: restated}}).GetToolBindingAck(); ack.GetRevision() != 1 {
		t.Fatal(ack)
	}

	tm := &pb.TimeMapping{Gen: 7, Revision: 1, T: []float64{0, 1}}
	apply(&pb.HostMsg{Msg: &pb.HostMsg_TimeMapping{TimeMapping: tm}})
	if ack := apply(&pb.HostMsg{Msg: &pb.HostMsg_TimeMapping{TimeMapping: withUnknown(tm)}}).GetTimeMappingAck(); ack.GetRevision() != 1 {
		t.Fatal(ack)
	}

	ds := &pb.DatasetState{Gen: 7, Geometry: &pb.Geometry{Gen: 7}, TimeMappingRevision: 1}
	pane := &pb.PaneEpoch{ViewId: 0, Epoch: 9, Gen: 7}
	apply(&pb.HostMsg{Msg: &pb.HostMsg_EpochChange{EpochChange: &pb.EpochChange{Datasets: []*pb.DatasetState{ds}, Panes: []*pb.PaneEpoch{pane}}}})
	again := &pb.EpochChange{Datasets: []*pb.DatasetState{withUnknown(ds)}, Panes: []*pb.PaneEpoch{withUnknown(pane)}}
	again.Datasets[0].Geometry = withUnknown(again.Datasets[0].Geometry)
	ack := apply(&pb.HostMsg{Msg: &pb.HostMsg_EpochChange{EpochChange: again}}).GetEpochAck()
	if len(ack.GetPanes()) != 1 || ack.Panes[0].Epoch != 9 || len(ack.Panes[0].ProtoReflect().GetUnknown()) != 0 {
		t.Fatal("restated epoch not acknowledged with known fields alone", ack)
	}

	// A known difference is still a mismatch.
	changed := proto.Clone(tool).(*pb.ToolBinding)
	changed.Triggers[0].Key = "Q"
	if err := s.Apply(c, &pb.HostMsg{Msg: &pb.HostMsg_ToolBinding{ToolBinding: changed}}); err == nil {
		t.Fatal("changed content accepted")
	}
}
