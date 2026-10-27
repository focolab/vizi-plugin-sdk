// Package memory implements the byte-access and grant lifecycle portions of
// vizi shared memory protocol 1.0. See README.md for integration obligations.
// It does not decode application payloads or send control-plane messages.
package memory

import (
	"errors"
	"fmt"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
)

// Enum numbers match proto/vizi_plugin.proto. Zero never selects a default.
type Discipline uint32

const (
	Constant Discipline = 1 + iota
	Owned
	Pulsed
	Loaned
	Staged
)

type Producer uint32

const (
	Host Producer = 1 + iota
	Plugin
)

type Region struct {
	Index          uint32
	Offset, Extent uint64 // mapping-relative, including the 32-byte preamble
	Discipline     Discipline
	Producer       Producer
	Buffers        uint32
	SlotBytes      uint64
	ID             string
	EpochOffset    *uint64 // present even at zero; slot/deposit-relative for Pulsed/Loaned, region-relative otherwise
}

func cloneRegion(r Region) Region {
	if r.EpochOffset != nil {
		offset := *r.EpochOffset
		r.EpochOffset = &offset
	}
	return r
}

type InstallSegment struct {
	SegmentID  uint64
	Purpose    string
	Subject    string
	Supersedes uint64
	Regions    []Region
}

type FailureClass string

const (
	Retryable   FailureClass = "retryable"
	TooLarge    FailureClass = "too_large"
	Malformed   FailureClass = "malformed"
	Unsupported FailureClass = "unsupported"
	Failed      FailureClass = "failed"
)

type GrantFailure struct {
	Class  FailureClass
	Detail string
}

func (f *GrantFailure) Error() string { return string(f.Class) + ": " + f.Detail }
func failure(class FailureClass, detail string) *GrantFailure {
	return &GrantFailure{class, detail}
}

// Ack has either a proof read from the mapping or a Failure. It is a value
// snapshot; modifying it cannot change the replay saved by the registry.
type Ack struct {
	SegmentID uint64
	Proof     [16]byte
	Failure   *GrantFailure
}
type State string

const (
	NotPresent  State = "not_present"
	Pending     State = "pending"
	Installed   State = "installed"
	FailedState State = "failed"
	Released    State = "released"
)

type Status struct {
	State                State
	HasGrant, HasInstall bool
	Ack                  *Ack
}

// Update is control-plane work for the caller to send reliably. Empty means
// still pending. Released contains SegmentReleased identities, after unmapping.
// PendingReleases must be retried after LoansDrained; do not acknowledge them
// early. ErrLoanActive/ErrStageActive indicate deferred retirement, not faults.
// Other errors are local/integration or cleanup failures; do not invent an ack.
type Update struct {
	Ack             *Ack
	Released        []uint64
	PendingReleases []uint64 // loan publications/deposit reads must drain before retrying Retire
}

// Config expresses local policy. Purposes optionally overrides the exact
// vocabulary implemented by the embedding. Nil selects G/V/O/P/X/S; an explicit
// empty slice accepts no purposes. MaxSegmentBytes defaults to 1 GiB.
type Config struct {
	// Contributions optionally lists known manifest IDs under purpose O or P.
	// Nil means the manifest is unknown: validate subject shape and trust the name.
	// A non-nil map enables membership checks; an empty map declares none.
	// Other purposes carry no subject and must not appear here.
	Contributions   map[string][]string
	Purposes        []string
	MaxSegmentBytes uint64
}

var (
	ErrClosed         = errors.New("memory session is closed")
	ErrNotInstalled   = errors.New("segment is not installed")
	ErrRetired        = errors.New("segment is retiring or released")
	ErrQuarantined    = errors.New("region is quarantined")
	ErrBounds         = errors.New("range is outside the region or destination is too small")
	ErrDiscipline     = errors.New("operation does not match region discipline or producer")
	ErrConsumed       = errors.New("handoff has already been consumed")
	ErrConsumerActive = errors.New("region already has a scheduled Pulsed consumer")
)

// Violation names the normative ContractViolation.where. The first corrupting
// failure quarantines that region; later operations return ErrQuarantined.
// The session-wide violations.Reporter supplies rate-bounded reporting, count
// and optional raw D when the supervisor wires its observation adapters.
type Violation struct {
	Where, Detail string
	SegmentID     uint64
	Region        uint32
}

func (v *Violation) Error() string {
	return fmt.Sprintf("%s: segment %d region %d: %s", v.Where, v.SegmentID, v.Region, v.Detail)
}

// Deposit is the release key a loan is released by, in either direction. Offset
// is MAPPING-relative -- the number the descriptor carried, echoed back
// unchanged (shared_memory_protocol.md §1, §7) -- and Length is the requested
// length, never the allocator's padded length. Region is redundant with Offset
// on purpose: a receiver checks that the offset falls inside the region named,
// so a mistargeted release is detected rather than applied.
type Deposit struct {
	SegmentID      uint64
	Region         uint32
	Offset, Length uint64
}

// HintPlan schedules work, never proves freshness. Regions includes host-produced
// Pulsed regions: named ones, or all of them for an out-of-range-bit sweep.
// Active PulsedConsumers are signaled automatically.
// Diagnostics are local, countable codes; repeated entries count separate bits.
type HintPlan struct {
	Regions     []uint32
	Diagnostics []string
	Violations  []Violation
}

// Keep the permission type identical to the doorbell's declaration.
type Permissions = doorbell.Permissions

// PayloadCapacity excludes a Pulsed slot's optional eight-byte pane header.
func (r Region) PayloadCapacity() uint64 {
	if r.Discipline == Pulsed && r.EpochOffset != nil {
		if r.SlotBytes < 8 {
			return 0
		}
		return r.SlotBytes - 8
	}
	return r.SlotBytes
}
