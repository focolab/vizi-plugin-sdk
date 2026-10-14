package control

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/violations"
)

// FaultContext contains only established supervisory facts. nil means unknown.
// Health is a local snapshot, not a guarantee that every plugin component works.
type FaultContext struct {
	PluginID, ManifestSource, SDKVersion *string
	PeerMajor, PeerMinor                 *uint32
	ProcessHealthy, StreamHealthy        *bool
}

// UnmappedFaultError retains the full evidence when the transport and wire enum
// disagree. No legal DoorbellFault class can be inferred in this case. The
// session supervisor surfaces this error; it must be retained by the launcher.
type UnmappedFaultError struct {
	Fault   *doorbell.Fault
	Context FaultContext
}

func (e *UnmappedFaultError) Error() string {
	data, _ := json.Marshal(e) // only concrete structs, scalars and byte slices
	return "unmapped doorbell failure class; fault evidence: " + string(data)
}

func (c *Client) faultContext() FaultContext {
	// This code is running in the plugin process. Stream health records only
	// whether the session context is still live when the supervisor detects
	// the fault; neither fact establishes the doorbell listener's health.
	version, processHealthy, streamHealthy := SDKVersion, true, c.ctx.Err() == nil
	facts := FaultContext{PluginID: &c.cfg.PluginID, SDKVersion: &version, ProcessHealthy: &processHealthy, StreamHealthy: &streamHealthy}
	if c.peerHello != nil {
		major, minor := c.peerHello.ProtocolMajor, c.peerHello.ProtocolMinor
		facts.PeerMajor, facts.PeerMinor = &major, &minor
	}
	return facts
}

func FaultMessage(f *doorbell.Fault, facts FaultContext) (*pb.PluginMsg, error) {
	if f == nil {
		return nil, fmt.Errorf("terminal has no framing fault")
	}
	name := "DOORBELL_FAILURE_" + strings.ToUpper(f.Class)
	value, ok := pb.DoorbellFailureClass_value[name]
	if !ok || value == 0 {
		// The IDL explicitly makes enum zero a protocol error. Preserve the
		// original evidence in the returned session error instead of inventing
		// a class or falling back to the invalid UNSPECIFIED.
		return nil, &UnmappedFaultError{Fault: f, Context: facts}
	}
	out := &pb.DoorbellFault{FailureClass: pb.DoorbellFailureClass(value), Capture: appendCapture(f.Capture), Ordinal: f.Ordinal, ExpectedCrc: f.ExpectedCRC, ObservedCrc: f.ObservedCRC, TransportClass: f.Transport, Direction: f.Direction, Detail: f.Detail, PluginId: facts.PluginID, ManifestSource: facts.ManifestSource, SdkVersion: facts.SDKVersion, PeerProtocolMajor: facts.PeerMajor, PeerProtocolMinor: facts.PeerMinor, ProcessHealthyAtDetection: facts.ProcessHealthy, StreamHealthyAtDetection: facts.StreamHealthy}
	if f.ObservedLength != nil {
		n := uint64(*f.ObservedLength)
		out.ObservedLen = &n
	}
	if f.ControlTruncated != nil {
		value := *f.ControlTruncated
		out.MsgCtrunc = &value
	}
	if f.OSError != nil {
		code := uint32(*f.OSError)
		out.OsError = &code
	}
	return &pb.PluginMsg{Msg: &pb.PluginMsg_DoorbellFault{DoorbellFault: out}}, nil
}

func ViolationMessage(r violations.Report) *pb.PluginMsg {
	out := &pb.ContractViolation{Where: pb.ViolationSite(r.Where), Detail: r.Detail, Count: r.Count}
	if r.Record != nil {
		out.Record = append([]byte(nil), r.Record[:]...)
	}
	return &pb.PluginMsg{Msg: &pb.PluginMsg_ContractViolation{ContractViolation: out}}
}

// ReportViolations drains one shared, rate-bounded reporter for the session.
// NewServer owns delivery for Config.Reporter; do not also call this on that
// reporter. Use this bridge only for a separately supervised reporter. Send admission failure is
// fatal to this delivery path, never swallowed or retried as duplicate reports.
func ReportViolations(ctx context.Context, c *Client, r *violations.Reporter) error {
	return reportViolations(ctx, c, r, nil)
}

func reportViolations(ctx context.Context, c *Client, r *violations.Reporter, admitted func(uint32)) error {
	for {
		report, err := r.Next(ctx)
		if err != nil {
			return err
		}
		if err := c.Send(ViolationMessage(report)); err != nil {
			return err
		}
		if admitted != nil {
			admitted(report.Count)
		}
	}
}

func appendCapture(b []byte) []byte {
	if b == nil {
		return nil
	}
	return append([]byte{}, b...)
}

func (c *Client) reportViolation(where violations.Site, detail string) error {
	if c.cfg.Reporter == nil {
		return fmt.Errorf("violation reporter is not configured: %s: %s", where, detail)
	}
	return c.cfg.Reporter.Observe(violations.Finding{Where: where, Detail: detail})
}
