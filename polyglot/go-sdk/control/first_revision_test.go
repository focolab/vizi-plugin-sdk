package control

import (
	"strings"
	"testing"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/violations"
)

// A refused statement is still received: after a ToolBinding with an unknown
// mode at revision 1 is refused, the host's revision 2 with a known mode is
// installed, activated and acknowledged, and is not the identity's first, so
// no arrangement_first_revision is reported. A genuine first statement above
// 1 is still reported (TestFirstArrangementInstallsReportsAndReacks).
func TestRefusedFirstToolBindingCountsAsReceived(t *testing.T) {
	reporter, err := violations.New(violations.Config{})
	if err != nil {
		t.Fatal(err)
	}
	defer reporter.Close()
	life := &lifecycle{}
	s := NewState(life, reporter)
	diagnostics := make(chan error, 4)
	p := openPeer(t, Config{Reporter: reporter, Diagnostic: func(err error) { diagnostics <- err }, Arrange: s.Apply}, nil)
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_ToolBinding{ToolBinding: &pb.ToolBinding{Tool: 7, Revision: 1, ToolId: "paint", Mode: pb.ToolMode(99)}}})
	select {
	case err := <-diagnostics:
		if !strings.Contains(err.Error(), "mode 99 is not a ToolMode this build knows") {
			t.Fatal(err)
		}
	case <-p.ctx.Done():
		t.Fatal("the refusal was silent")
	}
	if s.Tool(7) != nil || life.tool != 0 {
		t.Fatal("refused binding installed or activated")
	}
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_ToolBinding{ToolBinding: &pb.ToolBinding{Tool: 7, Revision: 2, ToolId: "paint", Mode: pb.ToolMode_TOOL_MODE_FIRE}}})
	out := p.recv(t)
	if ack := out.GetToolBindingAck(); ack.GetTool() != 7 || ack.GetRevision() != 2 {
		t.Fatal("revision 2 not acknowledged, or a report preceded its acknowledgement", out)
	}
	if s.Tool(7).GetRevision() != 2 || life.tool != 7 {
		t.Fatal("revision 2 not installed and activated")
	}
	if count := reporter.Stats()[violations.ArrangementFirstRevision].Observed; count != 0 {
		t.Fatal("refused revision 1 not counted as received", count)
	}
}
