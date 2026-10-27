package control

import (
	"testing"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func TestHostProtocolUnknownBeforeHello(t *testing.T) {
	if major, minor, ok := stateClient(t).HostProtocol(); ok || major != 0 || minor != 0 {
		t.Fatal("version known before Hello", major, minor, ok)
	}
	var c *Client
	if _, _, ok := c.HostProtocol(); ok {
		t.Fatal("nil client reported a version")
	}
}

func TestHostProtocolReportsHelloMinor(t *testing.T) {
	p := openPeer(t, Config{Handshake: func(h *pb.Hello) error {
		h.ProtocolMinor = 99 // the hook's copy; the observation must not change
		return nil
	}}, &pb.Hello{ProtocolMajor: 1, ProtocolMinor: 7})
	major, minor, ok := p.client.HostProtocol()
	if !ok || major != 1 || minor != 7 {
		t.Fatal(major, minor, ok)
	}
}
