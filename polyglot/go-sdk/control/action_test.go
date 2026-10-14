package control

import (
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func TestActionIDTracksInstalledArrangement(t *testing.T) {
	s := NewState(&lifecycle{}, nil)
	c := stateClient(t)
	binding := &pb.ToolBinding{
		Tool: 7, ToolId: "polygon", Revision: 1, Mode: pb.ToolMode_TOOL_MODE_GESTURE,
		Triggers: []*pb.Trigger{{Key: "P", Hold: pb.TriggerHold_TRIGGER_HOLD_TOGGLE}},
		Actions: []*pb.ToolAction{
			{Action: 1, ActionId: "node", Key: "N"},
			{Action: 2, ActionId: "finish"},     // A pane action needs no assigned key.
			{Action: 65537, ActionId: "future"}, // Never truncate a wire index to u16.
		},
	}
	install := func() {
		t.Helper()
		if err := s.Apply(c, &pb.HostMsg{Msg: &pb.HostMsg_ToolBinding{ToolBinding: binding}}); err != nil {
			t.Fatal(err)
		}
		if ack := (<-c.out).msg.GetToolBindingAck(); ack.GetRevision() != binding.Revision {
			t.Fatal(ack)
		}
	}
	check := func(in doorbell.Input, want string, found bool) {
		t.Helper()
		if got, ok := s.ActionID(in); got != want || ok != found {
			t.Fatalf("ActionID(%+v) = (%q, %v), want (%q, %v)", in, got, ok, want, found)
		}
	}
	in := doorbell.Input{Tool: 7, Phase: doorbell.Fire, Action: 1}
	check(in, "", false)
	install()
	check(in, "node", true)
	in.Action = 2
	check(in, "finish", true)
	for _, action := range []uint16{0, 3, 65535} {
		in.Action = action
		check(in, "", false)
	}
	in.Action = 1
	in.Tool = 8
	check(in, "", false)
	in.Tool = 7
	for _, phase := range []doorbell.Phase{doorbell.Press, doorbell.Drag, doorbell.Release, doorbell.Cancel} {
		in.Phase = phase
		check(in, "", false)
	}
	in.Phase = doorbell.Fire
	binding.Actions[0].ActionId = "updated"
	check(in, "node", true) // Apply owns an independent copy.
	snapshot := s.Tool(7)
	if snapshot.GetTriggers()[0].GetKey() != "P" || len(snapshot.GetActions()) != 3 {
		t.Fatal(snapshot)
	}
	snapshot.Actions[0].ActionId = "mutated"
	check(in, "node", true)
	binding.Revision++
	install()
	check(in, "updated", true)
	binding.Revision++
	binding.Actions = binding.Actions[2:]
	install()
	check(in, "", false)
	binding.Revision++
	binding.Retired = true
	install()
	check(in, "", false)
}
