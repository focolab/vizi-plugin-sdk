package control

import (
	"errors"
	"strings"
	"testing"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func TestReadyIsClampedToTheHostsMinor(t *testing.T) {
	for _, host := range []uint32{0, 1, 99} {
		if got := ReadyMinor(host); got != min(host, uint32(ProtocolMinor)) {
			t.Fatalf("host 1.%d: Ready minor %d", host, got)
		}
		// openPeer checks the Ready on the wire says 1.0.
		p := openPeer(t, Config{}, &pb.Hello{ProtocolMajor: 1, ProtocolMinor: host})
		major, minor, ok := p.client.SessionProtocol()
		if !ok || major != 1 || minor != 0 {
			t.Fatalf("host 1.%d: session %d.%d %v", host, major, minor, ok)
		}
		if _, hostMinor, _ := p.client.HostProtocol(); hostMinor != host {
			t.Fatalf("host minor %d, Hello said %d", hostMinor, host)
		}
	}
}

func TestReadyBelowTheManifestsEnginesIsRefused(t *testing.T) {
	// A "reject" token tells openPeer the handshake is expected to fail.
	p := openPeer(t, Config{Engines: "1.2"}, &pb.Hello{ProtocolMajor: 1, ProtocolMinor: 5, SessionToken: "reject"})
	_, err := p.stream.Recv()
	if err == nil || !strings.Contains(err.Error(), "this plugin's manifest requires protocol 1.2 but its code speaks 1.0") {
		t.Fatalf("%v", err)
	}
	if err := checkEngines("^1.0", 0); err != nil {
		t.Fatal(err)
	}
	if _, err := NewServer(Config{PluginID: "x", Handshake: func(*pb.Hello) error { return nil }, Engines: "one"}); err == nil {
		t.Fatal("an unparsable Engines was accepted")
	}
}

func TestSupportsAgainstTheNegotiatedMinor(t *testing.T) {
	var none *Client
	if none.Supports(FeatureSetView) {
		t.Fatal("supported before the handshake")
	}
	p := openPeer(t, Config{}, &pb.Hello{ProtocolMajor: 1, ProtocolMinor: 3})
	for _, f := range []string{FeatureViewSubscription, FeatureRegionsSubscription, FeatureRawRead, FeatureProcessingStages, FeatureRegionShapers, FeatureSetView, FeatureFollow} {
		if !p.client.Supports(f) || p.client.Require(f) != nil {
			t.Fatalf("1.0 feature %q unsupported", f)
		}
	}
	err := p.client.Require("telepathy")
	var unavailable *FeatureUnavailableError
	if !errors.As(err, &unavailable) || unavailable.Known || !strings.Contains(err.Error(), `"telepathy" is not one this SDK knows`) {
		t.Fatalf("%v", err)
	}
	// A feature newer than the session names both minors at the call site.
	features["test_future"] = 1
	t.Cleanup(func() { delete(features, "test_future") })
	err = p.client.Require("test_future")
	if p.client.Supports("test_future") || err == nil || err.Error() != `feature "test_future" needs protocol 1.1; this session speaks 1.0` {
		t.Fatalf("%v", err)
	}
}
