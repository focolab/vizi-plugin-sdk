package peakroi

import (
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/plugin"
)

func declare(h plugin.Handlers) (*plugin.Application, error) {
	d := plugin.NewDefinition(plugin.Info{ID: "org.example.peak_roi", Name: "Peak ROI", Version: "0.1.0"})
	d.Needs("data_access", "contribute_rois")
	commands := map[string]plugin.CommandRef{}
	commands["peak.set_z"] = d.Command(plugin.CommandSpec{ID: "peak.set_z", Title: "Set Z extent"}, h.Commands["peak.set_z"])
	commands["peak.set_y"] = d.Command(plugin.CommandSpec{ID: "peak.set_y", Title: "Set Y extent"}, h.Commands["peak.set_y"])
	commands["peak.set_x"] = d.Command(plugin.CommandSpec{ID: "peak.set_x", Title: "Set X extent"}, h.Commands["peak.set_x"])
	commands["peak.set_mode"] = d.Command(plugin.CommandSpec{ID: "peak.set_mode", Title: "Set timepoint mode"}, h.Commands["peak.set_mode"])
	commands["peak.run"] = d.Command(plugin.CommandSpec{ID: "peak.run", Title: "Find peak"}, h.Commands["peak.run"])
	d.Panel(plugin.PanelSpec{ID: "peak", Title: "Peak ROI",
		Root: plugin.Column("root",
			plugin.Row("rz",
				plugin.Label("lz", "Z extent (voxels)"),
				plugin.LineEdit("ez", "4", "", commands["peak.set_z"]),
			),
			plugin.Row("ry",
				plugin.Label("ly", "Y extent (voxels)"),
				plugin.LineEdit("ey", "8", "", commands["peak.set_y"]),
			),
			plugin.Row("rx",
				plugin.Label("lx", "X extent (voxels)"),
				plugin.LineEdit("ex", "8", "", commands["peak.set_x"]),
			),
			plugin.Dropdown("mode", []string{"Current timepoint", "All timepoints"}, "Current timepoint", commands["peak.set_mode"]),
			plugin.Button("go", "Find peak", commands["peak.run"]),
			plugin.Label("status", "Ready"),
		),
	}, h.Panels["peak"])
	if h.Shutdown != nil {
		d.OnShutdown(h.Shutdown)
	}
	return d.Build()
}
