package plugin

import (
	"context"
	"net"
	"strings"
	"testing"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/proto"
)

func TestDeclaredListsAndRowOperations(t *testing.T) {
	d := definition()
	command := d.Command(CommandSpec{ID: "select"}, func(_ context.Context, _ *control.Client, args Args) (Result, error) {
		row, err := args.String("row")
		return Result{Message: row}, err
	})
	cells := []Widget{Label("name", "template"), Checkbox("on", "", false, command), Image("image"), Dropdown("choice", []string{"one"}, "one", command), Progress("progress", 0, false)}
	ref := d.Panel(PanelSpec{ID: "p", Title: "Rows", Root: ListRows(ListSpec{ID: "rows", MaxRows: 2, VisibleRows: 1, Selectable: true, Command: command}, cells...)}, nil)
	cells[0] = Label("changed", "oops")
	app := buildDefinition(t, d)
	json, err := app.Manifest("plugin")
	if err != nil {
		t.Fatal(err)
	}
	m := decodeManifest(t, json)
	if m.WidgetCount() != 11 || m.Contributions.Panels[0].Root.Row[0].ID != "name" {
		t.Fatal(string(json))
	}
	cfg, err := app.Configure(control.Plugin{})
	if err != nil {
		t.Fatal(err)
	}
	client, stream := listTestSession(t, cfg.Control)
	recv := func() *pb.PluginMsg {
		t.Helper()
		m, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		return m
	}
	panel := ref.Bind(client)
	// Non-image operations are legal before PanelOpened; the host keeps state.
	if err := panel.SetRows("rows", "a", "b"); err != nil {
		t.Fatal(err)
	}
	if r := recv().GetPanelPatch().Ops[0]; r.WidgetId != "rows" || r.RowKey != "" || len(r.GetSetRows().Keys) != 2 {
		t.Fatal(r)
	}
	for _, keys := range [][]string{{"a", "b", "c"}, {"a", "a"}, {""}, {strings.Repeat("é", 129)}, {"\xff"}} {
		if err := panel.SetRows("rows", keys...); err == nil {
			t.Fatal("accepted invalid rows", keys)
		}
	}
	if err := panel.Row("a").SetRows("rows"); err == nil {
		t.Fatal("nested rows accepted")
	}
	// A rejected replacement sent nothing, and a whole empty list is allowed.
	if err := panel.SetRows("rows"); err != nil {
		t.Fatal(err)
	}
	if r := recv().GetPanelPatch().Ops[0].GetSetRows(); r == nil || len(r.Keys) != 0 {
		t.Fatal(r)
	}
	key := strings.Repeat("é", 128)
	if err := panel.SetRows("rows", key); err != nil {
		t.Fatal(err)
	}
	if got := recv().GetPanelPatch().Ops[0].GetSetRows().Keys[0]; got != key {
		t.Fatal(got)
	}
	cell := panel.Row(key)
	cases := []struct {
		run  func() error
		want *pb.PatchOp
	}{
		{func() error { return cell.SetValue("name", String("new")) }, &pb.PatchOp{WidgetId: "name", Op: &pb.PatchOp_SetValue{SetValue: String("new")}}},
		{func() error { return cell.SetEnabled("name", false) }, &pb.PatchOp{WidgetId: "name", Op: &pb.PatchOp_SetEnabled{SetEnabled: false}}},
		{func() error { return cell.SetVisible("name", true) }, &pb.PatchOp{WidgetId: "name", Op: &pb.PatchOp_SetVisible{SetVisible: true}}},
		{func() error { return cell.SetOptions("choice", "one", "two") }, &pb.PatchOp{WidgetId: "choice", Op: &pb.PatchOp_SetOptions{SetOptions: &pb.Options{Options: []string{"one", "two"}}}}},
		{func() error { return cell.SetProgress("progress", .4, true) }, &pb.PatchOp{WidgetId: "progress", Op: &pb.PatchOp_SetProgress{SetProgress: &pb.Progress{Indeterminate: true}}}},
	}
	for _, tc := range cases {
		if err := tc.run(); err != nil {
			t.Fatal(err)
		}
		tc.want.RowKey = key
		if got := recv().GetPanelPatch().Ops[0]; !proto.Equal(got, tc.want) {
			t.Fatalf("got %v want %v", got, tc.want)
		}
	}
	image := &pb.PatchOp{WidgetId: "image", Op: &pb.PatchOp_SetImage{SetImage: &pb.ImageRef{SegmentId: 7, Offset: 32, Len: 16}}}
	if err := cell.Patch(image); err != nil {
		t.Fatal(err)
	}
	if got := recv().GetPanelPatch().Ops[0]; got.RowKey != key || got.GetSetImage().SegmentId != 7 || image.RowKey != "" {
		t.Fatal(got, image)
	}
	// Selection and row-cell events both use the ordinary typed argument map.
	for _, args := range []map[string]*pb.Value{{"row": String(key)}, {"row": String("b"), "value": Bool(true)}} {
		if err := stream.Send(&pb.HostMsg{Id: "event", Msg: &pb.HostMsg_InvokeCommand{InvokeCommand: &pb.InvokeCommand{CommandId: command.ID(), Args: args}}}); err != nil {
			t.Fatal(err)
		}
		if got := recv(); got.GetCommandDone() == nil || !got.GetCommandDone().Ok || got.GetCommandDone().Message != args["row"].GetS() {
			t.Fatal(got)
		}
	}
}

func TestSelectableListRequiresItsOwnCommand(t *testing.T) {
	foreign := definition().Command(CommandSpec{ID: "foreign"}, noopCommand)
	for _, spec := range []ListSpec{{ID: "rows", Selectable: true}, {ID: "rows", Command: foreign}, {ID: "rows", Selectable: true, Command: foreign}} {
		d := definition()
		d.Panel(PanelSpec{ID: "p", Title: "P", Root: ListRows(spec, Label("name", ""))}, nil)
		if _, err := d.Build(); err == nil {
			t.Fatal("accepted bad command binding", spec)
		}
	}
}

func listTestSession(t *testing.T, cfg control.Config) (*control.Client, grpc.BidiStreamingClient[pb.HostMsg, pb.PluginMsg]) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	t.Cleanup(cancel)
	attached := make(chan *control.Client, 1)
	cfg.Handshake = func(*pb.Hello) error { return nil }
	cfg.Attach = func(c *control.Client) error { attached <- c; return nil }
	server, err := control.NewServer(cfg)
	if err != nil {
		t.Fatal(err)
	}
	l := bufconn.Listen(1 << 20)
	g := server.GRPCServer()
	go g.Serve(l)
	t.Cleanup(func() { g.Stop(); l.Close() })
	conn, err := grpc.NewClient("passthrough:///list", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return l.Dial() }))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	stream, err := pb.NewPluginClient(conn).Session(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if err := stream.Send(&pb.HostMsg{Id: "Hello from list test", Msg: &pb.HostMsg_Hello{Hello: &pb.Hello{ProtocolMajor: 1}}}); err != nil {
		t.Fatal(err)
	}
	if ready, err := stream.Recv(); err != nil || ready.GetReady() == nil {
		t.Fatal(ready, err)
	}
	select {
	case c := <-attached:
		return c, stream
	case <-ctx.Done():
		t.Fatal(ctx.Err())
		return nil, nil
	}
}
