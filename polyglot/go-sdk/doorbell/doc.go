// Package doorbell implements the plugin side of vizi doorbell v2 (protocol
// 1.0) on Linux, macOS, and Windows. It does not implement gRPC or map shared memory.
//
// FromEnv adopts the inherited Unix socket, or connects and authenticates the
// Windows loopback TCP socket. Adopt (Unix) and Dial (Windows) are explicit forms.
// Startup starts a listener before the caller publishes readiness. Call
// HandshakeComplete before acknowledging the matching control-plane handshake.
// Windows requires the Hello session token as its third argument. Publishing
// the Windows gRPC port file belongs to the supervisor, after transport startup.
// Register tools and pane epochs before acknowledging their arrangements.
// Events are delivered serially, without transport locks held. Handlers should
// return within 1 ms; slow handlers deliberately propagate socket backpressure.
// Stop is cancellation, not a join; Wait is bounded only by the handler.
//
// Terminal is a dedicated, buffered, single-publication supervisor mailbox.
// Fault publication precedes socket release and cannot wait on a handler or gRPC.
// The supervisor must drain it independently and report faults over gRPC. The
// same terminal outcome subsequently arrives in the ordered event stream, after
// cancellations. These are two views of one outcome, not two reports to send.
//
// A received grant descriptor is claimable only during its event handler. Claim
// transfers ownership to an *os.File; otherwise the transport closes it when the
// handler returns, including a panic. Rejected grants never expose descriptors.
// On Windows, grants instead carry a validated Memory.Name. The memory installer
// must open that existing section and validate its identity and preamble.
//
// Notifications are advisory: receivers must independently recheck every consumed
// Pulsed region within a documented finite bound, never gate that schedule on a
// notification, use hints only to schedule work, rate-limit that work themselves,
// and use a memory handoff discipline safe to recheck. Region validation and
// polling belong to the consumer. Successful sends mean acceptance, not delivery.
package doorbell
