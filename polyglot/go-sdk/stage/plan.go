// Package stage provides processing-stage planning and slot-layout primitives.
// Control integrates these with borrowed turns on installed shared-memory rings.
// The layout primitives alone do not confer slot ownership.
package stage

import (
	"errors"
	"fmt"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

const MaxChannels = 65536

// ErrProtocolPlan marks a wire channel/count bound, as distinct from an author's
// invalid plan (for example, spatial growth or a missing dependency).
var ErrProtocolPlan = errors.New("stage plan violates a protocol bound")

// PlanErrorKind distinguishes §4's explicit PROTOCOL bound failures from
// ordinary author plan errors. ValidateShape failures on a host request are
// always PROTOCOL; this function applies to ValidatePlan's reply validation.
func PlanErrorKind(err error) pb.Error_Kind {
	if errors.Is(err, ErrProtocolPlan) {
		return pb.Error_PROTOCOL
	}
	return pb.Error_INTERNAL
}

// ItemSize returns the byte width of a known wire dtype, or zero if unsupported.
func ItemSize(dtype pb.DType) uint64 {
	switch dtype {
	case pb.DType_DTYPE_U8:
		return 1
	case pb.DType_DTYPE_U16:
		return 2
	case pb.DType_DTYPE_F32:
		return 4
	case pb.DType_DTYPE_F64:
		return 8
	default:
		return 0
	}
}

func indices(channels []uint32) error {
	if len(channels) > MaxChannels {
		return fmt.Errorf("%w: stage has more than %d channel indices", ErrProtocolPlan, MaxChannels)
	}
	for _, c := range channels {
		if c >= MaxChannels {
			return fmt.Errorf("%w: stage channel index %d exceeds 65535", ErrProtocolPlan, c)
		}
	}
	return nil
}

func ValidateShape(s *pb.StageShape) error {
	if s == nil {
		return fmt.Errorf("missing stage shape")
	}
	if ItemSize(s.Dtype) == 0 {
		return fmt.Errorf("unknown stage dtype %d", s.Dtype)
	}
	return indices(s.Channels)
}

// ValidatePlan checks §3's output and dependencies. Output channels are not
// used to compute the result: the host assigns additions, as §4 requires.
func ValidatePlan(input *pb.StageShape, plan *pb.StagePlanned) error {
	if err := ValidateShape(input); err != nil {
		return err
	}
	if plan == nil {
		return fmt.Errorf("missing stage plan")
	}
	if err := ValidateShape(plan.Output); err != nil {
		return err
	}
	o := plan.Output
	if o.Z == 0 || o.Y == 0 || o.X == 0 {
		return fmt.Errorf("stage output axes must be at least 1")
	}
	if o.Z > input.Z || o.Y > input.Y || o.X > input.X {
		return fmt.Errorf("stage output grows a spatial dimension")
	}
	if len(plan.Modifies) > MaxChannels || len(plan.Adds) > MaxChannels {
		return fmt.Errorf("%w: too many stage channel declarations", ErrProtocolPlan)
	}
	available := make(map[uint32]bool, len(input.Channels))
	for _, c := range input.Channels {
		available[c] = true
	}
	needs := func(channels []uint32) error {
		if err := indices(channels); err != nil {
			return err
		}
		for _, c := range channels {
			if !available[c] {
				return fmt.Errorf("stage requires channel %d absent from its planned input", c)
			}
		}
		return nil
	}
	if err := needs(plan.Reads); err != nil {
		return err
	}
	for _, m := range plan.Modifies {
		if m == nil {
			return fmt.Errorf("missing modified channel declaration")
		}
		if err := needs([]uint32{m.Channel}); err != nil {
			return err
		}
		if err := needs(m.Needs); err != nil {
			return err
		}
	}
	keys := map[string]bool{}
	for _, a := range plan.Adds {
		if a == nil || a.Key == "" {
			return fmt.Errorf("added channel needs a nonempty key")
		}
		if keys[a.Key] {
			return fmt.Errorf("duplicate added channel key %q", a.Key)
		}
		keys[a.Key] = true
		if err := needs(a.Needs); err != nil {
			return err
		}
	}
	return nil
}
