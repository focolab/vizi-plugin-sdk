//go:build linux || darwin

package plugin

import (
	"context"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"testing"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"golang.org/x/sys/unix"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/proto"
)

func TestToolLifecycleHooksThroughSession(t *testing.T) {
	t.Run("handlers", func(t *testing.T) { testToolLifecycleHooks(t, false) })
	t.Run("definition", func(t *testing.T) { testToolLifecycleHooks(t, true) })
}

func testToolLifecycleHooks(t *testing.T, declared bool) {
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	kind := unix.SOCK_SEQPACKET
	if runtime.GOOS == "darwin" {
		kind = unix.SOCK_DGRAM
	}
	pair, err := unix.Socketpair(unix.AF_UNIX, kind, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer unix.Close(pair[0])
	// Not t.TempDir(): it names the directory after the test, and Darwin's
	// sun_path is 104 bytes, which the subtest names here exceed.
	dir, err := os.MkdirTemp("", "vizi-hooks-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(dir)
	path := filepath.Join(dir, "s")
	t.Setenv("VIZI_PLUGIN_DOORBELL_FD", strconv.Itoa(pair[1]))
	t.Setenv("VIZI_PLUGIN_UDS", path)
	events := make(chan string, 16)
	cancelEntered, releaseCancel := make(chan struct{}), make(chan struct{})
	defer func() {
		select {
		case <-releaseCancel:
		default:
			close(releaseCancel)
		}
	}()
	h := Handlers{
		Activated: map[string]ToolLifecycleHandler{"lasso": func(_ context.Context, _ *control.Client, b *pb.ToolBinding) error {
			if b.Tool != 1 || b.GetActions()[0].ActionId != "node" {
				t.Error("activation lost binding", b)
			}
			events <- "activated"
			b.ToolId = "mutated" // snapshots cannot mutate state or subsequent retirement
			return nil
		}, "barrier": func(context.Context, *control.Client, *pb.ToolBinding) error {
			events <- "barrier"
			return nil
		}},
		Deactivated: map[string]ToolLifecycleHandler{"lasso": func(_ context.Context, _ *control.Client, b *pb.ToolBinding) error {
			if !b.Retired || b.Revision != 3 || b.GetActions()[0].ActionId != "node" {
				t.Error("retirement lost binding", b)
			}
			events <- "deactivated"
			return nil
		}},
	}
	event := func(_ *control.Session, e doorbell.Event) {
		if e.Frame != nil && e.Frame.Input != nil {
			if e.Frame.Input.Phase == doorbell.Fire {
				if e.Frame.Input.Action == 2 {
					events <- "action"
				} else {
					events <- "unknown action"
				}
			} else {
				events <- "press"
			}
		}
		if e.Cancellation != nil {
			close(cancelEntered)
			select {
			case <-releaseCancel:
			case <-ctx.Done():
			}
			events <- "cancel"
		}
	}
	var cfg control.Plugin
	if declared {
		d := NewDefinition(Info{ID: "hooks.test", Name: "Tool hooks", Version: "1"})
		for _, id := range []string{"lasso", "barrier"} {
			spec := ToolSpec{ID: id}
			if id == "lasso" {
				spec.Actions = []ActionSpec{
					{ID: "node", Key: "N", Handle: func(_ *control.Session, f *doorbell.Frame) error {
						if f.Input.Action != 2 {
							t.Error("action did not use current host assignment")
						}
						events <- "action"
						return nil
					}},
					{ID: "other", Key: "O", Handle: func(*control.Session, *doorbell.Frame) error {
						t.Error("action routed by declaration index")
						return nil
					}},
				}
			}
			d.Tool(spec, ToolHandlers{Event: event, Activated: h.Activated[id], Deactivated: h.Deactivated[id]})
		}
		app, buildErr := d.Build()
		if buildErr != nil {
			t.Fatal(buildErr)
		}
		cfg, err = app.Configure(control.Plugin{})
	} else {
		cfg.Control, err = h.Configure(control.Config{PluginID: "hooks.test"})
		cfg.DoorbellHandler = event
	}
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { done <- control.ServeEnv(ctx, cfg) }()
	conn, err := grpc.NewClient("passthrough:///hooks", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
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
	next := func(want string) {
		t.Helper()
		select {
		case got := <-events:
			if got != want {
				t.Fatalf("got %s, want %s", got, want)
			}
		case <-ctx.Done():
			t.Fatal(ctx.Err())
		}
	}
	send(&pb.HostMsg{Id: "Hello from hook-test 1.0", Msg: &pb.HostMsg_Hello{Hello: &pb.Hello{ProtocolMajor: 1}}})
	if recv().GetReady() == nil {
		t.Fatal("no Ready")
	}
	b := &pb.ToolBinding{Tool: 1, ToolId: "lasso", Revision: 1, Mode: pb.ToolMode_TOOL_MODE_GESTURE, Actions: []*pb.ToolAction{{Action: 1, ActionId: "node"}}}
	send(&pb.HostMsg{Msg: &pb.HostMsg_ToolBinding{ToolBinding: b}})
	if recv().GetToolBindingAck().GetTool() != 1 {
		t.Fatal("no activation ack")
	}
	next("activated")
	// Replay and in-place restatement both ack, but neither re-engages the tool.
	send(&pb.HostMsg{Msg: &pb.HostMsg_ToolBinding{ToolBinding: b}})
	recv()
	b.Revision = 2
	// Swap the host's indices without re-engagement. Dispatch must consult the
	// current arrangement, rather than caching the declaration's position.
	b.Actions = []*pb.ToolAction{{Action: 2, ActionId: "node"}, {Action: 1, ActionId: "other"}}
	send(&pb.HostMsg{Msg: &pb.HostMsg_ToolBinding{ToolBinding: b}})
	recv()
	g := &pb.Geometry{Gen: 7, Levels: []*pb.LevelScale{{Z: 1, Y: 1, X: 1}}, Crop: &pb.EdgeBox{Z0: -.5, Z1: 9.5, Y0: -.5, Y1: 9.5, X0: -.5, X1: 9.5}}
	send(&pb.HostMsg{Msg: &pb.HostMsg_EpochChange{EpochChange: &pb.EpochChange{Datasets: []*pb.DatasetState{{Gen: 7, Geometry: g}}, Panes: []*pb.PaneEpoch{{ViewId: 0, Epoch: 1, Gen: 7}}}}})
	if recv().GetEpochAck() == nil {
		t.Fatal("no epoch ack")
	}
	raw := hostInputRecord(doorbell.Input{Generation: 7, Epoch: 1, Interaction: 1, Tool: 1, Phase: doorbell.Press, Channels: 1, Position: doorbell.Position{Kind: doorbell.Voxel, Values: [12]float32{2, 3, 4}}}, 1)
	if err := unix.Sendmsg(pair[0], raw[:], nil, nil, 0); err != nil {
		t.Fatal(err)
	}
	next("press")
	for i, action := range []uint16{2, 63} {
		raw = hostInputRecord(doorbell.Input{Generation: 7, Epoch: 1, Interaction: uint32(i + 2), Tool: 1, Phase: doorbell.Fire, Action: action, Channels: 1, Position: doorbell.Position{Kind: doorbell.Voxel, Values: [12]float32{2, 3, 4}}}, uint32(i+2))
		if err := unix.Sendmsg(pair[0], raw[:], nil, nil, 0); err != nil {
			t.Fatal(err)
		}
		if action == 2 {
			next("action")
		} else {
			next("unknown action")
		}
	}
	// Omit ID and actions from retirement: the hook must retain the live binding.
	send(&pb.HostMsg{Msg: &pb.HostMsg_ToolBinding{ToolBinding: &pb.ToolBinding{Tool: 1, Revision: 3, Retired: true}}})
	if recv().GetToolBindingAck().GetRevision() != 3 {
		t.Fatal("no retirement ack")
	}
	select {
	case <-cancelEntered:
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
	select {
	case e := <-events:
		t.Fatal("hook overtook cancellation", e)
	default:
	}
	close(releaseCancel)
	next("cancel")
	next("deactivated")
	// Refused binding: no callback. A later valid hook is a listener barrier,
	// so shutdown cannot suppress an incorrectly queued callback and hide a bug.
	send(&pb.HostMsg{Msg: &pb.HostMsg_ToolBinding{ToolBinding: &pb.ToolBinding{Tool: 2, ToolId: "lasso", Revision: 1}}})
	send(&pb.HostMsg{Msg: &pb.HostMsg_ToolBinding{ToolBinding: &pb.ToolBinding{Tool: 3, ToolId: "barrier", Revision: 1, Mode: pb.ToolMode_TOOL_MODE_GESTURE}}})
	for recv().GetToolBindingAck().GetTool() != 3 {
	}
	next("barrier")
	send(&pb.HostMsg{Msg: &pb.HostMsg_Shutdown{Shutdown: &pb.Shutdown{DeadlineMs: proto.Uint32(2000)}}})
	for recv().GetShutdownReport() == nil {
	}
	select {
	case e := <-events:
		t.Fatal("unexpected duplicate or refused-binding hook", e)
	default:
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
