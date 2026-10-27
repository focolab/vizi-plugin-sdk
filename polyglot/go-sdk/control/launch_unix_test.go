//go:build linux || darwin

package control

import (
	"context"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"golang.org/x/sys/unix"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/proto"
)

func TestServeEnvRealEndpointsFaultAndGracefulCompletion(t *testing.T) {
	for _, tc := range []struct {
		name   string
		minor  uint32
		ending string
	}{
		{"version_fault", 12, ""},
		{"graceful", 0, ""},
		{"stream_eof", 0, "eof"},
		{"host_disconnected", 0, "disconnect"},
		{"peer_closed", 0, "peer_closed"},
		{"future_reason", 0, "future_reason"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			minor := tc.minor
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
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
			dir, err := os.MkdirTemp("", "vizi-ctl-")
			if err != nil {
				t.Fatal(err)
			}
			defer os.RemoveAll(dir)
			path := filepath.Join(dir, "plugin.sock")
			t.Setenv("VIZI_PLUGIN_DOORBELL_FD", strconv.Itoa(pair[1]))
			t.Setenv("VIZI_PLUGIN_UDS", path)
			result := make(chan error, 1)
			go func() {
				result <- ServeEnv(ctx, Plugin{Memory: memory.Config{Purposes: []string{"test"}}, Control: Config{BuildIdentity: "test.launch test build", PluginID: "test.launch", Diagnostic: func(error) {}}})
			}()
			conn, err := grpc.NewClient("passthrough:///plugin", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
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
			major := uint32(1)
			if minor == 12 {
				major = 0
			}
			helloID := "Hello from test host 1.0"
			if major == 0 {
				helloID = "hello" // a 0.x host's Hello id; the version check wins
			}
			if err := stream.Send(&pb.HostMsg{Id: helloID, Msg: &pb.HostMsg_Hello{Hello: &pb.Hello{ProtocolMajor: major, ProtocolMinor: minor}}}); err != nil {
				t.Fatal(err)
			}
			ready, err := stream.Recv()
			if minor == 12 {
				if err == nil || !strings.Contains(err.Error(), "host 0.12, Go SDK 1.0") {
					t.Fatalf("%v", err)
				}
				select {
				case err := <-result:
					if err == nil || !strings.Contains(err.Error(), "0.12") {
						t.Fatal(err)
					}
				case <-ctx.Done():
					t.Fatal(ctx.Err())
				}
				return
			}
			if err != nil || ready.GetReady().GetPluginId() != "test.launch" {
				t.Fatalf("%v %v", ready, err)
			}
			if tc.ending == "peer_closed" || tc.ending == "future_reason" {
				reason := pb.DoorbellShutdownReason_DOORBELL_SHUTDOWN_PEER_CLOSED
				if tc.ending == "future_reason" {
					reason = pb.DoorbellShutdownReason(12345)
				}
				if err := stream.Send(&pb.HostMsg{Msg: &pb.HostMsg_DoorbellShutdown{DoorbellShutdown: &pb.DoorbellShutdown{Reason: reason}}}); err != nil {
					t.Fatal(err)
				}
				report, err := stream.Recv()
				if err != nil || report.GetDoorbellFinalReport() == nil || !strings.Contains(report.GetDoorbellFinalReport().Detail, reason.String()) {
					t.Fatal(report, err)
				}
				if err := stream.Send(&pb.HostMsg{Msg: &pb.HostMsg_Shutdown{Shutdown: &pb.Shutdown{DeadlineMs: proto.Uint32(1000)}}}); err != nil {
					t.Fatal(err)
				}
				if report, err := stream.Recv(); err != nil || report.GetShutdownReport() == nil {
					t.Fatal(report, err)
				}
				select {
				case err := <-result:
					if err != nil {
						t.Fatal(err)
					}
				case <-ctx.Done():
					t.Fatal(ctx.Err())
				}
				return
			}
			if tc.ending != "" {
				// No Shutdown and no cancellation of ServeEnv's parent context.
				// Keep the doorbell open: control-stream termination alone suffices.
				if tc.ending == "eof" {
					err = stream.CloseSend()
				} else {
					err = conn.Close()
				}
				if err != nil {
					t.Fatal(err)
				}
				select {
				case err := <-result:
					if tc.ending == "eof" && err != nil {
						t.Fatal(err)
					}
				case <-time.After(time.Second):
					t.Fatal("ServeEnv remained running after host stream ended")
				}
				return
			}
			// An unknown release is reported on the wire, and leaves the stream
			// usable. ServeEnv must translate the memory violation exactly once.
			if err := stream.Send(&pb.HostMsg{Msg: &pb.HostMsg_ReleaseLoan{ReleaseLoan: &pb.ReleaseLoan{SegmentId: 987, Region: 3, Offset: 64, Len: 4}}}); err != nil {
				t.Fatal(err)
			}
			report, err := stream.Recv()
			if err != nil || report.GetContractViolation() == nil || report.GetContractViolation().Where != pb.ViolationSite_VIOLATION_SITE_UNKNOWN_RELEASE || report.GetContractViolation().Count != 1 || !strings.Contains(report.GetContractViolation().Detail, "987") {
				t.Fatalf("unknown release report: %v %v", report, err)
			}
			// A documented framing fault ends only the doorbell. The reliable stream
			// must report it once, then remain available for authoritative Shutdown.
			raw, err := doorbell.EncodeNotification(doorbell.Notification{SegmentID: 1}, 1)
			if err != nil {
				t.Fatal(err)
			}
			raw[108] ^= 1
			if err := unix.Sendmsg(pair[0], raw[:], nil, nil, 0); err != nil {
				t.Fatal(err)
			}
			fault, err := stream.Recv()
			if err != nil || fault.GetDoorbellFault().GetFailureClass() != pb.DoorbellFailureClass_DOORBELL_FAILURE_BAD_CRC {
				t.Fatalf("%v %v", fault, err)
			}
			if fault.Id != "" || fault.GetDoorbellFault().ManifestSource != nil {
				t.Fatal("fabricated fault identity or manifest source")
			}
			facts := fault.GetDoorbellFault()
			if facts.SdkVersion == nil || *facts.SdkVersion != SDKVersion || facts.ProcessHealthyAtDetection == nil || !*facts.ProcessHealthyAtDetection || facts.StreamHealthyAtDetection == nil || !*facts.StreamHealthyAtDetection {
				t.Fatal("missing local fault facts", facts)
			}
			if facts.PeerProtocolMajor == nil || *facts.PeerProtocolMajor != major || facts.PeerProtocolMinor == nil || *facts.PeerProtocolMinor != minor {
				t.Fatal("host Hello facts missing from fault", facts)
			}
			if err := stream.Send(&pb.HostMsg{Msg: &pb.HostMsg_Shutdown{Shutdown: &pb.Shutdown{DeadlineMs: proto.Uint32(1000)}}}); err != nil {
				t.Fatal(err)
			}
			if m, err := stream.Recv(); err != nil || m.GetShutdownReport().GetOutcome() != pb.ShutdownReport_NO_HOOK {
				t.Fatalf("%v %v", m, err)
			}
			if _, err := stream.Recv(); !errors.Is(err, io.EOF) {
				t.Fatalf("duplicate fault or unclean close: %v", err)
			}
			select {
			case err := <-result:
				if err != nil {
					t.Fatal(err)
				}
			case <-ctx.Done():
				t.Fatal(ctx.Err())
			}
		})
	}
}
