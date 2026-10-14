package control

import (
	"fmt"
	"io"
	"strings"
	"testing"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

func padMessage(t *testing.T, m proto.Message, size int) {
	t.Helper()
	// Unknown fields are preserved and count toward the whole-message bound.
	// Padding the envelope isolates this limit from known per-field limits.
	const field = 1000
	m.ProtoReflect().SetUnknown(nil)
	n := size - proto.Size(m) - protowire.SizeTag(field) - protowire.SizeVarint(uint64(size))
	wire := protowire.AppendTag(nil, field, protowire.BytesType)
	wire = protowire.AppendBytes(wire, make([]byte, n))
	m.ProtoReflect().SetUnknown(wire)
	if got := proto.Size(m); got != size {
		t.Fatalf("fixture size = %d, want %d", got, size)
	}
}

func TestMessageCeilingBothDirections(t *testing.T) {
	// Pin the specification independently of the implementation constant.
	const ceiling = 32 << 20
	p := openPeer(t, Config{Arrange: func(c *Client, m *pb.HostMsg) error {
		return c.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_ToolBindingAck{ToolBindingAck: &pb.ToolBindingAck{Tool: m.GetToolBinding().Tool, Revision: 1}}})
	}}, nil, grpc.MaxCallRecvMsgSize(64<<20), grpc.MaxCallSendMsgSize(64<<20))
	// The peer's larger limits ensure a refusal comes from the SDK.
	in := &pb.HostMsg{Msg: &pb.HostMsg_ToolBinding{ToolBinding: &pb.ToolBinding{Tool: 7, Revision: 1}}}
	padMessage(t, in, ceiling)
	p.send(t, in)
	if ack := p.recv(t).GetToolBindingAck(); ack.GetTool() != 7 {
		t.Fatal("message at receive ceiling did not reach dispatch")
	}
	out := &pb.PluginMsg{Msg: &pb.PluginMsg_OverlayRemove{OverlayRemove: &pb.OverlayRemove{OverlayId: "o", Revision: 1}}}
	padMessage(t, out, ceiling)
	if err := p.client.Send(out); err != nil {
		t.Fatal("message at send ceiling refused", err)
	}
	if got := p.recv(t); !proto.Equal(got, out) {
		t.Fatal("message at send ceiling was changed")
	}
	padMessage(t, out, ceiling+1)
	if err := p.client.Send(out); err == nil || !strings.Contains(err.Error(), fmt.Sprint(ceiling+1)) || !strings.Contains(err.Error(), fmt.Sprint(ceiling)) {
		t.Fatal("oversized send must be refused with actual and maximum sizes", err)
	}
	if err := p.client.Context().Err(); err != nil {
		t.Fatal("local send refusal killed the session", err)
	}
	padMessage(t, in, ceiling+1)
	if err := p.stream.Send(in); err != nil && err != io.EOF {
		t.Fatal(err)
	}
	_, err := p.stream.Recv()
	if status.Code(err) != codes.ResourceExhausted || !strings.Contains(err.Error(), fmt.Sprint(ceiling+1)) || !strings.Contains(err.Error(), fmt.Sprint(ceiling)) {
		t.Fatal("oversized receive must fault with actual and maximum sizes", err)
	}
}
