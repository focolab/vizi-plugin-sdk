package main

import (
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/plugin"
)

func declare(h plugin.Handlers, event func(*control.Session, doorbell.Event)) (*plugin.Application, error) {
	d := plugin.NewDefinition(plugin.Info{ID: "org.example.interactive_seg", Name: "Interactive Segmentation", Version: "0.1.0"})
	d.Needs("data_access", "contribute_rois")
	commands := map[string]plugin.CommandRef{}
	commands["seg.set_min_size"] = d.Command(plugin.CommandSpec{ID: "seg.set_min_size", Title: "Set minimum size"}, h.Commands["seg.set_min_size"])
	commands["seg.set_threshold"] = d.Command(plugin.CommandSpec{ID: "seg.set_threshold", Title: "Set threshold scale"}, h.Commands["seg.set_threshold"])
	commands["seg.set_3d"] = d.Command(plugin.CommandSpec{ID: "seg.set_3d", Title: "Set 3D growth"}, h.Commands["seg.set_3d"])
	commands["seg.set_mode"] = d.Command(plugin.CommandSpec{ID: "seg.set_mode", Title: "Set accumulate mode"}, h.Commands["seg.set_mode"])
	commands["seg.set_region"] = d.Command(plugin.CommandSpec{ID: "seg.set_region", Title: "Set measured region"}, h.Commands["seg.set_region"])
	commands["seg.pick"] = d.Command(plugin.CommandSpec{ID: "seg.pick", Title: "Activate lasso"}, h.Commands["seg.pick"])
	commands["seg.commit"] = d.Command(plugin.CommandSpec{ID: "seg.commit", Title: "Commit to annotations"}, h.Commands["seg.commit"])
	commands["seg.measure"] = d.Command(plugin.CommandSpec{ID: "seg.measure", Title: "Measure over time"}, h.Commands["seg.measure"])
	commands["seg.reset"] = d.Command(plugin.CommandSpec{ID: "seg.reset", Title: "Clear segmentation"}, h.Commands["seg.reset"])
	d.Panel(plugin.PanelSpec{ID: "seg", Title: "Interactive Segmentation",
		Root: plugin.Column("root",
			plugin.Button("pick", "Select region…", commands["seg.pick"]),
			plugin.Row("rsize",
				plugin.Label("lsize", "Minimum size (voxels)"),
				plugin.Slider(plugin.SliderSpec{ID: "size", Min: 1, Max: 5000, Step: 10, Value: 50, Float: false}, commands["seg.set_min_size"]),
			),
			plugin.Row("rthresh",
				plugin.Label("lthresh", "Threshold ×"),
				plugin.Slider(plugin.SliderSpec{ID: "thresh", Min: 0.5, Max: 2.0, Step: 0.05, Value: 1.0, Float: true}, commands["seg.set_threshold"]),
			),
			plugin.Checkbox("grow3d", "Grow through Z", true, commands["seg.set_3d"]),
			plugin.Dropdown("mode", []string{"Replace", "Add"}, "Replace", commands["seg.set_mode"]),
			plugin.Button("commit", "Commit to annotations", commands["seg.commit"]),
			plugin.Row("rmeasure",
				plugin.Dropdown("region", []string{"(none)"}, "(none)", commands["seg.set_region"]),
				plugin.Button("measure", "Measure", commands["seg.measure"]),
			),
			plugin.Image("plot"),
			plugin.Button("reset", "Clear", commands["seg.reset"]),
			plugin.Label("status", "Ready"),
		),
	}, h.Panels["seg"])
	d.Overlay(plugin.OverlaySpec{ID: "seg.trail", Name: "Lasso trail", SegmentMiB: 0})
	d.Overlay(plugin.OverlaySpec{ID: "seg.labels", Name: "Segmentation", SegmentMiB: 64})
	d.Tool(plugin.ToolSpec{ID: "seg.lasso", Menu: plugin.ToolsMenu(), Name: "Lasso segment", Views: []string{"2d"}}, plugin.ToolHandlers{Event: event, Activated: h.Activated["seg.lasso"], Deactivated: h.Deactivated["seg.lasso"]})
	if h.Shutdown != nil {
		d.OnShutdown(h.Shutdown)
	}
	return d.Build()
}
