package control

import (
	"errors"
	"fmt"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
)

// PublishLiveOverlay writes a serialized OverlayUpdate directly into the selected
// O segment's unique lane (§3.3). The caller owns the overlay revision sequence
// across live, reliable and remove messages; Sample.Revision is a separate slot
// counter. A zero sample means coalesced or quietly dropped after retirement. Once Sample is nonzero the publication
// committed, even if the subsequent advisory notification returns an error.
// Use the plugin.Overlay helper to manage the shared overlay revision sequence.
func (s *Session) PublishLiveOverlay(segment uint64, update *pb.OverlayUpdate, pair *memory.PaneEpoch) (memory.Sample, error) {
	sample, err := s.publishLiveOverlay(segment, update, pair)
	if s != nil && s.Memory != nil && s.Memory.DropRetiredPublication(err) {
		return memory.Sample{}, nil
	}
	return sample, err
}

func (s *Session) publishLiveOverlay(segment uint64, update *pb.OverlayUpdate, pair *memory.PaneEpoch) (sample memory.Sample, err error) {
	if s == nil || s.Client == nil || s.Memory == nil || s.Memory.Registry == nil {
		return memory.Sample{}, errors.New("live publication requires a session")
	}
	s.Client.mu.Lock()
	closing := s.Client.closing
	s.Client.mu.Unlock()
	if closing {
		return memory.Sample{}, ErrShuttingDown
	}
	if err := s.Client.Context().Err(); err != nil {
		return memory.Sample{}, err
	}
	seg, err := s.Memory.Registry.PublicationSegment(segment)
	if err != nil {
		return memory.Sample{}, err
	}
	defer func() {
		if errors.Is(err, memory.ErrRetired) {
			next, lookupErr := s.Memory.Registry.PublicationSegment(seg.ID())
			if lookupErr == nil && next.ID() != seg.ID() {
				sample, err = s.publishLiveOverlay(next.ID(), update, pair)
			}
		}
	}()
	if seg.Purpose() != memory.PurposeOverlay {
		return memory.Sample{}, errors.New("live overlay requires O segment")
	}
	if err := ValidateLiveOverlay(update, seg.Subject()); err != nil {
		return memory.Sample{}, err
	}
	lane, err := seg.LiveLane()
	if err != nil {
		return memory.Sample{}, err
	}
	data, err := proto.Marshal(update)
	if err != nil {
		return memory.Sample{}, err
	}
	if uint64(len(data)) > lane.PayloadCapacity() {
		return memory.Sample{}, fmt.Errorf("live overlay needs %d bytes; granted payload capacity is %d bytes: %w", len(data), lane.PayloadCapacity(), memory.ErrBounds)
	}
	sample, err = seg.PublishPulsedEpoch(lane.Index, data, pair)
	if err != nil || sample.Revision == 0 || s.Doorbell == nil {
		return sample, err
	}
	var mask doorbell.Mask
	if lane.Index < uint32(len(mask)*8) {
		mask[lane.Index/8] |= 1 << (lane.Index % 8)
	} else {
		return sample, nil
	}
	hint := doorbell.Notification{SegmentID: seg.ID(), Mask: mask}
	if pair != nil {
		hint.ViewID, hint.Epoch = pair.ViewID, pair.Epoch
	}
	_, err = s.Doorbell.Notify(hint)
	return sample, err
}

// ValidateLiveOverlay checks the two live-lane application invariants. Sender
// failures are local errors, not reports against the host. Receiver integrations
// may inspect the returned memory.Violation and submit it to their reporter.
func ValidateLiveOverlay(update *pb.OverlayUpdate, subject string) error {
	if update == nil {
		return errors.New("missing live OverlayUpdate")
	}
	if subject == "" || update.OverlayId != subject {
		return &memory.Violation{Where: "lane_subject_mismatch", Detail: fmt.Sprintf("overlay %q does not match subject %q", update.OverlayId, subject)}
	}
	if err := ValidateSize(update); err != nil {
		return err
	}
	return walkMessages(update.ProtoReflect(), func(m protoreflect.Message) error {
		if b, ok := m.Interface().(*pb.GeomBuffer); ok {
			if _, inline := b.Source.(*pb.GeomBuffer_Inline); !inline {
				return &memory.Violation{Where: "live_geometry_not_inline", Detail: "live GeomBuffer must carry inline bytes"}
			}
		}
		return nil
	})
}
