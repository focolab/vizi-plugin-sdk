package plugin

import (
	"context"
	"strings"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

// A command's outcome, the same in every SDK:
//
//	undeclared command                   -> Error{NOT_FOUND}
//	declared command, no handler         -> CommandDone{ok: false, command_id}
//	cancellation                         -> Error{CANCELLED}
//
// In Go a declared command always has a handler: Definition.Command declares
// and registers in one call and refuses a nil handler, and Handlers.Configure
// refuses a nil entry, so the second row is pinned as refused at build time.
func TestCommandOutcomes(t *testing.T) {
	h := Handlers{Commands: map[string]CommandHandler{
		"slow": func(ctx context.Context, _ *control.Client, _ Args) (Result, error) {
			control.StopWork(ctx, "stopped")
			return Result{}, nil
		},
	}}
	cfg, err := h.Configure(control.Config{BuildIdentity: "outcomes.test test build", PluginID: "outcomes.test", Handshake: func(*pb.Hello) error { return nil }})
	if err != nil {
		t.Fatal(err)
	}
	exchange := exportTestSession(t, cfg)
	invoke := func(command string) *pb.PluginMsg {
		return exchange(&pb.HostMsg{Msg: &pb.HostMsg_InvokeCommand{InvokeCommand: &pb.InvokeCommand{CommandId: command}}})
	}

	t.Run("undeclared", func(t *testing.T) {
		reply := invoke("nobody-declared-this")
		e := reply.GetError()
		if e == nil || e.Kind != pb.Error_NOT_FOUND || e.Message != `command "nobody-declared-this" is not one this plugin declares` {
			t.Fatalf("want Error{NOT_FOUND}, got %v", reply)
		}
	})
	t.Run("cancelled", func(t *testing.T) {
		reply := invoke("slow")
		if reply.GetError().GetKind() != pb.Error_CANCELLED {
			t.Fatalf("want Error{CANCELLED}, got %v", reply)
		}
	})
	t.Run("declared without a handler is refused at build", func(t *testing.T) {
		d := NewDefinition(Info{ID: "org.example.outcomes", Name: "Outcomes", Version: "0.1"})
		d.Command(CommandSpec{ID: "orphan"}, nil)
		if _, err := d.Build(); err == nil || !strings.Contains(err.Error(), `"orphan" needs a handler`) {
			t.Fatalf("want the build refused, got %v", err)
		}
		if _, err := (Handlers{Commands: map[string]CommandHandler{"orphan": nil}}).Configure(control.Config{}); err == nil {
			t.Fatal("want Configure to refuse a nil command handler")
		}
	})
}
