package main

import (
	"context"
	"os"
	"os/signal"

	"github.com/focolab/vizi-plugin-sdk/examples/peak_roi/go/internal/peakroi"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/plugin"
)

func main() {
	app, base, err := peakroi.Declaration()
	if err != nil {
		os.Exit(plugin.DeclarationExitCode(plugin.Info{ID: "org.example.peak_roi", Version: "0.1.0"}, os.Args[1:], err, plugin.RunOptions{}))
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	if code := app.RunCLI(ctx, os.Args[1:], plugin.RunOptions{Runtime: func() (control.Plugin, error) { return base, nil }}); code != 0 {
		os.Exit(code)
	}
}
