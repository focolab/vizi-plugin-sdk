package main

import (
	_ "embed"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/manifest"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/plugin"
)

//go:embed vizi-crate-cal.bin
var calibrationResource []byte

func declare(h plugin.Handlers) (*plugin.Application, error) {
	d := plugin.NewDefinition(plugin.Info{ID: "org.example.format_bridge", Name: "Crate Format Bridge", Version: "0.1.0"})
	d.Needs("data_access")
	commands := map[string]plugin.CommandRef{}
	commands["crate.set_verify"] = d.Command(plugin.CommandSpec{ID: "crate.set_verify", Title: "Verify CRC-32 on volume reads"}, h.Commands["crate.set_verify"])
	commands["crate.describe"] = d.Command(plugin.CommandSpec{ID: "crate.describe", Title: "Describe active dataset"}, h.Commands["crate.describe"])
	commands["crate.pick_calibration"] = d.Command(plugin.CommandSpec{ID: "crate.pick_calibration", Title: "Use a different calibration"}, h.Commands["crate.pick_calibration"])
	commands["crate.check_file"] = d.Command(plugin.CommandSpec{ID: "crate.check_file", Title: "Check a crate file…", Menu: plugin.ToolsMenu(), DefaultKeybinding: "Ctrl+Shift+K"}, h.Commands["crate.check_file"])
	d.Panel(plugin.PanelSpec{ID: "crate", Title: "Crate", MenuLabel: "Crate bridge…",
		Root: plugin.Column("root",
			plugin.Label("head", "No crate open"),
			plugin.Label("status", "Ready"),
			plugin.Row("rvox",
				plugin.Label("lvox", "Voxel size"),
				plugin.Label("vox", "(unknown)"),
			),
			plugin.Row("rcal",
				plugin.Label("lcal", "Calibration"),
				plugin.Label("cal", "(not provisioned)"),
			),
			plugin.Checkbox("verify", "Verify CRC-32 on volume reads", true, commands["crate.set_verify"]),
			plugin.Button("describe", "Describe active dataset", commands["crate.describe"]),
			plugin.Button("pickcal", "Use a different calibration…", commands["crate.pick_calibration"]),
		),
	}, h.Panels["crate"])
	d.Reader(plugin.ReaderSpec{ID: "crate", Name: "Crate volume container", Extensions: []string{".crate"}}, h.Readers["crate"])
	d.Exporter(plugin.ExporterSpec{ID: "crate", Name: "Crate volume container", Extension: ".crate"}, h.Export)
	d.Asset(plugin.AssetSpec{ID: "calibration", Name: "Crate sensor calibration v1", Filename: "vizi-crate-cal.bin", Size: 32, SHA256: "20845d7cc64ec17ab899a993c1138352828823e55ebc1959f257a28e528671d3", Source: manifest.AssetSource{Kind: "manual"}})
	d.Resource("vizi-crate-cal.bin", calibrationResource)
	if h.Shutdown != nil {
		d.OnShutdown(h.Shutdown)
	}
	return d.Build()
}
