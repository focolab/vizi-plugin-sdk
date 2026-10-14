package control

import (
	"errors"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/reflect/protoreflect"
)

var ErrDuplicateReservation = errors.New("one message names the same reservation more than once")

type reservationKey struct{ segment, offset, length uint64 }

// Outbound validation happens before admission: refusal consumes no new use.
// Earlier admitted uses remain the host's to release. HostMsg currently has
// at most one descriptor; this check is
// needed for plugin PanelPatch, OverlayUpdate and geometry-bearing requests.
func validateReservations(m *pb.PluginMsg) error {
	seen := make(map[reservationKey]bool)
	return walkMessages(m.ProtoReflect(), func(m protoreflect.Message) error {
		var key reservationKey
		switch d := m.Interface().(type) {
		case *pb.SegmentRef:
			key = reservationKey{d.SegmentId, d.Offset, d.Len}
		case *pb.ImageRef:
			key = reservationKey{d.SegmentId, d.Offset, d.Len}
		default:
			return nil
		}
		if seen[key] {
			return ErrDuplicateReservation
		}
		seen[key] = true
		return nil
	})
}

func walkMessages(m protoreflect.Message, visit func(protoreflect.Message) error) (err error) {
	if !m.IsValid() {
		return nil
	}
	if err = visit(m); err != nil {
		return err
	}
	m.Range(func(f protoreflect.FieldDescriptor, v protoreflect.Value) bool {
		if f.Message() == nil {
			return true
		}
		if f.IsMap() {
			if f.MapValue().Message() != nil {
				v.Map().Range(func(_ protoreflect.MapKey, x protoreflect.Value) bool {
					err = walkMessages(x.Message(), visit)
					return err == nil
				})
			}
		} else if f.IsList() {
			for i := 0; i < v.List().Len() && err == nil; i++ {
				err = walkMessages(v.List().Get(i).Message(), visit)
			}
		} else {
			err = walkMessages(v.Message(), visit)
		}
		return err == nil
	})
	return err
}

func (c *Client) validateOutbound(m *pb.PluginMsg) error {
	if err := c.validatePanelRows(m.GetPanelPatch()); err != nil {
		return err
	}
	if err := ValidateSize(m); err != nil {
		return err
	}
	if err := validateReservations(m); err != nil {
		c.queueMu.Lock()
		c.queueStats.Refused++
		c.queueMu.Unlock()
		return err
	}
	return nil
}

// AdmitOutbound counts sender uses at the queue's final admission point, after
// capacity and validation checks. Pair with SentOutbound in custom servers;
// ServeEnv installs both automatically. Never call it for speculative validation.
func (m *Memory) AdmitOutbound(msg *pb.PluginMsg) error {
	refs := outboundLoans(msg)
	if len(refs) == 0 {
		return nil
	}
	done, err := m.Registry.AdmitLoans(refs)
	if err != nil {
		return err
	}
	m.mu.Lock()
	if m.sends == nil {
		m.sends = make(map[*pb.PluginMsg][]func())
	}
	m.sends[msg] = append(m.sends[msg], done)
	m.mu.Unlock()
	return nil
}

// SentOutbound completes claims only after a successful transport send.
func (m *Memory) SentOutbound(msg *pb.PluginMsg) {
	m.mu.Lock()
	var done func()
	if pending := m.sends[msg]; len(pending) != 0 {
		done = pending[0]
		pending[0] = nil
		if len(pending) == 1 {
			delete(m.sends, msg)
		} else {
			m.sends[msg] = pending[1:]
		}
	}
	m.mu.Unlock()
	if done != nil {
		done()
	}
}

func outboundLoans(msg *pb.PluginMsg) []memory.LoanUse {
	var refs []memory.LoanUse
	_ = walkMessages(msg.ProtoReflect(), func(body protoreflect.Message) error {
		switch d := body.Interface().(type) {
		case *pb.SegmentRef:
			refs = append(refs, memory.LoanUse{SegmentID: d.SegmentId, Offset: d.Offset, Length: d.Len, Uses: d.Uses})
		case *pb.ImageRef:
			refs = append(refs, memory.LoanUse{SegmentID: d.SegmentId, Offset: d.Offset, Length: d.Len, Uses: 1})
		}
		return nil
	})
	return refs
}
