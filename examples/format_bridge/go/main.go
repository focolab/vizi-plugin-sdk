package main

import (
	"context"
	"log"
	"os"
	"os/signal"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/plugin"
)

func main() {
	if len(os.Args) > 1 && (os.Args[1] == "compare" || os.Args[1] == "recreate" || os.Args[1] == "check") {
		os.Exit(runTool(os.Args[1:], os.Stdout, os.Stderr))
	}
	a := newApplication()
	app, base, err := a.declarationRuntime()
	if err != nil {
		os.Exit(plugin.DeclarationExitCode(plugin.Info{ID: "org.example.format_bridge", Version: "0.1.0"}, os.Args[1:], err, plugin.RunOptions{}))
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	code := app.RunCLI(ctx, os.Args[1:], plugin.RunOptions{Runtime: func() (control.Plugin, error) { return base, nil }})
	if closeErr := a.shutdown(context.Background()); closeErr != nil {
		log.Print(closeErr)
	}
	if code != 0 {
		os.Exit(code)
	}
}
