package violations

import (
	"fmt"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
)

// ObserveFrame admits a doorbell contract_violation. Grant acknowledgements,
// framing faults and local diagnostics retain their existing independent routes.
// It does not take ownership of the frame's descriptor.
func (r *Reporter) ObserveFrame(f doorbell.Frame) error {
	if f.Violation == nil || f.Violation.Route == "grant_ack" {
		return nil
	}
	if f.Violation.Route != "contract_violation" || !f.HasRecord || !f.HasHeader {
		return ErrInvalidFrame
	}
	s, err := ParseSite(f.Violation.Where)
	if err != nil {
		return err
	}
	detail := fmt.Sprintf("%c record %d (receive %d): %s", f.Header.Op, f.Header.ID, f.Ordinal, s)
	return r.ObserveRecord(f.Raw, Finding{s, detail})
}

// ObserveHint admits all violations detected for ONE received D, counting once
// per rule even when several regions were named. Segment and region context is
// retained in bounded detail. Diagnostics remain local, not ContractViolations.
func (r *Reporter) ObserveHint(raw doorbell.RawRecord, plan memory.HintPlan) error {
	if raw[0] != doorbell.OpNotification {
		return ErrInvalidFrame
	}
	findings := make([]Finding, 0, len(plan.Violations))
	for i := range plan.Violations {
		v := &plan.Violations[i]
		s, err := ParseSite(v.Where)
		if err != nil {
			return err
		}
		findings = append(findings, Finding{s, v.Error()})
	}
	return r.ObserveRecord(raw, findings...)
}

// ObserveError walks wrapped and joined memory errors, including simultaneous
// invalid-length and release-CAS failures. Each *memory.Violation leaf is one
// recordless occurrence. Ordinary Go errors (including ErrQuarantined) are not
// peer violations; the caller must still handle the original error locally.
// Submit each failed operation once, not both its wrapper and its leaves.
func (r *Reporter) ObserveError(err error) error {
	var groups [siteLimit]group
	var walk func(error) error
	walk = func(err error) error {
		switch e := err.(type) {
		case *memory.Violation:
			if e == nil {
				return nil
			}
			s, parseErr := ParseSite(e.Where)
			if parseErr != nil {
				return parseErr
			}
			g := &groups[s]
			g.count++
			g.detail = combine(g.detail, e.Error())
		case interface{ Unwrap() []error }:
			for _, child := range e.Unwrap() {
				if err := walk(child); err != nil {
					return err
				}
			}
		case interface{ Unwrap() error }:
			return walk(e.Unwrap())
		}
		return nil
	}
	if err := walk(err); err != nil {
		return err
	}
	return r.observe(groups, nil)
}
