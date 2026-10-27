package control

import (
	"context"
	"errors"
	"fmt"
	"testing"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

// §8.3: the control layer stamps CommandDone.command_id with the invoked
// command's id whatever a raw handler returned -- a CommandDone that omits it,
// one that names another command, an error, a HostError and a panic alike.
func TestControlCommandDoneEchoesCommandID(t *testing.T) {
	p := openPeer(t, Config{HandleRequest: func(ctx context.Context, c *Client, in *pb.HostMsg) (*pb.PluginMsg, error) {
		switch in.GetInvokeCommand().GetCommandId() {
		case "omits":
			return &pb.PluginMsg{Msg: &pb.PluginMsg_CommandDone{CommandDone: &pb.CommandDone{Ok: true}}}, nil
		case "names-another":
			return &pb.PluginMsg{Msg: &pb.PluginMsg_CommandDone{CommandDone: &pb.CommandDone{CommandId: "other", Ok: false}}}, nil
		case "error":
			return nil, errors.New("refused")
		case "host-error":
			return nil, &HostError{Kind: pb.Error_INVALID_ARGUMENT, Message: "flag: missing"}
		default:
			panic("boom")
		}
	}}, nil)
	for _, command := range []string{"omits", "names-another", "error", "host-error", "panics"} {
		p.send(t, &pb.HostMsg{Id: command, Msg: &pb.HostMsg_InvokeCommand{InvokeCommand: &pb.InvokeCommand{CommandId: command}}})
		reply := p.recv(t)
		if reply.Id != command || reply.GetCommandDone() == nil || reply.GetCommandDone().CommandId != command {
			t.Fatalf("%s: %v", command, reply)
		}
	}
}

// A raw handler's UndeclaredCommand, wrapped or not, is Error{NOT_FOUND} --
// not a CommandDone.
func TestControlUndeclaredCommandIsNotFound(t *testing.T) {
	p := openPeer(t, Config{HandleRequest: func(ctx context.Context, c *Client, in *pb.HostMsg) (*pb.PluginMsg, error) {
		undeclared := &UndeclaredCommand{CommandID: in.GetInvokeCommand().GetCommandId()}
		if in.Id == "wrapped" {
			return nil, fmt.Errorf("routing: %w", undeclared)
		}
		return nil, undeclared
	}}, nil)
	for _, id := range []string{"bare", "wrapped"} {
		p.send(t, &pb.HostMsg{Id: id, Msg: &pb.HostMsg_InvokeCommand{InvokeCommand: &pb.InvokeCommand{CommandId: "nope"}}})
		reply := p.recv(t)
		if e := reply.GetError(); reply.Id != id || e == nil || e.Kind != pb.Error_NOT_FOUND || e.Message != `command "nope" is not one this plugin declares` {
			t.Fatalf("%s: %v", id, reply)
		}
	}
}
