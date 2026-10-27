package control

import (
	"encoding/json"
	"errors"
	"google.golang.org/protobuf/reflect/protoreflect"
	"strings"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

func TestFaultPreservesAbsenceAndZeroFacts(t *testing.T) {
	zero := uint32(0)
	n := 0
	m, err := FaultMessage(&doorbell.Fault{Class: "bad_crc", ExpectedCRC: &zero, ObservedLength: &n}, FaultContext{})
	if err != nil {
		t.Fatal(err)
	}
	f := m.GetDoorbellFault()
	if f.ExpectedCrc == nil || *f.ExpectedCrc != 0 || f.ObservedCrc != nil || f.Ordinal != nil || f.ProcessHealthyAtDetection != nil || f.PluginId != nil {
		t.Fatal(f)
	}
	if _, err := FaultMessage(&doorbell.Fault{Class: "made_up"}, FaultContext{}); err == nil {
		t.Fatal("invented enum")
	}
}

func TestUnmappedFaultPreservesEvidenceWithoutInventingWireClass(t *testing.T) {
	id := "test.plugin"
	major := uint32(0)
	f := &doorbell.Fault{Class: "future_class", Detail: "original OS failure", Capture: []byte{0, 255, 17}, Transport: "seqpacket", Direction: "host->plugin"}
	m, err := FaultMessage(f, FaultContext{PluginID: &id, PeerMajor: &major})
	var unmapped *UnmappedFaultError
	if m != nil || !errors.As(err, &unmapped) {
		t.Fatalf("%v %v", m, err)
	}
	var recovered UnmappedFaultError
	_, data, ok := strings.Cut(err.Error(), "fault evidence: ")
	if !ok {
		t.Fatal(err)
	}
	if err := json.Unmarshal([]byte(data), &recovered); err != nil {
		t.Fatal(err)
	}
	if recovered.Fault.Class != f.Class || recovered.Fault.Detail != f.Detail || string(recovered.Fault.Capture) != string(f.Capture) || recovered.Context.PluginID == nil || *recovered.Context.PluginID != id || recovered.Context.PeerMajor == nil || *recovered.Context.PeerMajor != 0 || recovered.Context.PeerMinor != nil {
		t.Fatal(data)
	}
}

func TestFaultContextUsesOnlyObservedPeerVersion(t *testing.T) {
	c := stateClient(t)
	f := c.faultContext()
	if f.PeerMajor != nil || f.PeerMinor != nil {
		t.Fatal("unobserved version populated")
	}
	// Deliberately differ from SDK constants to test provenance independently
	// of today's major-version admission policy.
	c.peerHello = &pb.Hello{ProtocolMajor: 7, ProtocolMinor: 42}
	f = c.faultContext()
	if *f.PeerMajor != 7 || *f.PeerMinor != 42 {
		t.Fatal(f)
	}
	p := openPeer(t, Config{Handshake: func(h *pb.Hello) error {
		h.ProtocolMajor, h.ProtocolMinor = 8, 99
		return nil
	}}, nil)
	f = p.client.faultContext()
	if *f.PeerMajor != 1 || *f.PeerMinor != 0 {
		t.Fatal("handshake hook mutated stored observation", f)
	}
}

func TestFaultContextIncludesLocalHealthSnapshot(t *testing.T) {
	c := stateClient(t)
	c.cfg.PluginID = "test.plugin"
	for _, healthy := range []bool{true, false} {
		if !healthy {
			c.cancel(ErrClosed)
		}
		msg, err := FaultMessage(&doorbell.Fault{Class: "bad_crc"}, c.faultContext())
		if err != nil {
			t.Fatal(err)
		}
		wire, err := proto.Marshal(msg)
		if err != nil {
			t.Fatal(err)
		}
		out := &pb.PluginMsg{}
		if err := proto.Unmarshal(wire, out); err != nil {
			t.Fatal(err)
		}
		f := out.GetDoorbellFault()
		if f.SdkVersion == nil || *f.SdkVersion != SDKVersion || f.ProcessHealthyAtDetection == nil || !*f.ProcessHealthyAtDetection || f.StreamHealthyAtDetection == nil || *f.StreamHealthyAtDetection != healthy {
			t.Fatal("missing or incorrect local fault facts", f)
		}
		if f.ManifestSource != nil || f.GetPluginId() != "test.plugin" {
			t.Fatal("incorrect provenance", f)
		}
	}
}

func TestShortfallsMergeWantedAndCountWithoutStarvingKeys(t *testing.T) {
	s := NewShortfalls()
	for _, f := range []memory.Shortfall{{SegmentID: 1, Region: 0, WantedBytes: 64, LargestFreeBytes: 32}, {SegmentID: 2, Region: 0, WantedBytes: 128, LargestFreeBytes: 16}, {SegmentID: 1, Region: 0, WantedBytes: 256, LargestFreeBytes: 8}, {SegmentID: 1, Region: 0, WantedBytes: 128, LargestFreeBytes: 0}} {
		if err := s.Observe(f); err != nil {
			t.Fatal(err)
		}
	}
	m := s.next().GetSegmentShortfall()
	if m.SegmentId != 1 || m.WantedBytes != 256 || m.Dropped != 3 || m.LargestFreeBytes != 8 {
		t.Fatal(m)
	}
	if s.next().GetSegmentShortfall().SegmentId != 2 || s.next() != nil {
		t.Fatal("key lost or repeated")
	}
}

func TestFaultOptionalFieldsSurviveWireRoundTrip(t *testing.T) {
	for _, present := range []bool{false, true} {
		input := &doorbell.Fault{Class: "bad_crc"}
		if present {
			n := 0
			b := false
			input.Capture = []byte{}
			input.ObservedLength = &n
			input.ControlTruncated = &b
		}
		msg, err := FaultMessage(input, FaultContext{})
		if err != nil {
			t.Fatal(err)
		}
		wire, err := proto.Marshal(msg)
		if err != nil {
			t.Fatal(err)
		}
		out := &pb.PluginMsg{}
		if err := proto.Unmarshal(wire, out); err != nil {
			t.Fatal(err)
		}
		f := out.GetDoorbellFault()
		for _, field := range []protoreflect.Name{"capture", "observed_len", "msg_ctrunc"} {
			if f.ProtoReflect().Has(f.ProtoReflect().Descriptor().Fields().ByName(field)) != present {
				t.Fatal(field, present, f)
			}
		}
	}
}
