package control

import (
	"context"
	"errors"
	"io"
	"strings"
	"testing"
	"time"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/violations"
	"google.golang.org/protobuf/proto"
)

func TestStableMajorCompatibility(t *testing.T) {
	for _, version := range [][2]uint32{{1, 0}, {1, 27}, {0, 13}, {0, 12}, {2, 0}} {
		p := openPeer(t, Config{}, &pb.Hello{ProtocolMajor: version[0], ProtocolMinor: version[1]})
		if version[0] != 1 {
			if _, err := p.stream.Recv(); err == nil || !strings.Contains(err.Error(), "protocol mismatch") {
				t.Fatal(version, err)
			}
		} else if p.client == nil {
			t.Fatal("compatible minor refused", version)
		}
	}
}

func TestShutdownOutcomesBeforeEOF(t *testing.T) {
	for _, tc := range []struct {
		name string
		hook func(context.Context, *Client, *pb.Shutdown) error
		want pb.ShutdownReport_Outcome
	}{
		{"none", nil, pb.ShutdownReport_NO_HOOK},
		{"completed", func(context.Context, *Client, *pb.Shutdown) error { return nil }, pb.ShutdownReport_COMPLETED},
		{"error", func(context.Context, *Client, *pb.Shutdown) error { return errors.New("cleanup failed") }, pb.ShutdownReport_RAISED},
		{"panic", func(context.Context, *Client, *pb.Shutdown) error { panic("cleanup panic") }, pb.ShutdownReport_RAISED},
		{"overrun", func(ctx context.Context, _ *Client, _ *pb.Shutdown) error { <-ctx.Done(); return ctx.Err() }, pb.ShutdownReport_OVERRAN},
	} {
		t.Run(tc.name, func(t *testing.T) {
			p := openPeer(t, Config{Shutdown: tc.hook}, nil)
			p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_Shutdown{Shutdown: &pb.Shutdown{DeadlineMs: proto.Uint32(30)}}})
			report := p.recv(t).GetShutdownReport()
			if report == nil || report.Outcome != tc.want {
				t.Fatal(report)
			}
			if tc.want == pb.ShutdownReport_RAISED && report.Detail == "" {
				t.Fatal("lost failure detail")
			}
			if _, err := p.stream.Recv(); !errors.Is(err, io.EOF) {
				t.Fatal(err)
			}
		})
	}
}

func TestCompletedHookWithOverrunningRequestStillReportsCompleted(t *testing.T) {
	started := make(chan struct{})
	p := openPeer(t, Config{Shutdown: func(context.Context, *Client, *pb.Shutdown) error { return nil }, HandleRequest: func(ctx context.Context, _ *Client, _ *pb.HostMsg) (*pb.PluginMsg, error) {
		close(started)
		<-ctx.Done()
		return nil, ctx.Err()
	}}, nil)
	p.send(t, &pb.HostMsg{Id: "work", Msg: &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{Uri: "x"}}})
	<-started
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_Shutdown{Shutdown: &pb.Shutdown{DeadlineMs: proto.Uint32(30)}}})
	if got := p.recv(t).GetShutdownReport().GetOutcome(); got != pb.ShutdownReport_COMPLETED {
		t.Fatal(got)
	}
}

func TestToolRevisionReplayUpdateRetirementAndConflict(t *testing.T) {
	reporter, err := violations.New(violations.Config{})
	if err != nil {
		t.Fatal(err)
	}
	defer reporter.Close()
	s := NewState(&lifecycle{}, reporter)
	c := stateClient(t)
	apply := func(rev uint32, id string, retired bool) error {
		return s.Apply(c, &pb.HostMsg{Msg: &pb.HostMsg_ToolBinding{ToolBinding: &pb.ToolBinding{Tool: 7, Revision: rev, ToolId: id, Mode: pb.ToolMode_TOOL_MODE_FIRE, Retired: retired}}})
	}
	for _, rev := range []uint32{1, 1, 2} {
		id := "first"
		if rev == 2 {
			id = "updated"
		}
		if err := apply(rev, id, false); err != nil {
			t.Fatal(err)
		}
		if ack := (<-c.out).msg.GetToolBindingAck(); ack.Revision != rev {
			t.Fatal(ack)
		}
	}
	if s.Tool(7).ToolId != "updated" {
		t.Fatal(s.Tool(7))
	}
	if err := apply(2, "changed", false); err == nil {
		t.Fatal("same revision changed content")
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	report, err := reporter.Next(ctx)
	if err != nil || report.Where != violations.ArrangementContentMismatch {
		t.Fatal(report, err)
	}
	if err := apply(1, "first", false); err == nil {
		t.Fatal("stale revision installed")
	}
	if err := apply(0, "x", false); err == nil {
		t.Fatal("zero revision installed")
	}
	for i := 0; i < 2; i++ {
		if err := apply(3, "", true); err != nil {
			t.Fatal(err)
		}
		if ack := (<-c.out).msg.GetToolBindingAck(); ack.Revision != 3 {
			t.Fatal(ack)
		}
	}
	if s.Tool(7) != nil {
		t.Fatal("retired tool still active")
	}
}

func TestShutdownReportReservedSlot(t *testing.T) {
	c := stateClient(t)
	c.out = make(chan outbound, 1)
	if err := c.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_OverlayRemove{OverlayRemove: &pb.OverlayRemove{OverlayId: "last"}}}); err != nil {
		t.Fatal(err)
	}
	sent := c.sealWithReport(&pb.ShutdownReport{Outcome: pb.ShutdownReport_NO_HOOK})
	if nextBatch(t, c)[0].GetOverlayRemove() == nil {
		t.Fatal("lost final push")
	}
	if nextBatch(t, c)[0].GetShutdownReport() == nil {
		t.Fatal("lost report at capacity")
	}
	x, ok := c.nextOutbound()
	if !ok || x.sent != sent {
		t.Fatal("barrier did not follow report")
	}
}

// Exercise the actual dispatcher diagnostic path as well as the same
// ObserveError adapter used by ServeEnv. Regression has its own site;
// future enum values MUST remain local under §11.
func TestToolRejectionsReachDiagnosticsWithoutInventingWireSites(t *testing.T) {
	for _, tc := range []struct {
		name      string
		installed uint32
		binding   *pb.ToolBinding
		detail    string
	}{
		{"stale", 2, &pb.ToolBinding{Tool: 7, Revision: 1, ToolId: "paint", Mode: pb.ToolMode_TOOL_MODE_FIRE}, "stale ToolBinding revision"},
		{"missing_id", 0, &pb.ToolBinding{Tool: 7, Revision: 1, Mode: pb.ToolMode_TOOL_MODE_FIRE}, "requires tool_id and a known mode"},
		{"missing_mode", 0, &pb.ToolBinding{Tool: 7, Revision: 1, ToolId: "paint"}, "requires tool_id and a known mode"},
		{"future_mode", 0, &pb.ToolBinding{Tool: 7, Revision: 1, ToolId: "paint", Mode: pb.ToolMode(99)}, "mode 99 is not a ToolMode this build knows"},
		{"future_mode_update", 1, &pb.ToolBinding{Tool: 7, Revision: 2, ToolId: "paint", Mode: pb.ToolMode(1000)}, "mode 1000 is not a ToolMode this build knows"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			reporter, err := violations.New(violations.Config{})
			if err != nil {
				t.Fatal(err)
			}
			defer reporter.Close()
			s := NewState(&lifecycle{}, reporter)
			diagnostics := make(chan error, 1)
			p := openPeer(t, Config{Diagnostic: func(err error) { diagnostics <- err }, Arrange: func(c *Client, m *pb.HostMsg) error {
				err := s.Apply(c, m)
				if observeErr := reporter.ObserveError(err); observeErr != nil {
					return observeErr
				}
				return err
			}}, nil)
			for rev := uint32(1); rev <= tc.installed; rev++ {
				p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_ToolBinding{ToolBinding: &pb.ToolBinding{Tool: 7, Revision: rev, ToolId: "paint", Mode: pb.ToolMode_TOOL_MODE_FIRE}}})
				if ack := p.recv(t).GetToolBindingAck(); ack.GetRevision() != rev {
					t.Fatal(ack)
				}
			}
			before := s.Tool(7)
			p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_ToolBinding{ToolBinding: tc.binding}})
			select {
			case err := <-diagnostics:
				if !strings.Contains(err.Error(), tc.detail) || !strings.Contains(err.Error(), "tool=7 revision=") || !strings.Contains(err.Error(), "installed_revision=") {
					t.Fatal("unactionable rejection", err)
				}
			case <-p.ctx.Done():
				t.Fatal("rejection was silent")
			}
			if !proto.Equal(before, s.Tool(7)) {
				t.Fatal("rejected statement changed state")
			}
			for site, counts := range reporter.Stats() {
				if counts.Observed != 0 && !(tc.name == "stale" && site == violations.ArrangementRegressed && counts.Observed == 1) {
					t.Fatal("unexpected violation site", site, counts)
				}
			}
			// Stream ordering makes the next ack a barrier: no invalid statement was
			// acknowledged, and a rejection did not close the session.
			p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_ToolBinding{ToolBinding: &pb.ToolBinding{Tool: 8, Revision: 1, ToolId: "next", Mode: pb.ToolMode_TOOL_MODE_FIRE}}})
			if ack := p.recv(t).GetToolBindingAck(); ack.GetTool() != 8 || ack.GetRevision() != 1 {
				t.Fatal("invalid binding acknowledged", ack)
			}
		})
	}
}

func TestToolContentConflictReachesWireExactlyOnce(t *testing.T) {
	reporter, err := violations.New(violations.Config{})
	if err != nil {
		t.Fatal(err)
	}
	defer reporter.Close()
	s := NewState(&lifecycle{}, reporter)
	p := openPeer(t, Config{Arrange: func(c *Client, m *pb.HostMsg) error {
		err := s.Apply(c, m)
		if observeErr := reporter.ObserveError(err); observeErr != nil {
			return observeErr
		}
		return err
	}, Connected: func(c *Client) {
		if err := ReportViolations(c.Context(), c, reporter); err != nil && c.Context().Err() == nil {
			c.Fail(err)
		}
	}}, nil)
	binding := &pb.ToolBinding{Tool: 7, Revision: 1, ToolId: "paint", Mode: pb.ToolMode_TOOL_MODE_FIRE}
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_ToolBinding{ToolBinding: binding}})
	if p.recv(t).GetToolBindingAck().GetTool() != 7 {
		t.Fatal("missing initial ack")
	}
	binding = proto.Clone(binding).(*pb.ToolBinding)
	binding.CancelOnCameraMove = true
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_ToolBinding{ToolBinding: binding}})
	report := p.recv(t).GetContractViolation()
	if report == nil || report.Where != pb.ViolationSite_VIOLATION_SITE_ARRANGEMENT_CONTENT_MISMATCH || report.Count != 1 {
		t.Fatal(report)
	}
	if stats := reporter.Stats()[violations.ArrangementContentMismatch]; stats.Observed != 1 || stats.Pending != 0 {
		t.Fatal("adapter double-reported conflict", stats)
	}
}
