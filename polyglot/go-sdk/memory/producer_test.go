//go:build (linux || darwin || windows) && (amd64 || arm64)

package memory

import (
	"bytes"
	"errors"
	"reflect"
	"sync"
	"testing"
)

func producerSegment() *Segment {
	return &Segment{id: 81, data: make([]byte, 16384), installed: true, regions: []regionState{
		{layout: Region{Index: 0, Offset: 32, Extent: 3 * (16 + 64), Discipline: Pulsed, Producer: Plugin, Buffers: 3, SlotBytes: 64}},
		{layout: Region{Index: 1, Offset: 512, Extent: 8192, Discipline: Loaned, Producer: Plugin, SlotBytes: 64}},
		{layout: Region{Index: 2, Offset: 9000, Extent: 512, Discipline: Loaned, Producer: Host}},
	}}
}

func TestEpochHeaderMustFitActualPublicationAndDeposit(t *testing.T) {
	s := producerSegment()
	offset := uint64(56)
	s.regions[0].layout.EpochOffset = &offset
	if _, err := s.PublishPulsed(0, make([]byte, 63)); !errors.Is(err, ErrBounds) {
		t.Fatal(err)
	}
	if stats, _ := s.PublishingStats(0); stats.LastRevision != 0 {
		t.Fatal("rejected payload consumed a revision")
	}
	if _, err := s.PublishPulsedEpoch(0, make([]byte, 56), &PaneEpoch{ViewID: 2, Epoch: 9}); err != nil {
		t.Fatal(err)
	}
	// The first rotated slot is 1. Simulate a peer advertising fewer bytes than
	// the arranged header; consuming must release the claim and isolate it.
	s.regions[0].layout.Producer = Host
	le.PutUint64(s.data[56:64], 63)
	_, err := s.ReadPulsed(0, make([]byte, 64))
	var violation *Violation
	if !errors.As(err, &violation) || violation.Where != "invalid_published_length" {
		t.Fatal(err)
	}
	if loadState(s.data, 48) != 0 {
		t.Fatal("invalid publication left a read claim held")
	}
	zero := uint64(0)
	s.regions[1].layout.EpochOffset = &zero
	if _, err := s.WriteLoan(1, make([]byte, 7)); err != nil {
		t.Fatal(err)
	}
	s.regions[2].layout.EpochOffset = &zero
	read, err := s.AcceptDeposit(Deposit{SegmentID: 81, Region: 2, Offset: 9000, Length: 7})
	if err != nil {
		t.Fatal(err)
	}
	copied := make([]byte, 7)
	if _, err := read.Copy(copied); err != nil {
		t.Fatal("short deposit was not delivered", err)
	}
	if pair, err := s.regions[2].layout.ReadEpoch(copied, 0); pair != nil || err != nil {
		t.Fatal(pair, err)
	}
	if _, err := s.AcceptDeposit(Deposit{SegmentID: 81, Region: 2, Offset: 9000, Length: 8}); err != nil {
		t.Fatal(err)
	}
}

func TestLoanShortfallCapturedAtAllocation(t *testing.T) {
	s := producerSegment()
	_, shortfall, err := s.WriteLoanReport(1, make([]byte, 8193))
	if !errors.Is(err, ErrArenaFull) || shortfall == nil || shortfall.WantedBytes != 8193 || shortfall.LargestFreeBytes != 8192 {
		t.Fatalf("%+v %v", shortfall, err)
	}
	if stats, _ := s.LoanStats(1); stats.Refused != 1 || stats.Outstanding != 0 {
		t.Fatal(stats)
	}
}

func TestPublishCandidateOrder(t *testing.T) {
	for _, tc := range []struct {
		name   string
		words  [3]uint64
		last   uint64
		chosen int
	}{
		{"empty rotation", [3]uint64{}, 0, 1},
		{"oldest ready", [3]uint64{7, 3, 6}, 7, 1},
		{"empty before ready", [3]uint64{9, 8, 0}, 9, 2},
		{"newest excluded", [3]uint64{9, readClaim | 8, writeClaim | 7}, 9, -1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := producerSegment()
			s.regions[0].publishing.LastRevision = tc.last
			for i, word := range tc.words {
				storeState(s.data, 32+uint64(i)*16, word)
			}
			p, err := s.PublishPulsed(0, []byte("hello"))
			if err != nil {
				t.Fatal(err)
			}
			for i, word := range tc.words {
				want := word
				if i == tc.chosen {
					want = tc.last + 1
				}
				if got := loadState(s.data, 32+uint64(i)*16); got != want {
					t.Fatalf("slot %d: %x, want %x", i, got, want)
				}
			}
			stats, _ := s.PublishingStats(0)
			if tc.chosen < 0 {
				if p != (Sample{}) || stats.Coalesced != 1 {
					t.Fatal(p, stats)
				}
			} else {
				i := uint64(tc.chosen)
				if p != (Sample{tc.last + 1, 5}) || le.Uint64(s.data[40+i*16:48+i*16]) != 5 || string(s.data[80+i*64:85+i*64]) != "hello" {
					t.Fatal(p)
				}
			}
		})
	}
}

func TestPublishEightAttemptsBurnRevisionAndPreserveNewest(t *testing.T) {
	s := producerSegment()
	var slots []uint64
	s.cas = func(_ []byte, offset, old, next uint64) bool { slots = append(slots, offset); return false }
	p, err := s.PublishPulsed(0, []byte("lost"))
	if err != nil || p != (Sample{}) || len(slots) != 24 {
		t.Fatal(p, err, slots)
	}
	if !reflect.DeepEqual(slots[:3], []uint64{48, 64, 32}) {
		t.Fatal(slots)
	}
	s.cas = nil
	p, err = s.PublishPulsed(0, []byte("next"))
	if err != nil || p.Revision != 2 {
		t.Fatal(p, err)
	}
	stats, _ := s.PublishingStats(0)
	if stats != (PublishStats{LastRevision: 2, Published: 1, Coalesced: 1}) {
		t.Fatal(stats)
	}
}

func TestPublishRollbackAndInterference(t *testing.T) {
	for _, interference := range []bool{false, true} {
		s := producerSegment()
		local := errors.New("copy failed")
		s.publishCopy = func(_, _ []byte) error {
			if got := loadState(s.data, 48); got != writeClaim|1 {
				t.Fatalf("copy without claim: %x", got)
			}
			if interference {
				storeState(s.data, 48, readClaim|99)
			}
			return local
		}
		_, err := s.PublishPulsed(0, []byte("fail"))
		if !errors.Is(err, local) {
			t.Fatal(err)
		}
		var v *Violation
		if interference {
			if !errors.As(err, &v) || v.Where != "release_cas_failed" || loadState(s.data, 48) != readClaim|99 {
				t.Fatal(err)
			}
			if _, err = s.PublishPulsed(0, []byte("retry")); err != ErrQuarantined {
				t.Fatal(err)
			}
		} else {
			if errors.As(err, &v) || loadState(s.data, 48) != 0 {
				t.Fatal(err)
			}
			s.publishCopy = nil
			if p, err := s.PublishPulsed(0, []byte("retry")); err != nil || p.Revision != 2 {
				t.Fatal(p, err)
			}
		}
	}
}

func TestPublishInvalidAndExhausted(t *testing.T) {
	s := producerSegment()
	for _, data := range [][]byte{nil, make([]byte, 65)} {
		if _, err := s.PublishPulsed(0, data); err != ErrBounds {
			t.Fatal(err)
		}
	}
	s.regions[0].publishing.LastRevision = revisionMask - 1
	p, err := s.PublishPulsed(0, []byte("last"))
	if err != nil || p.Revision != revisionMask {
		t.Fatal(p, err)
	}
	if _, err = s.PublishPulsed(0, []byte("wrap")); err != ErrRevisionExhausted {
		t.Fatal(err)
	}
	if s.regions[0].quarantined {
		t.Fatal("exhaustion quarantined durable value")
	}
	s.regions[0].layout.Producer = Host // test peer consumes the final READY value
	buf := make([]byte, 64)
	if p, err := s.ReadPulsed(0, buf); err != nil || p.Revision != revisionMask || string(buf[:p.Length]) != "last" {
		t.Fatal(p, err)
	}
	for _, word := range []uint64{writeClaim, readClaim, writeClaim | readClaim | 3} {
		s = producerSegment()
		storeState(s.data, 32, word)
		_, err := s.PublishPulsed(0, []byte("bad"))
		var v *Violation
		if !errors.As(err, &v) || v.Where != "impossible_state_word" || loadState(s.data, 32) != word {
			t.Fatal(err)
		}
	}
}

func TestLoanPaddingFullAndExactRelease(t *testing.T) {
	s := producerSegment()
	s.regions[1].layout.Extent = 192
	// §7: the key carries the MAPPING-relative offset the descriptor will, so
	// the arena's first reservation is region 1's own offset and not zero.
	a, err := s.WriteLoan(1, bytes.Repeat([]byte{1}, 65))
	if err != nil || a != (Deposit{81, 1, 512, 65}) {
		t.Fatal(a, err)
	}
	b, err := s.WriteLoan(1, []byte{2})
	if err != nil || b.Offset != 512+128 || b.Length != 1 {
		t.Fatal(b, err)
	}
	if err := s.ReleaseLoan(b); err != nil {
		t.Fatal(err)
	}
	if _, err := s.WriteLoan(1, []byte{3}); err != ErrArenaFull {
		t.Fatal(err)
	}
	if !bytes.Equal(s.data[512:577], bytes.Repeat([]byte{1}, 65)) {
		t.Fatal("overwrote outstanding loan")
	}
	if err := s.ReleaseLoan(a); err != nil {
		t.Fatal(err)
	}
	c, err := s.WriteLoan(1, []byte{3})
	if err != nil || c.Offset != 512 {
		t.Fatal(c, err)
	}
	stats, _ := s.LoanStats(1)
	if stats != (LoanStats{64, 1, 1}) {
		t.Fatal(stats)
	}
	bad := c
	bad.Length = 64
	err = s.ReleaseLoan(bad)
	var v *Violation
	if !errors.As(err, &v) || v.Where != "unknown_release" {
		t.Fatal(err)
	}
	if _, err = s.WriteLoan(1, []byte{4}); err != ErrQuarantined {
		t.Fatal(err)
	}
	stats, _ = s.LoanStats(1)
	if stats.Outstanding != 1 {
		t.Fatal("invalid release freed bytes")
	}
}

// §8.12: `region` is redundant with `offset` so that a mistargeting is DETECTED
// rather than applied. The forgotten conversion this direction produces lands
// outside the region named, which is the case this check exists for.
func TestReleaseOutsideTheRegionItNamesIsAMismatchNotAnUnknownRelease(t *testing.T) {
	s := producerSegment()
	key, err := s.WriteLoan(1, []byte{1})
	if err != nil {
		t.Fatal(err)
	}
	stale := key
	stale.Offset -= s.regions[1].layout.Offset // as a region-relative sender would send it
	var v *Violation
	if err := s.ReleaseLoan(stale); !errors.As(err, &v) || v.Where != "release_region_mismatch" {
		t.Fatal(err)
	}
	if stats, _ := s.LoanStats(1); stats.Outstanding != 1 {
		t.Fatal("a mistargeted release freed bytes")
	}
}

func TestLoanWritersShareCursorAndWrongSideIsolated(t *testing.T) {
	s := producerSegment()
	keys := make(chan Deposit, 100)
	var wg sync.WaitGroup
	for i := 0; i < 100; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			key, err := s.WriteLoan(1, []byte{1})
			if err != nil {
				t.Error(err)
				return
			}
			keys <- key
		}()
	}
	wg.Wait()
	close(keys)
	seen := map[uint64]bool{}
	for key := range keys {
		if seen[key.Offset] {
			t.Fatal("overlapping allocations")
		}
		seen[key.Offset] = true
	}
	if len(seen) != 100 {
		t.Fatal(len(seen))
	}
	var v *Violation
	if err := s.ReleaseLoan(Deposit{81, 2, 0, 1}); !errors.As(err, &v) || v.Where != "wrong_producer_release" {
		t.Fatal(err)
	}
	if s.regions[1].quarantined || !s.regions[2].quarantined {
		t.Fatal("wrong isolation scope")
	}
	s.retiring.Store(true)
	if _, err := s.WriteLoan(1, []byte{1}); err != ErrRetired {
		t.Fatal(err)
	}
	if err := s.ReleaseLoan(Deposit{81, 1, 512, 1}); err != nil {
		t.Fatal("retirement must allow finishing releases", err)
	}
	if _, err := s.PublishPulsed(0, []byte{1}); err != ErrRetired {
		t.Fatal(err)
	}
}

func TestLoanAlignmentAlways64RegardlessOfReservedField(t *testing.T) {
	for _, alignment := range []uint64{0, 1, 64, 128} {
		s := producerSegment()
		s.regions[1].layout.SlotBytes = alignment
		first, err := s.WriteLoan(1, []byte{1})
		if err != nil {
			t.Fatal(err)
		}
		second, err := s.WriteLoan(1, []byte{2})
		if err != nil {
			t.Fatal(err)
		}
		if second.Offset-first.Offset != 64 || first.Length != 1 {
			t.Fatal(first, second)
		}
		if err := s.ReleaseLoan(first); err != nil {
			t.Fatal(err)
		}
		if err := s.ReleaseLoan(second); err != nil {
			t.Fatal(err)
		}
		again, err := s.WriteLoan(1, []byte{3})
		if err != nil || again.Offset != first.Offset {
			t.Fatal(again, err)
		}
	}
}

func TestPulsedTailHeaderAndPayloadAreOnePublication(t *testing.T) {
	s := producerSegment()
	tail := uint64(56)
	s.regions[0].layout.EpochOffset = &tail
	payload := []byte("short payload")
	pair := &PaneEpoch{ViewID: 5, Epoch: 31}
	if _, err := s.PublishPulsed(0, payload); err == nil {
		t.Fatal("pane-associated publish without pair accepted")
	}
	sample, err := s.PublishPulsedEpoch(0, payload, pair)
	if err != nil || sample.Length != len(payload) {
		t.Fatal(sample, err)
	}
	// Independently inspect the chosen slot, length word, and fixed tail.
	start := uint64(32 + 3*16 + 64)
	if got := le.Uint64(s.data[56:64]); got != uint64(len(payload)) {
		t.Fatal("header counted in length", got)
	}
	if !bytes.Equal(s.data[start:start+uint64(len(payload))], payload) || le.Uint32(s.data[start+56:]) != 5 || le.Uint32(s.data[start+60:]) != 31 {
		t.Fatal("wire slot layout incorrect")
	}
	s.regions[0].layout.Producer = Host
	buf := make([]byte, 64)
	got, err := s.ReadPulsed(0, buf)
	if err != nil || got != sample || !bytes.Equal(buf[:got.Length], payload) {
		t.Fatal(got, err)
	}
	decoded, err := s.regions[0].layout.ReadEpoch(buf, 0)
	if err != nil || *decoded != *pair {
		t.Fatal(decoded, err)
	}
	if word := loadState(s.data, 48); word != 0 {
		t.Fatal("reader retained claim", word)
	}
}
