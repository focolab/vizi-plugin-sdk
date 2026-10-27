package memory

import (
	"errors"
	"fmt"
)

// ErrRejectedPair means a copied pane header contains an invalid view or epoch.
// Only the pair is rejected; the copied payload remains usable (§2).
var ErrRejectedPair = errors.New("invalid durable pane pair")

// PaneEpoch is read from a durable publication, never inferred from D.
type PaneEpoch struct {
	ViewID uint8
	Epoch  uint32
}

// ReadEpoch decodes the arranged header from an ALREADY SAFELY COPIED range.
// offset is the copied range's offset within its ownership unit: a Pulsed slot,
// a Loaned deposit, or the region for Constant/Owned (shared-memory §2).
// Absent headers return nil. A short Loaned deposit carries no pair and remains
// usable. Other partial copies return ErrBounds without invalidating the payload.
// An invalid view or zero epoch returns an error wrapping ErrRejectedPair.
// Every error concerns only the annotation: retain the payload and diagnose the
// unavailable pair separately, rather than rejecting the data because of it.
// The caller compares Epoch with its acknowledged pane state before using data.
func (r Region) ReadEpoch(data []byte, offset uint64) (*PaneEpoch, error) {
	if r.EpochOffset == nil {
		return nil, nil
	}
	if *r.EpochOffset < offset || !inRange(*r.EpochOffset-offset, 8, uint64(len(data))) {
		if r.Discipline == Loaned {
			return nil, nil
		}
		return nil, ErrBounds
	}
	p := data[*r.EpochOffset-offset:]
	view, epoch := le.Uint32(p[:4]), le.Uint32(p[4:8])
	if view > 255 || epoch == 0 {
		return nil, fmt.Errorf("%w: view=%d epoch=%d", ErrRejectedPair, view, epoch)
	}
	return &PaneEpoch{uint8(view), epoch}, nil
}
