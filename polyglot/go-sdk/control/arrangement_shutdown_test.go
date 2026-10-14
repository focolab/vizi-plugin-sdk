package control

import (
	"context"
	"errors"
	"io"
	"strings"
	"testing"
	"testing/synctest"
	"time"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/violations"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
)

func TestFirstArrangementInstallsReportsAndReacks(t *testing.T) {
	for _, kind := range []string{"tool", "time"} {
		t.Run(kind, func(t *testing.T) {
			reporter, err := violations.New(violations.Config{})
			if err != nil {
				t.Fatal(err)
			}
			defer reporter.Close()
			state := NewState(&lifecycle{}, nil) // exercise Server's shared reporter fallback
			p := openPeer(t, Config{Reporter: reporter, Arrange: state.Apply}, nil)
			msg := &pb.HostMsg{Msg: &pb.HostMsg_ToolBinding{ToolBinding: &pb.ToolBinding{Tool: 7, Revision: 12, ToolId: "paint", Mode: pb.ToolMode_TOOL_MODE_FIRE}}}
			if kind == "time" {
				msg = &pb.HostMsg{Msg: &pb.HostMsg_TimeMapping{TimeMapping: &pb.TimeMapping{Gen: 7, Revision: 12, T: []float64{0, 1}}}}
			}
			checkAck := func(m *pb.PluginMsg) {
				t.Helper()
				if kind == "tool" {
					if m.GetToolBindingAck().GetRevision() != 12 || state.Tool(7).GetRevision() != 12 {
						t.Fatal("not installed/acknowledged", m)
					}
				} else {
					state.mu.RLock()
					installed := proto.Equal(state.times[timeKey{7, 12}], msg.GetTimeMapping())
					state.mu.RUnlock()
					if m.GetTimeMappingAck().GetRevision() != 12 || !installed {
						t.Fatal("not installed/acknowledged", m)
					}
				}
			}
			p.send(t, msg)
			var reported, acknowledged bool
			for range 2 {
				out := p.recv(t)
				if report := out.GetContractViolation(); report != nil {
					if reported || report.Where != pb.ViolationSite_VIOLATION_SITE_ARRANGEMENT_FIRST_REVISION || report.Count != 1 || !strings.Contains(report.Detail, "12") {
						t.Fatal(report)
					}
					reported = true
				} else {
					if acknowledged {
						t.Fatal("duplicate acknowledgment", out)
					}
					checkAck(out)
					acknowledged = true
				}
			}
			if !reported || !acknowledged {
				t.Fatal("missing report or acknowledgment")
			}
			p.send(t, msg)
			checkAck(p.recv(t))
			if count := reporter.Stats()[violations.ArrangementFirstRevision].Observed; count != 1 {
				t.Fatal("replay re-reported first revision", count)
			}
		})
	}
}

func TestMissingDeadlineReportPrecedesImmediateCompletion(t *testing.T) {
	reporter, err := violations.New(violations.Config{ReportInterval: 50 * time.Millisecond})
	if err != nil {
		t.Fatal(err)
	}
	defer reporter.Close()
	p := openPeer(t, Config{Reporter: reporter}, nil)
	// Consume the report allowance so missing_deadline must wait for the worker.
	if err := reporter.Observe(violations.Finding{Where: violations.ZeroID, Detail: "earlier"}); err != nil {
		t.Fatal(err)
	}
	if p.recv(t).GetContractViolation().GetWhere() != pb.ViolationSite_VIOLATION_SITE_ZERO_ID {
		t.Fatal("earlier report absent")
	}
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_Shutdown{Shutdown: &pb.Shutdown{}}})
	if report := p.recv(t).GetContractViolation(); report.GetWhere() != pb.ViolationSite_VIOLATION_SITE_MISSING_DEADLINE || report.GetCount() != 1 {
		t.Fatal("shutdown lost pending report", report)
	}
	if report := p.recv(t).GetShutdownReport(); report.GetOutcome() != pb.ShutdownReport_NO_HOOK {
		t.Fatal(report)
	}
	if _, err := p.stream.Recv(); !errors.Is(err, io.EOF) {
		t.Fatal(err)
	}
}

type recordingStream struct {
	*stalledStream
	out chan *pb.PluginMsg
}

func (s *recordingStream) Send(m *pb.PluginMsg) error {
	s.out <- proto.Clone(m).(*pb.PluginMsg)
	return nil
}

func TestShutdownAbsentVersusZeroDeadline(t *testing.T) {
	for _, present := range []bool{false, true} {
		t.Run(map[bool]string{false: "absent", true: "zero"}[present], func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				ctx, cancel := context.WithCancel(context.Background())
				defer cancel()
				reporter, err := violations.New(violations.Config{})
				if err != nil {
					t.Fatal(err)
				}
				defer reporter.Close()
				stream := &recordingStream{stalledStream: &stalledStream{ctx: ctx, in: make(chan *pb.HostMsg, 2)}, out: make(chan *pb.PluginMsg, 8)}
				server, err := NewServer(Config{BuildIdentity: "deadlines test build", PluginID: "deadlines", Reporter: reporter, Handshake: func(*pb.Hello) error { return nil }, Diagnostic: func(error) {}, Shutdown: func(ctx context.Context, _ *Client, _ *pb.Shutdown) error { <-ctx.Done(); return ctx.Err() }})
				if err != nil {
					t.Fatal(err)
				}
				done := make(chan error, 1)
				go func() { done <- server.Session(stream) }()
				stream.in <- &pb.HostMsg{Id: "Hello from test host 1.0", Msg: &pb.HostMsg_Hello{Hello: &pb.Hello{ProtocolMajor: 1}}}
				if (<-stream.out).GetReady() == nil {
					t.Fatal("missing Ready")
				}
				sh := &pb.Shutdown{}
				if present {
					sh.DeadlineMs = proto.Uint32(0)
				}
				// Round-trip through protobuf so scalar presence is part of the test.
				data, err := proto.Marshal(sh)
				if err != nil {
					t.Fatal(err)
				}
				decoded := &pb.Shutdown{}
				if err := proto.Unmarshal(data, decoded); err != nil {
					t.Fatal(err)
				}
				start := time.Now()
				stream.in <- &pb.HostMsg{Msg: &pb.HostMsg_Shutdown{Shutdown: decoded}}
				if err := <-done; err != nil {
					t.Fatal(err)
				}
				elapsed := time.Since(start)
				want := 5 * time.Second
				if present {
					want = 0
				}
				if elapsed != want {
					t.Fatal("wrong grace period", elapsed, want)
				}
				count := reporter.Stats()[violations.MissingDeadline].Observed
				if (present && count != 0) || (!present && count != 1) {
					t.Fatal("presence confused with zero", count)
				}
			})
		})
	}
}

func TestRoiPoseFieldBoundsAndWholeMessageCeiling(t *testing.T) {
	for _, name := range []protoreflect.Name{"pose_translation", "pose_rotation", "pose_stretch"} {
		t.Run(string(name), func(t *testing.T) {
			roi := &pb.Roi{}
			field := roi.ProtoReflect().Descriptor().Fields().ByName(name)
			values := roi.ProtoReflect().Mutable(field).List()
			for range 1048576 {
				values.Append(protoreflect.ValueOfFloat64(0))
			}
			reply := &pb.HostMsg{Msg: &pb.HostMsg_Rois{Rois: &pb.RoiList{Rois: []*pb.RoiRecord{{Roi: roi}}}}}
			if err := ValidateSize(reply); err != nil {
				t.Fatal("valid long pose refused", err)
			}
			values.Append(protoreflect.ValueOfFloat64(0))
			var overflow *RepeatedFieldError
			if err := ValidateSize(reply); !errors.As(err, &overflow) || overflow.Limit != 1048576 {
				t.Fatal("pose field bound not enforced", err)
			}
		})
	}
	// One ~24 MiB reply is admitted; two such records exceed the
	// whole-message ceiling even though each repeated field fits its bound.
	roi := &pb.Roi{PoseStretch: make([]float64, 1048576), PoseTranslation: make([]float64, 1048576), PoseRotation: make([]float64, 1048576)}
	if err := ValidateSize(&pb.HostMsg{Msg: &pb.HostMsg_Rois{Rois: &pb.RoiList{Rois: []*pb.RoiRecord{{Roi: roi}}}}}); err != nil {
		t.Fatal("valid large ROI reply refused", err)
	}
	if err := ValidateSize(&pb.HostMsg{Msg: &pb.HostMsg_Rois{Rois: &pb.RoiList{Rois: []*pb.RoiRecord{{Roi: roi}, {Roi: roi}}}}}); err == nil || !strings.Contains(err.Error(), "32 MiB") {
		t.Fatal("message cap bypassed", err)
	}
	if err := ValidateSize(&pb.Geometry{Channels: make([]*pb.ChannelInfo, 65537)}); err == nil {
		t.Fatal("general bound raised accidentally")
	}
}

func TestRoiPoseExtensionBounds(t *testing.T) {
	for _, name := range []protoreflect.Name{"pose_translation", "pose_rotation", "pose_stretch"} {
		t.Run(string(name), func(t *testing.T) {
			extension := &pb.RoiPoseExtension{}
			f := extension.ProtoReflect().Descriptor().Fields().ByName(name)
			values := extension.ProtoReflect().Mutable(f).List()
			for range 1048576 {
				values.Append(protoreflect.ValueOfFloat64(0))
			}
			request := &pb.PluginMsg{Msg: &pb.PluginMsg_ExtendRoiPoses{ExtendRoiPoses: &pb.ExtendRoiPoses{Extensions: []*pb.RoiPoseExtension{extension}}}}
			if err := ValidateSize(request); err != nil {
				t.Fatal("legal extension rejected", err)
			}
			values.Append(protoreflect.ValueOfFloat64(0))
			var overflow *RepeatedFieldError
			if err := ValidateSize(request); !errors.As(err, &overflow) || overflow.Limit != 1048576 {
				t.Fatal("extension bound lost", err)
			}
		})
	}
}
