package memory

import (
	"errors"
	"testing"
)

func TestFindPurposeSelectsOnlyAnUnambiguousActiveGrant(t *testing.T) {
	r, err := New(Config{})
	if err != nil {
		t.Fatal(err)
	}
	a := &Segment{id: 1, purpose: "G"}
	b := &Segment{id: 2, purpose: "G"}
	r.entries[1] = &entry{state: Installed, segment: a}
	r.entries[2] = &entry{state: Pending, segment: b}
	if got, err := r.FindPurpose("G"); err != nil || got != a {
		t.Fatal(got, err)
	}
	r.entries[2].state = Installed
	if _, err := r.FindPurpose("G"); err == nil {
		t.Fatal("ambiguous grants accepted")
	}
	a.retiring.Store(true)
	if got, err := r.FindPurpose("G"); err != nil || got != b {
		t.Fatal(got, err)
	}
	r.entries[2].state = Released
	if _, err := r.FindPurpose("G"); !errors.Is(err, ErrNotInstalled) {
		t.Fatal(err)
	}
	for _, purpose := range []string{"", "O", "P", "arbitrary"} {
		if _, err := r.FindPurpose(purpose); err == nil {
			t.Fatal("accepted purpose", purpose)
		}
	}
}
