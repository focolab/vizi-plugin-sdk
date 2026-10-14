// Package violations aggregates ContractViolation detections across a session.
// Detectors never wait for network delivery. A control-plane sender calls Next
// when ready to accept another reliable message; see README.md for integration.
package violations

import (
	"errors"
	"fmt"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
)

// Site numbers are the ViolationSite wire enum in proto/vizi_plugin.proto.
// Zero and unknown values are rejected, never silently assigned a new category.
type Site uint32

const (
	EpochMismatch Site = 1 + iota
	InteractionMismatch
	MissingArrangement
	ZeroID
	MissWithPicked
	UnknownTool
	OwnedRegionHint
	ImpossibleStateWord
	InvalidPublishedLength
	ReleaseCASFailed
	WrongProducerRelease
	UnknownRelease
	SeqOnOpen
	PositionTail
	NonfinitePosition
	InvalidTarget
	// Appended AFTER the sixteen above: those numbers are fixed, and the host
	// switches on them.
	InstallWithoutSegment
	ReleaseRegionMismatch
	ArrangementContentMismatch
	ArrangementRegressed
	RepeatedFieldOverflow
	LiveGeometryNotInline
	LaneSubjectMismatch
	UsesDisagreement
	MissingDeadline
	ArrangementFirstRevision
	UnusableContext
	// Host -> plugin only: an overlay push was refused. Kept in the shared
	// site vocabulary for decoding; plugin-side detectors do not emit it.
	OverlayItemRefused
	// Host -> plugin only: one panel-patch op was refused; other ops still
	// apply. Kept separate from overlay refusal for per-site accounting.
	PanelPatchRefused
	// Host -> plugin only: a wanted chunk or a push to a chunked overlay was refused.
	OverlayChunkRefused
	StageSlotInvalid
	StageArmRefused
	// Host -> plugin only: shape exchange ordering and invalid geometry.
	ShapeOutOfStep
	ShapeRefused
	siteLimit
)

var siteNames = [siteLimit]string{
	EpochMismatch: "epoch_mismatch", InteractionMismatch: "interaction_mismatch",
	MissingArrangement: "missing_arrangement", ZeroID: "zero_id",
	MissWithPicked: "miss_with_picked", UnknownTool: "unknown_tool",
	OwnedRegionHint: "owned_region_hint", ImpossibleStateWord: "impossible_state_word",
	InvalidPublishedLength: "invalid_published_length", ReleaseCASFailed: "release_cas_failed",
	WrongProducerRelease: "wrong_producer_release", UnknownRelease: "unknown_release",
	SeqOnOpen: "seq_on_open", PositionTail: "position_tail",
	NonfinitePosition: "nonfinite_position", InvalidTarget: "invalid_target",
	InstallWithoutSegment:      "install_without_segment",
	ReleaseRegionMismatch:      "release_region_mismatch",
	ArrangementContentMismatch: "arrangement_content_mismatch",
	ArrangementRegressed:       "arrangement_regressed",
	RepeatedFieldOverflow:      "repeated_field_overflow",
	LiveGeometryNotInline:      "live_geometry_not_inline",
	LaneSubjectMismatch:        "lane_subject_mismatch",
	UsesDisagreement:           "uses_disagreement",
	MissingDeadline:            "missing_deadline",
	ArrangementFirstRevision:   "arrangement_first_revision",
	UnusableContext:            "unusable_context",
	OverlayItemRefused:         "overlay_item_refused",
	PanelPatchRefused:          "panel_patch_refused",
	OverlayChunkRefused:        "overlay_chunk_refused",
	StageSlotInvalid:           "stage_slot_invalid",
	StageArmRefused:            "stage_arm_refused",
	ShapeOutOfStep:             "shape_out_of_step", ShapeRefused: "shape_refused",
}

func (s Site) valid() bool { return s > 0 && s < siteLimit }

func (s Site) String() string {
	if !s.valid() {
		return fmt.Sprintf("violation_site(%d)", s)
	}
	return siteNames[s]
}

func ParseSite(name string) (Site, error) {
	for s := Site(1); s < siteLimit; s++ {
		if siteNames[s] == name {
			return s, nil
		}
	}
	return 0, fmt.Errorf("%w: %q", ErrUnknownSite, name)
}

// Finding describes a rule violation. Detail is diagnostic text, never a key.
// Include the segment/region or other actionable context when available.
type Finding struct {
	Where  Site
	Detail string
}

// Report maps directly to ContractViolation. Record is an independent copy, nil
// on aggregated repeats or when the first detection had no doorbell record.
// A returned report belongs to the sender and must be delivered reliably once.
type Report struct {
	Where  Site
	Detail string
	Record *doorbell.RawRecord
	Count  uint32
}

// Config is receiver policy, not a set of wire constants. Zero selects defaults.
// ReportInterval spaces ALL reports, including first reports and exempt grants,
// under one renewable session budget with a burst of one. RepeatInterval also
// limits how often any ordinary category can repeat. There is no lifetime cap
// that could permanently hide a category discovered late in a long session.
type Config struct {
	ReportInterval  time.Duration // default 100 ms (at most ten reports/second)
	RepeatInterval  time.Duration // default one second per category
	MaxGrantReports int           // default 64 pending M/zero_id reports
}

// MaxDetailBytes bounds each retained diagnostic, with UTF-8-safe truncation.
const MaxDetailBytes = 1024

var (
	ErrClosed         = errors.New("violation reporter is closed for observations")
	ErrUnknownSite    = errors.New("unknown violation site")
	ErrInvalidFrame   = errors.New("contract violation requires a complete validated frame")
	ErrGrantQueueFull = errors.New("pending M/zero_id report queue is full; observation was not admitted")
	ErrCountOverflow  = errors.New("violation occurrence counter would overflow; observation was not admitted")
)

// Counts are lifetime totals for one wire category. Pending includes occurrences
// waiting for a first report, repeat, or individual M/zero_id report. Emitted
// counts occurrences transferred by Next, not network acknowledgements.
type Counts struct {
	Observed, Emitted, Pending, Reports uint64
}
