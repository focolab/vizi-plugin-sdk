package memory

import (
	"bytes"
	"context"
	"errors"
	"math"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
)

func TestEpochHeaderPresenceBoundsAndOwnershipUnit(t *testing.T) {
	zero := uint64(0)
	for _, discipline := range []Discipline{Constant, Owned, Pulsed, Loaned} {
		r := Region{Discipline: discipline, EpochOffset: &zero}
		data := make([]byte, 8)
		le.PutUint32(data[4:], 17)
		pair, err := r.ReadEpoch(data, 0)
		if err != nil || pair.ViewID != 0 || pair.Epoch != 17 {
			t.Fatalf("%v %v", pair, err)
		}
		if pair, err := r.ReadEpoch(data[:7], 0); (discipline == Loaned && (pair != nil || err != nil)) || (discipline != Loaned && !errors.Is(err, ErrBounds)) {
			t.Fatal(err)
		}
		le.PutUint32(data, 256)
		if _, err := r.ReadEpoch(data, 0); err == nil {
			t.Fatal("high view bytes accepted")
		}
	}
	if p, err := (Region{}).ReadEpoch(nil, 0); p != nil || err != nil {
		t.Fatal("absent header fabricated")
	}
}

func TestReadEpochRejectedPairPreservesCopiedPayload(t *testing.T) {
	for _, tc := range []struct {
		name        string
		view, epoch uint32
	}{
		{"invalid view", 256, 17},
		{"zero epoch", 3, 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			offset := uint64(8)
			r := Region{Discipline: Pulsed, SlotBytes: 16, EpochOffset: &offset}
			copied := make([]byte, 16)
			copy(copied, "payload!")
			le.PutUint32(copied[8:], tc.view)
			le.PutUint32(copied[12:], tc.epoch)
			before := bytes.Clone(copied)
			pair, err := r.ReadEpoch(copied, 0)
			if pair != nil || !errors.Is(err, ErrRejectedPair) || errors.Is(err, ErrBounds) {
				t.Fatalf("rejected annotation must be distinguishable: pair=%v err=%v", pair, err)
			}
			if !bytes.Equal(copied, before) {
				t.Fatal("annotation rejection changed copied data")
			}
			if _, err := r.ReadEpoch(copied[:15], 0); !errors.Is(err, ErrBounds) || errors.Is(err, ErrRejectedPair) {
				t.Fatalf("partial copy mislabeled as a rejected pair: %v", err)
			}
		})
	}
}

func TestRejectedGrantIsReplayableAndQueryable(t *testing.T) {
	r, err := New(Config{Purposes: []string{"test"}})
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	f := &doorbell.Frame{Disposition: doorbell.Rejected, HasRecord: true, HasHeader: true, Memory: &doorbell.Memory{SegmentID: 9, Size: 31}, Violation: &doorbell.Violation{Where: "size_below_preamble", Route: "grant_ack", Class: "malformed"}}
	u, err := r.RejectGrant(f)
	if err != nil || u.Ack == nil || u.Ack.Failure.Class != Malformed {
		t.Fatalf("%+v %v", u, err)
	}
	status := r.Status(9)
	if status.State != FailedState || !status.HasGrant || status.HasInstall {
		t.Fatal(status)
	}
	u, err = r.RejectGrant(f)
	if err != nil || u.Ack.Failure.Detail != "size_below_preamble" {
		t.Fatalf("%+v %v", u, err)
	}
	if _, err = r.Retire(9); err != nil {
		t.Fatal(err)
	}
	u, err = r.RejectGrant(f)
	if err != nil || len(u.Released) != 1 || u.Ack != nil {
		t.Fatalf("retired grant revived: %+v %v", u, err)
	}
}

func TestInstallCopiesEpochPresenceAndRejectsOverflow(t *testing.T) {
	r, err := New(Config{Purposes: []string{"test"}})
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	offset := uint64(0)
	in := InstallSegment{SegmentID: 1, Purpose: "test", Regions: []Region{{Index: 0, Offset: 32, Extent: 16, Discipline: Constant, Producer: Host, EpochOffset: &offset}}}
	if u, err := r.Install(in); err != nil || u.Ack != nil {
		t.Fatalf("%v %v", u, err)
	}
	offset = math.MaxUint64
	// Modifying the caller's optional pointer must not change the saved install.
	copyOffset := uint64(0)
	in.Regions[0].EpochOffset = &copyOffset
	if u, err := r.Install(in); err != nil || u.Ack != nil {
		t.Fatalf("saved install aliased caller: %v %v", u, err)
	}
	in.SegmentID = 2
	in.Regions[0].EpochOffset = &offset
	u, err := r.Install(in)
	if err != nil || u.Ack == nil || u.Ack.Failure.Class != Malformed {
		t.Fatalf("overflow not rejected: %v %v", u, err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := r.Wait(ctx, 1); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	if _, err := r.Retire(1); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Wait(context.Background(), 1); !errors.Is(err, ErrRetired) {
		t.Fatal(err)
	}
}
