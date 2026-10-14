package memory

import "errors"

// Standard purposes are defined by shared-memory §3. Producer and discipline
// still come from each region; a label never overrides its ownership rules.
const (
	PurposeReader  = "G"
	PurposeVolume  = "V"
	PurposeOverlay = "O"
	PurposePanel   = "P"
	PurposeExport  = "X"
	PurposeStage   = "S"
)

// Arena returns the segment's unique plugin-produced Loaned region. Region IDs
// are descriptive only. Metadata remains available while retiring so existing
// loans can be sliced or reclaimed. WriteLoan refuses new work on retiring arenas.
func (s *Segment) Arena() (Region, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for i := range s.regions {
		r := &s.regions[i]
		if r.layout.Discipline == Loaned && r.layout.Producer == Plugin {
			if _, err := s.region(r.layout.Index, true); err != nil {
				return Region{}, err
			}
			return cloneRegion(r.layout), nil
		}
	}
	return Region{}, errors.New("segment has no plugin-produced Loaned arena")
}

// LiveLane returns the unique plugin-produced Pulsed region, without using ID.
func (s *Segment) LiveLane() (Region, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for i := range s.regions {
		r := &s.regions[i]
		if r.layout.Discipline == Pulsed && r.layout.Producer == Plugin {
			if _, err := s.region(r.layout.Index, false); err != nil {
				return Region{}, err
			}
			return cloneRegion(r.layout), nil
		}
	}
	return Region{}, errors.New("segment has no plugin-produced Pulsed lane")
}
