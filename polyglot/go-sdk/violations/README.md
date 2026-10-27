# Session-wide ContractViolation reporting

`violations` is the reporting layer for the Go doorbell and memory packages.
It needs no generated gRPC bindings. One `Reporter` per session collects both
layers' detections; a control-plane sender calls `Next(ctx)` when ready for one
more reliable message. Observations hold a short local mutex and never wait for
network delivery. The reporter starts no background goroutines.

```go
reports, err := violations.New(violations.Config{})
// Check err, then share reports across all session detectors.

// In the doorbell callback, once for each frame:
err = reports.ObserveFrame(frame)
// Handle err locally. grant_ack and framing faults still use their own routes.

// After processing a decoded D, once for the whole received record:
plan := segment.Hint(*frame.Notification, acknowledgedEpoch)
err = reports.ObserveHint(frame.Raw, plan)

// When a synchronous read or PulsedUpdate returns an error:
err = reports.ObserveError(readError)
// Also handle readError itself: non-violation errors remain local errors.
```

Check observation errors. Invalid categories and invalid input are integration
errors; they do not generate invented wire violations. Admission is atomic across
a batch. Calling an adapter and then submitting its individual detections again
would double-count them. These are explicit hooks for the supervisor to wire;
constructing a reporter alone does not attach it to an existing session.

The reliable sender owns this loop, separate from memory polling and doorbell
callbacks:

```go
for {
    report, err := reports.Next(ctx)
    if errors.Is(err, io.EOF) {
        break
    }
    if err != nil {
        return err
    }
    // Translate Where numerically to ViolationSite, Detail to detail,
    // Count to count, and Record[:] to record when Record != nil.
    // Keep ownership until the reliable queue accepts this message.
    if err := enqueueReliably(ctx, report); err != nil {
        // Resolve this in-flight message in the session's failure path.
        // It has left the reporter; do not silently drop it or Observe it again.
        return err
    }
}
```

`enqueueReliably` is an integration placeholder. The package does not send gRPC,
decide retry/reconnect semantics, or close the doorbell. `Next` is the dequeue
boundary before the one-shot reliable queue, so aggregation and prioritization
happen before messages enter that queue. Call `Next` only when ready to accept a
report; prefetching would move prioritization out of the reporter. A channel-based
sender may forward its result to a bounded channel, retaining at most one
in-flight report while that send blocks. New observations continue accumulating;
they cannot preempt a report already transferred to the sender.

## Granularity and evidence

The fixed vocabulary names every `ViolationSite` category. Site 28,
`OverlayItemRefused` / `overlay_item_refused`, is host → plugin only; plugin
detectors do not emit it. Receive it through `control.Config.OnMessage` as a
`HostMsg.ContractViolation`: its detail identifies the overlay, item and refused
rule, and count folds repetitions. The generated enum and `Site.String` both
name it; no unknown-enum filter drops the received report.
With the authoring layer, set `plugin.Handlers.PeerViolation` to handle it;
without that hook, the handler returns a diagnostic containing the named
protobuf report, including detail and count.

Site 29, `PanelPatchRefused` / `panel_patch_refused`, uses the same receive path
and is also host → plugin only. Its detail names the panel, op, widget and rule.
Refusal applies to the individual panel-patch op; neighboring ops still apply.
It has a separate site because panel and overlay failures have separate counters.

The reporter retains observations by category even with no sender.
Detail, segment IDs, region indices, wrapping doorbell IDs and raw bytes are not
aggregation keys. The reportable wire identity is the rule; diagnostic context
identifies an affected resource without creating a new rate budget per resource.

- `ObserveRecord` counts once per `(received record, rule)`. Several Owned bits
  in one D contribute one occurrence of `OWNED_REGION_HINT`, with their segment
  and region details combined. Two receptions of identical bytes count twice.
- `Observe` counts one recordless occurrence. `ObserveError` walks every memory
  violation in a wrapped/joined error, so invalid length and a failed release CAS
  remain two separately actionable rules. Later `ErrQuarantined` is not another
  peer violation.
- An ordinary category's first report always has `Count=1`, original detail and
  a copied raw record when available, even when transmission was delayed and
  repeats have accumulated. Those repeats stay pending for subsequent reports.
- Repeats carry their accumulated count and representative detail from the
  latest admitted observation/batch, without a raw record. They are continued
  occurrences of a rule, not claims of newly discovered defects. Detail is valid
  UTF-8, copied and capped at 1024 bytes with `...` on truncation. It is diagnostic
  evidence, not a complete history of every affected region.
- `Stats` exposes lifetime observed/emitted/pending occurrence counts and report
  totals for every category. Emitted means transferred by `Next`, not acknowledged
  by the host. Counts use uint64 locally; large repeats split into uint32 wire
  chunks without loss. At uint64 exhaustion admission explicitly fails rather
  than wrapping a counter.

## Scheduling and bounds

Defaults are **one report every 100 ms**, with **at least one second between
ordinary reports of the same category** after its first report. These are SDK
policy, not wire deadlines; execution also depends on Go/OS scheduling and sender
readiness. Every report, including first occurrences and exempt grants, consumes
the same spacing budget. Changing the rule or region never resets it. Idle time
does not accumulate a burst of tokens.

At each dequeue, all as-yet-unreported ordinary categories take priority over
repeats, in first-observed order (enum order for simultaneous batch findings).
Then pending individual M/zero_id reports run FIFO, then eligible ordinary
repeats run round-robin. There are finitely many ordinary first reports, so an
alternating-rule flood cannot endlessly claim novelty. Observation traffic never
postpones an existing repeat deadline. A blocked `Next` owns a timer and emits
pending repeats even if all new traffic stops.

The session budget **replenishes with time**; there is no lifetime report cap
that would permanently suppress a novel rule discovered late. This is the SDK's
interpretation of the spec's "per-session report budget". A low configured rate
delays first reports, preserving them rather than suppressing them.

`M/zero_id` keeps the spec's exception: one raw report per affected grant, never
an aggregated count. It is separate from ordinary E/zero_id aggregation and
does not consume that category's first-record evidence. Since the number of
grants is not the number of enum categories, these reports need their own bounded
FIFO: default 64 slots, configurable by `MaxGrantReports`. Full admission returns
`ErrGrantQueueFull` without altering counts or taking ownership. The supervisor
must retain/retry that detection through its bounded recovery path or handle
session failure; ignoring the error loses the host's grant correlation. The
reporter never blocks a high-rate detector on a full queue. Ordinary categories
continue to be admitted while the grant queue is full. Submit each affected grant
once; the reporter cannot infer a grant identity from its invalid zero ID.

Call `Close` after all session detectors have stopped. It stops admission and
wakes the sender, which can drain pending reports at the normal rate to `io.EOF`.
Cancellation bounds how long the supervisor waits; pending counts remain in
`Stats`. Closing never floods the control plane with an unthrottled final batch.

## Integration and verification

The counting, budget and delay choices above are SDK policy, not wire rules.
A custom supervisor must wire all detection sources, count local diagnostics
separately, convert these reports to protobuf, and admit them to the session's
bounded reliable sender. `control.ServeEnv` does this automatically.

`go test -race ./...` includes virtual-time scheduling tests, all-category floods,
concurrent detectors, record granularity, joined memory errors, malformed-grant
queue saturation, cancellation/drain, count overflow, and a check against the
repository's authoritative enum numbers.

Host-only `overlay_chunk_refused` (30) reports an inapplicable wanted chunk or
an `OverlayUpdate` sent to a chunked overlay. Unwanted chunks are dropped and
counted by the host instead. Received reports use the existing `OnMessage` /
`Handlers.PeerViolation` hooks and retain their detail and count.
