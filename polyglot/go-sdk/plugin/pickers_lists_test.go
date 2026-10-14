package plugin

import (
	"context"
	"math"
	"slices"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

func TestIntegerListValues(t *testing.T) {
	values := []int64{math.MinInt64, -2, 0, 2, math.MaxInt64}
	value := Ints(values...)
	values[0] = 99
	wire, err := proto.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	decoded := new(pb.Value)
	if err := proto.Unmarshal(wire, decoded); err != nil {
		t.Fatal(err)
	}
	got, err := (Args{"value": decoded}).Ints("value")
	if err != nil || !slices.Equal(got, []int64{math.MinInt64, -2, 0, 2, math.MaxInt64}) {
		t.Fatal(got, err)
	}
	got[1] = 99
	if decoded.GetInts().Values[1] != -2 {
		t.Fatal("list aliases wire storage")
	}
	if got, err := (Args{"value": Ints()}).Ints("value"); err != nil || len(got) != 0 {
		t.Fatal(got, err)
	}
	for _, a := range []Args{nil, {"value": Int(1)}, {"value": String("1")}} {
		if _, err := a.Ints("value"); err == nil {
			t.Fatal("accepted non-list", a)
		}
	}
}

func TestDeclaredPickersSubscriptionsAndListCommand(t *testing.T) {
	d := definition()
	chosen := make(chan []int64, 1)
	cmd := d.Command(CommandSpec{ID: "select"}, func(_ context.Context, c *control.Client, args Args) (Result, error) {
		v, err := args.Ints("value")
		if err != nil {
			return Result{}, err
		}
		chosen <- v
		return Result{Message: "picked"}, nil
	})
	initial := []int64{0, 2}
	multi := MultiChannelPicker("channels", initial, cmd)
	initial[0] = 99
	d.Panel(PanelSpec{ID: "p", Title: "Pickers", Root: Column("root", multi, ChannelPicker("one", 0, cmd), ColorPicker("color", "#FF8800", cmd), ColormapPicker("map", "gray", cmd), RoiPicker("roi", "", cmd))}, nil)
	viewCalled, regionsCalled := false, false
	d.OnViewChanged(func(context.Context, *control.Client, *pb.ViewChanged) error { viewCalled = true; return nil })
	d.OnRegionsChanged(func(context.Context, *control.Client, *pb.RegionsChanged) error { regionsCalled = true; return nil })
	app := buildDefinition(t, d)
	body, err := app.Manifest("plugin")
	if err != nil {
		t.Fatal(err)
	}
	m := decodeManifest(t, body)
	if !slices.Equal(m.Subscriptions, []string{View, Regions}) || !slices.Equal(m.Permissions, []string{DataAccess}) {
		t.Fatal(string(body))
	}
	children := m.Contributions.Panels[0].Root.Children
	if children[0].Multiple == nil || !*children[0].Multiple || children[0].Value.([]any)[0] != float64(0) {
		t.Fatal(children)
	}
	for _, child := range children {
		if child.Command != cmd.ID() {
			t.Fatal(child)
		}
	}
	cfg, err := app.Configure(control.Plugin{})
	if err != nil {
		t.Fatal(err)
	}
	if err := cfg.Control.ViewChanged(context.Background(), nil, &pb.ViewChanged{}); err != nil {
		t.Fatal(err)
	}
	if err := cfg.Control.RegionsChanged(context.Background(), nil, &pb.RegionsChanged{}); err != nil {
		t.Fatal(err)
	}
	if !viewCalled || !regionsCalled {
		t.Fatal("subscription hooks missing")
	}
	// The generated interface receives a list-valued command over gRPC.
	cfg.Control.Handshake = func(*pb.Hello) error { return nil }
	exchange := exportTestSession(t, cfg.Control)
	reply := exchange(&pb.HostMsg{Msg: &pb.HostMsg_InvokeCommand{InvokeCommand: &pb.InvokeCommand{CommandId: "select", Args: map[string]*pb.Value{"value": Ints(0, 2)}}}})
	if !slices.Equal(<-chosen, []int64{0, 2}) || reply.GetCommandDone().Message != "picked" {
		t.Fatal(reply)
	}
}
