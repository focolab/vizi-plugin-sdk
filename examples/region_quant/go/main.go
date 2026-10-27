package main

import (
	"context"
	"os"
	"os/signal"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/plugin"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func declaration(a *application) (*plugin.Application, error) {
	a.traces = newTraceWorker(
		func(ctx context.Context) (*pb.ViewState, error) { return a.session.Client.GetViewState(ctx) },
		a.measure,
		func(text string) error { return a.handle().SetValue("tracing", plugin.String(text)) },
		func(err error) { a.session.Client.Diagnose(err) },
	)
	d := plugin.NewDefinition(plugin.Info{ID: "org.example.region_quant", Name: "Region Quant", Version: "0.1.0"})
	channel := d.Command(plugin.CommandSpec{ID: "quant.channel", Title: "Measure a channel"}, a.channel)
	pick := d.Command(plugin.CommandSpec{ID: "quant.pick", Title: "Select a region"}, a.pick)
	brightest := d.Command(plugin.CommandSpec{ID: "quant.brightest", Title: "Go to the brightest frame"}, a.brightest)
	export := d.Command(plugin.CommandSpec{ID: "quant.export", Title: "Export region means"}, a.export)
	a.panel = d.Panel(plugin.PanelSpec{ID: "quant", Title: "Region Quant", ViewTargets: []string{"t", "select_region"}, FollowTargets: []string{"frame_region"}, Root: plugin.Column("root",
		plugin.ChannelPicker("channel", 0, channel),
		plugin.ListRows(plugin.ListSpec{ID: "regions", MaxRows: maxRows, VisibleRows: 8, Selectable: true, Command: pick}, plugin.Label("name", ""), plugin.Label("voxels", ""), plugin.Label("mean", ""), plugin.Image("swatch")).MovesView(),
		plugin.Button("brightest", "Brightest frame", brightest).MovesView(), plugin.Button("export", "Export CSV…", export), plugin.Label("tracing", ""), plugin.Label("status", "Ready"),
	)}, a.opened)
	d.OnFollow(a.panel, a.tour)
	d.OnViewChanged(a.viewChanged)
	d.OnRegionsChanged(a.regionsChanged)
	return d.Build()
}
func main() {
	a := &application{ready: make(chan struct{}), gate: make(chan struct{}, 1), rows: map[string]row{}, pngs: map[int][]byte{}}
	app, err := declaration(a)
	if err != nil {
		os.Exit(plugin.DeclarationExitCode(plugin.Info{ID: "org.example.region_quant", Version: "0.1.0"}, os.Args[1:], err, plugin.RunOptions{}))
	}
	base := control.Plugin{
		Started: func(s *control.Session) { a.session = s; close(a.ready); go a.traces.run(s.Client.Context()) },
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	if code := app.RunCLI(ctx, os.Args[1:], plugin.RunOptions{Runtime: func() (control.Plugin, error) { return base, nil }}); code != 0 {
		os.Exit(code)
	}
}
