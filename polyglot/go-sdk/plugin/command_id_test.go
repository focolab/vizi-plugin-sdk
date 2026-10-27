package plugin

import (
	"context"
	"errors"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

// §8.3: CommandDone.command_id echoes the invoked command's id on every path
// -- success, a handler error, a panic, an argument-validation failure (a
// missing or ill-typed arg), and a failure raised while scheduling after-reply
// work.  A CommandDone without it is the sender's protocol error.  An
// undeclared command is Error{NOT_FOUND} (command_outcomes_test.go).
func TestCommandDoneAlwaysEchoesCommandID(t *testing.T) {
	h := Handlers{Commands: map[string]CommandHandler{
		"ok": func(context.Context, *control.Client, Args) (Result, error) { return Result{Message: "fine"}, nil },
		"needs-flag": func(_ context.Context, _ *control.Client, args Args) (Result, error) {
			if _, err := args.Bool("flag"); err != nil {
				return Result{}, err
			}
			return Result{}, nil
		},
		"fails": func(context.Context, *control.Client, Args) (Result, error) { return Result{}, errors.New("refused") },
		"host-error": func(context.Context, *control.Client, Args) (Result, error) {
			return Result{}, &control.HostError{Kind: pb.Error_IO_ERROR, Message: "disk full"}
		},
		"panics": func(context.Context, *control.Client, Args) (Result, error) { panic("boom") },
	}}
	cfg, err := h.Configure(control.Config{BuildIdentity: "command-id.test test build", PluginID: "command-id.test", Handshake: func(*pb.Hello) error { return nil }})
	if err != nil {
		t.Fatal(err)
	}
	exchange := exportTestSession(t, cfg)
	for _, tc := range []struct {
		name, command string
		args          map[string]*pb.Value
		ok            bool
	}{
		{"success", "ok", nil, true},
		{"missing arg", "needs-flag", nil, false},
		{"ill-typed arg", "needs-flag", map[string]*pb.Value{"flag": String("yes")}, false},
		{"handler error", "fails", nil, false},
		{"host error", "host-error", nil, false},
		{"panic", "panics", nil, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			reply := exchange(&pb.HostMsg{Msg: &pb.HostMsg_InvokeCommand{InvokeCommand: &pb.InvokeCommand{CommandId: tc.command, Args: tc.args}}})
			done := reply.GetCommandDone()
			if done == nil || done.Ok != tc.ok || done.CommandId != tc.command {
				t.Fatalf("want CommandDone{command_id: %q, ok: %v}, got %v", tc.command, tc.ok, reply)
			}
		})
	}
}
