package control

import (
	"errors"
	"fmt"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/reflect/protoreflect"
)

// PublishLoan reserves bytes in the explicitly selected segment's arena and
// admits one push naming them. build returns a PanelPatch, OverlayUpdate,
// OverlayChunk or OverlayLabelTable with
// exactly one ImageRef/SegmentRef naming this deposit (uses 0 or 1). The supplied
// ref may be annotated with shape/dtype metadata. A successful admission transfers
// the loan to the host until ReleaseLoan; failure, including a build panic,
// reclaims it locally. Never send or retain the supplied descriptor yourself.
// A retiring arena selects its installed successor; without one, the push is
// quietly dropped (nil error) and Memory.DroppedPublications increments.
// Use PublishChunkLoan for solicited chunks, which defer without shortfalls.
//
// purpose must be P or O. Reader replies and multi-message uses have a different
// admission lifetime and use the explicit WriteLoan API, whose caller MUST
// reclaim a never-admitted loan on build/send failure. A partially admitted
// shared loan cannot be rolled back; see control/README.md. Pane-header placement
// in application encodings is still unspecified; this helper refuses such arenas.
func (s *Session) PublishLoan(segment uint64, purpose string, data []byte, build func(*pb.SegmentRef) (*pb.PluginMsg, error)) error {
	if s == nil || s.Client == nil {
		return errors.New("publication requires a session")
	}
	err := s.publishLoan(segment, purpose, data, build, false, s.Client.Send)
	if s.Memory != nil && s.Memory.DropRetiredPublication(err) {
		return nil
	}
	return err
}

// PublishChunkLoan is PublishLoan for solicited OverlayChunk labels. Arena
// exhaustion returns memory.ErrArenaFull without a shortfall or refusal count;
// the caller retains its answer and retries after Memory.LoanChanges or a new set.
func (s *Session) PublishChunkLoan(segment uint64, data []byte, build func(*pb.SegmentRef) (*pb.PluginMsg, error)) error {
	if s == nil || s.Client == nil {
		return errors.New("publication requires a session")
	}
	err := s.publishLoan(segment, memory.PurposeOverlay, data, build, true, s.Client.Send)
	if s.Memory != nil && s.Memory.DropRetiredPublication(err) {
		return nil
	}
	return err
}

func (s *Session) publishLoan(segment uint64, purpose string, data []byte, build func(*pb.SegmentRef) (*pb.PluginMsg, error), chunk bool, send func(*pb.PluginMsg) error) (err error) {
	if s == nil || s.Client == nil || s.Memory == nil || s.Memory.Registry == nil || (!chunk && s.Shortfalls == nil) || build == nil {
		return errors.New("publication requires a session and builder")
	}
	if purpose != memory.PurposePanel && purpose != memory.PurposeOverlay {
		return errors.New("push publication requires panel or overlay purpose")
	}
	seg, d, err := s.reservePublication(segment, purpose, data, chunk)
	if err != nil {
		return err
	}
	accepted := false
	defer func() {
		if p := recover(); p != nil {
			err = fmt.Errorf("publication builder panic: %v", p)
		}
		if !accepted {
			if releaseErr := seg.ReleaseLoan(d); releaseErr != nil && !errors.Is(releaseErr, memory.ErrRetired) {
				err = errors.Join(err, releaseErr)
			}
		}
	}()
	ref := &pb.SegmentRef{SegmentId: d.SegmentID, Offset: d.Offset, Len: d.Length, Uses: 1}
	msg, err := build(ref)
	if err != nil {
		return err
	}
	if msg == nil || (chunk && msg.GetOverlayChunk() == nil) || (purpose == memory.PurposePanel && msg.GetPanelPatch() == nil) || (purpose == memory.PurposeOverlay && msg.GetOverlayUpdate() == nil && msg.GetOverlayChunk() == nil && msg.GetOverlayLabelTable() == nil) {
		return errors.New("publication kind does not match segment purpose")
	}
	if (msg.GetOverlayChunk() != nil && msg.GetOverlayChunk().OverlayId != seg.Subject()) || (msg.GetOverlayLabelTable() != nil && msg.GetOverlayLabelTable().OverlayId != seg.Subject()) || (msg.GetPanelPatch() != nil && msg.GetPanelPatch().PanelId != seg.Subject()) || (msg.GetOverlayUpdate() != nil && msg.GetOverlayUpdate().OverlayId != seg.Subject()) {
		return errors.New("publication contribution does not match segment subject")
	}
	count := 0
	err = walkMessages(msg.ProtoReflect(), func(m protoreflect.Message) error {
		var key reservationKey
		switch x := m.Interface().(type) {
		case *pb.SegmentRef:
			if x.Uses > 1 {
				return errors.New("PublishLoan requires one message use")
			}
			key = reservationKey{x.SegmentId, x.Offset, x.Len}
		case *pb.ImageRef:
			key = reservationKey{x.SegmentId, x.Offset, x.Len}
		default:
			return nil
		}
		count++
		if key != (reservationKey{d.SegmentID, d.Offset, d.Length}) {
			return errors.New("publication names a different reservation")
		}
		return nil
	})
	if err != nil {
		return err
	}
	if count != 1 {
		return errors.New("publication must name its reservation exactly once")
	}
	if err = send(msg); err != nil {
		return err
	}
	accepted = true
	return nil
}

// Retry only if retirement actually installed a different successor. No builder
// has run and no reservation was acquired on a failed attempt.
func (s *Session) reservePublication(segment uint64, purpose string, data []byte, chunk bool) (*memory.Segment, memory.Deposit, error) {
	for {
		seg, err := s.Memory.Registry.PublicationSegment(segment)
		if err != nil {
			return nil, memory.Deposit{}, err
		}
		if seg.Purpose() != purpose {
			return nil, memory.Deposit{}, fmt.Errorf("segment purpose %q, expected %q", seg.Purpose(), purpose)
		}
		arena, err := seg.Arena()
		var d memory.Deposit
		if err == nil {
			if arena.EpochOffset != nil {
				return nil, d, errors.New("application encoding of pane-associated arena requires an explicit binding")
			}
			if chunk {
				d, err = seg.TryWriteLoan(arena.Index, data)
			} else {
				d, err = s.WriteLoan(seg.ID(), arena.Index, data)
			}
		}
		if !errors.Is(err, memory.ErrRetired) {
			return seg, d, err
		}
		next, lookupErr := s.Memory.Registry.PublicationSegment(seg.ID())
		if lookupErr != nil || next.ID() == seg.ID() {
			return nil, d, err
		}
		segment = next.ID()
	}
}
