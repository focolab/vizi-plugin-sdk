package peakroi

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"os"
	"slices"
	"testing"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/proto"
)

// Independently verify this plugin's startup and shutdown over gRPC. The
// emulator originally rejected its successful COMPLETED hook outcome.
// This test deliberately has no doorbell; hostsim covers the real transport.
func TestRuntimeEarlyCallbacksAndCompletedShutdown(t *testing.T) {
	runtime, err := Runtime()
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	start := make(chan struct{})
	cfg := runtime.Control
	cfg.Handshake = func(*pb.Hello) error { return nil }
	cfg.Connected = func(client *control.Client) {
		select {
		case <-start:
			runtime.Started(&control.Session{Client: client})
		case <-ctx.Done():
		}
	}
	server, err := control.NewServer(cfg)
	if err != nil {
		t.Fatal(err)
	}
	listener := bufconn.Listen(1 << 20)
	grpcServer := server.GRPCServer()
	go grpcServer.Serve(listener)
	defer grpcServer.Stop()
	defer listener.Close()
	conn, err := grpc.NewClient("passthrough:///peak-test", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	stream, err := pb.NewPluginClient(conn).Session(ctx)
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
	send(&pb.HostMsg{Id: "Hello from peak test 1.0", Msg: &pb.HostMsg_Hello{Hello: &pb.Hello{ProtocolMajor: 1}}})
	if recv().GetReady() == nil {
		t.Fatal("missing Ready")
	}
	send(&pb.HostMsg{Msg: &pb.HostMsg_PanelOpened{PanelOpened: &pb.PanelOpened{PanelId: "peak"}}})
	send(&pb.HostMsg{Id: "edit", Msg: &pb.HostMsg_InvokeCommand{InvokeCommand: &pb.InvokeCommand{CommandId: "peak.set_z", Args: map[string]*pb.Value{"value": {Kind: &pb.Value_S{S: "2"}}}}}})
	close(start)
	widgets := map[string]bool{}
	done := false
	for !done || len(widgets) < 5 {
		m := recv()
		if p := m.GetPanelPatch(); p != nil {
			for _, op := range p.Ops {
				widgets[op.WidgetId] = true
			}
		}
		if d := m.GetCommandDone(); d != nil {
			if m.Id != "edit" || !d.Ok {
				t.Fatal(m)
			}
			done = true
		}
	}
	send(&pb.HostMsg{Msg: &pb.HostMsg_Shutdown{Shutdown: &pb.Shutdown{DeadlineMs: proto.Uint32(1000)}}})
	if report := recv().GetShutdownReport(); report == nil || report.Outcome != pb.ShutdownReport_COMPLETED {
		t.Fatal("shutdown outcome", report)
	}
	if _, err := stream.Recv(); !errors.Is(err, io.EOF) {
		t.Fatal("shutdown did not close stream", err)
	}
}

func TestManifestAndDefaults(t *testing.T) {
	// The Go SDK has no manifest validator. Check this plugin's declarations
	// against the documented subset it uses, without claiming full validation.
	type widget struct {
		Kind, ID, Command, Text string
		Value                   json.RawMessage
		Options                 []string
		Children                []json.RawMessage
	}
	var m struct {
		ManifestVersion int `json:"manifest_version"`
		ID, Version     string
		Engines         map[string]string
		Entry           struct{ Command []string }
		Permissions     []string
		Contributions   struct {
			Commands []struct {
				ID   string
				Menu json.RawMessage
			}
			Panels []struct {
				ID   string
				Root json.RawMessage
			}
		}
	}
	data, err := os.ReadFile("../../vizi-plugin.json")
	if err != nil {
		t.Fatal(err)
	}
	if err = json.Unmarshal(data, &m); err != nil {
		t.Fatal(err)
	}
	if m.ManifestVersion != 1 || m.ID != "org.example.peak_roi" || m.Version != "0.1.0" || m.Engines["vizi"] != "1.0" || !slices.Equal(m.Entry.Command, []string{"./peak-roi"}) {
		t.Fatal("manifest identity/entry")
	}
	if !slices.Equal(m.Permissions, []string{"data_access", "contribute_rois"}) {
		t.Fatal(m.Permissions)
	}
	commands := map[string]bool{}
	for _, c := range m.Contributions.Commands {
		if string(c.Menu) != "null" || commands[c.ID] {
			t.Fatal("command declaration", c)
		}
		commands[c.ID] = true
	}
	for _, id := range []string{"peak.set_z", "peak.set_y", "peak.set_x", "peak.set_mode", "peak.run"} {
		if !commands[id] {
			t.Fatal("missing command", id)
		}
	}
	if len(commands) != 5 || len(m.Contributions.Panels) != 1 || m.Contributions.Panels[0].ID != "peak" {
		t.Fatal("contributions")
	}
	widgets := map[string]widget{}
	var visit func(json.RawMessage)
	visit = func(raw json.RawMessage) {
		var w widget
		if err := json.Unmarshal(raw, &w); err != nil {
			t.Fatal(err)
		}
		if w.ID == "" {
			t.Fatal("missing widget ID")
		}
		if _, exists := widgets[w.ID]; exists {
			t.Fatal("duplicate widget")
		}
		widgets[w.ID] = w
		if w.Command != "" && !commands[w.Command] {
			t.Fatal("unknown command")
		}
		for _, child := range w.Children {
			visit(child)
		}
	}
	visit(m.Contributions.Panels[0].Root)
	a := newApplication(context.Background(), nil)
	for axis, id := range []string{"ez", "ey", "ex"} {
		var value string
		if err := json.Unmarshal(widgets[id].Value, &value); err != nil || value != a.extents[axis] {
			t.Fatal("extent default", id)
		}
		if widgets[[]string{"lz", "ly", "lx"}[axis]].Text != []string{"Z extent (voxels)", "Y extent (voxels)", "X extent (voxels)"}[axis] {
			t.Fatal("axis label")
		}
	}
	if widgets["status"].Text != a.status || string(widgets["mode"].Value) != `"Current timepoint"` || !slices.Equal(widgets["mode"].Options, []string{"Current timepoint", "All timepoints"}) {
		t.Fatal("panel defaults")
	}
}
