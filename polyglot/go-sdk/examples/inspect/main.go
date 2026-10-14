// inspect declares its handlers and manifest together. Build it, then run
// --manifest[=DIR] or --install[=DIR]; no arguments serves a host session.
package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/plugin"
)

func main() {
	d := plugin.NewDefinition(plugin.Info{
		ID: "org.example.inspect", Name: "Dataset inspector", Version: "0.1.0",
	})
	d.Needs(plugin.DataAccess)
	inspect := d.Command(plugin.CommandSpec{
		ID: "inspect", Title: "Inspect active dataset", Menu: plugin.ToolsMenu(),
	}, func(ctx context.Context, host *control.Client, _ plugin.Args) (plugin.Result, error) {
		view, err := host.GetViewState(ctx)
		if err != nil {
			return plugin.Result{}, err
		}
		return plugin.Result{Message: fmt.Sprintf("%s: TCZYX=%v, t=%d, c=%d, generation=%d", view.DatasetName, view.ShapeTczyx, view.T, view.C, view.OpenGeneration)}, nil
	})
	d.Panel(plugin.PanelSpec{
		ID: "inspector", Title: "Dataset inspector",
		Root: plugin.Column("root",
			plugin.Label("description", "Inspect the active dataset's dimensions and position."),
			plugin.Button("inspect-button", "Inspect", inspect),
		),
	}, nil)
	app, err := d.Build()
	if err != nil {
		os.Exit(plugin.DeclarationExitCode(plugin.Info{ID: "org.example.inspect", Version: "0.1.0"}, os.Args[1:], err, plugin.RunOptions{}))
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	if code := app.RunCLI(ctx, os.Args[1:], plugin.RunOptions{}); code != 0 {
		os.Exit(code)
	}
}
