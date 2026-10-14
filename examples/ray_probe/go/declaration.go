package main

import (
	"embed"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/plugin"
)

//go:embed icons/*.svg
var paneIcons embed.FS

func declare(h plugin.Handlers, event func(*control.Session, doorbell.Event)) (*plugin.Application, error) {
	d := plugin.NewDefinition(plugin.Info{ID: "org.example.ray_probe", Name: "Ray probe", Version: "0.1.0"})
	d.Needs("data_access", "contribute_rois")
	commands := map[string]plugin.CommandRef{}
	commands["probe.set_radius"] = d.Command(plugin.CommandSpec{ID: "probe.set_radius", Title: "Set ROI radius"}, h.Commands["probe.set_radius"])
	commands["probe.set_refine"] = d.Command(plugin.CommandSpec{ID: "probe.set_refine", Title: "Refine at level 0"}, h.Commands["probe.set_refine"])
	commands["probe.undo"] = d.Command(plugin.CommandSpec{ID: "probe.undo", Title: "Undo pick"}, h.Commands["probe.undo"])
	commands["probe.clear"] = d.Command(plugin.CommandSpec{ID: "probe.clear", Title: "Clear picks"}, h.Commands["probe.clear"])
	commands["probe.contribute"] = d.Command(plugin.CommandSpec{ID: "probe.contribute", Title: "Contribute picks as ROIs", Menu: plugin.ToolsMenu()}, h.Commands["probe.contribute"])
	d.Panel(plugin.PanelSpec{ID: "probe", Title: "Ray probe",
		Root: plugin.Column("root",
			plugin.Label("status", "Ready"),
			plugin.Row("rradius",
				plugin.Label("lradius", "ROI radius (voxels)"),
				plugin.LineEdit("radius", "3", "", commands["probe.set_radius"]),
			),
			plugin.Checkbox("refine", "Refine at level 0", true, commands["probe.set_refine"]),
			plugin.Row("rbuttons",
				plugin.Button("undo", "Undo", commands["probe.undo"]),
				plugin.Button("clear", "Clear", commands["probe.clear"]),
				plugin.Button("contribute", "Contribute", commands["probe.contribute"]),
			),
			plugin.Label("count", "No picks"),
		),
	}, h.Panels["probe"])
	d.Panel(plugin.PanelSpec{ID: "profile", Title: "Ray profile",
		Root: plugin.Column("root",
			plugin.Image("plot"),
			plugin.Label("pick", "No pick yet"),
		),
	}, h.Panels["profile"])
	d.Overlay(plugin.OverlaySpec{ID: "probe.picks", Name: "Picks", SegmentMiB: 4})
	d.Overlay(plugin.OverlaySpec{ID: "probe.aim", Name: "Aim", SegmentMiB: 4})
	cancelOnMove := false
	// Definition routes assigned action indices; retain the existing event logic.
	action := func(s *control.Session, f *doorbell.Frame) error {
		event(s, doorbell.Event{Frame: f})
		return nil
	}
	d.Tool(plugin.ToolSpec{
		ID: "probe.point", Name: "Probe", Menu: plugin.ToolsMenu(),
		Views: []string{"2d", "3d"}, Mode: "gesture", CancelOnCameraMove: &cancelOnMove,
		Pane:     &plugin.PaneButton{Icon: "icons/probe.svg", Tooltip: "Probe: the brightest voxel under the pointer", Hold: "toggle"},
		Triggers: []plugin.Trigger{{Key: "Shift+P", Hold: "toggle"}, {Button: "middle"}},
		Actions: []plugin.ActionSpec{
			{ID: "undo", Name: "Undo pick", Key: "U", Handle: action,
				Pane: &plugin.PaneButton{Icon: "icons/undo.svg", Tooltip: "Undo the last pick"}},
			{ID: "clear", Name: "Clear picks", Key: "X", Handle: action,
				Pane: &plugin.PaneButton{Icon: "icons/clear.svg", Tooltip: "Clear every pick"}},
		},
	}, plugin.ToolHandlers{Event: event, Activated: h.Activated["probe.point"], Deactivated: h.Deactivated["probe.point"]})
	d.Resources(paneIcons, "icons/probe.svg", "icons/undo.svg", "icons/clear.svg")
	if h.Shutdown != nil {
		d.OnShutdown(h.Shutdown)
	}
	return d.Build()
}
