//go:build (linux || darwin || windows) && (amd64 || arm64)

package memory

import (
	"bytes"
	"errors"
	"testing"
)

func TestSliceUnpublishedLoanKeepsAlignmentAndReleaseIdentity(t *testing.T) {
	s := producerSegment()
	original := []byte{0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11}
	key, err := s.WriteLoan(1, original)
	if err != nil {
		t.Fatal(err)
	}
	for _, bad := range []struct{ offset, length uint64 }{{0, 0}, {11, 2}, {^uint64(0), 4}} {
		if _, err := s.SliceLoan(key, bad.offset, bad.length); !errors.Is(err, ErrBounds) {
			t.Fatal(err)
		}
		if !bytes.Equal(s.data[key.Offset:key.Offset+key.Length], original) {
			t.Fatal("failed slice mutated bytes")
		}
	}
	next, err := s.SliceLoan(key, 4, 4)
	if err != nil {
		t.Fatal(err)
	}
	if next.Offset != key.Offset || next.Offset%64 != 0 || next.Length != 4 || !bytes.Equal(s.data[next.Offset:next.Offset+4], []byte{4, 5, 6, 7}) {
		t.Fatal(next)
	}
	registry := &Registry{entries: map[uint64]*entry{s.id: {state: Installed, segment: s}}}
	if err := registry.NameLoans([]LoanUse{{s.id, key.Offset, key.Length, 1}}); err == nil {
		t.Fatal("old key still admitted")
	}
	if err := registry.NameLoans([]LoanUse{{s.id, next.Offset, next.Length, 1}}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.SliceLoan(next, 0, 2); !errors.Is(err, ErrLoanAdmitted) {
		t.Fatal(err)
	}
	if !bytes.Equal(s.data[next.Offset:next.Offset+4], []byte{4, 5, 6, 7}) {
		t.Fatal("admitted loan changed")
	}
	if err := s.ApplyHostRelease(next); err != nil {
		t.Fatal(err)
	}
	if stats, _ := s.LoanStats(1); stats.Outstanding != 0 || stats.Cursor != 0 {
		t.Fatal(stats)
	}
}
