package control

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"maps"
	"sync/atomic"
	"time"
	"unicode/utf8"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/manifest"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/stage"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/violations"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

const ProtocolMajor = doorbell.ProtocolMajor
const ProtocolMinor = doorbell.ProtocolMinor

// Config separates SDK routing from application work. Arrange and Release are
// bounded SDK operations, not application callbacks. HandleRequest, OnMessage,
// Shutdown and Connected run on separate workers and may call the host. All
// callbacks must honor cancellation; Go cannot forcibly terminate a goroutine.
type Config struct {
	// Shapers maps declared region type IDs to computations. Registration is
	// snapshotted by NewServer. Missing handlers reject every commit explicitly.
	Shapers map[string]ShapeHandler
	// ShapeEditEnded runs on the notification worker, after preview cancellation.
	// Unknown drags are delivered too, without any host diagnostic.
	ShapeEditEnded func(context.Context, *Client, *pb.ShapeEditEnded) error
	// Panels supplies declaration bounds and row-cell ownership for raw as well
	// as typed panel updates. Definition.Configure fills it automatically.
	Panels map[string]PanelLayout
	// StageMemory and StageNotify are installed by ServeEnv. Custom servers must
	// supply an installed registry and a D sender to enable stage processing.
	StageMemory       *Memory
	StageNotify       func(doorbell.Notification) error
	Stages            map[string]StageHandlers
	StagePollInterval time.Duration // default 40 ms; D also wakes the worker

	PluginID string
	// BuildIdentity optionally replaces the plugin-name label in Ready.id.
	// Embedded main/SDK module and VCS provenance is always appended. Empty
	// uses PluginID. The complete identity must fit 2048 UTF-8 bytes, no Cc.
	BuildIdentity string
	// Engines is the manifest's engines.vizi, the oldest protocol the plugin
	// was written for; empty means "1.0". A Ready below it is refused at the
	// handshake. Definition.Configure fills it.
	Engines  string
	Reporter *violations.Reporter // optional shared session reporter; Server owns delivery
	// Handshake authenticates Hello and opens the doorbell's version gate. It
	// runs after identity and major-version validation, before Ready; required
	// even for a test peer (which can explicitly supply a no-op).
	Handshake func(*pb.Hello) error
	// SessionToken, when non-empty, is the Windows session token every stream
	// must prove before it can become the session. A stream presenting it in
	// call metadata (SessionTokenMetadataKey) is checked before its handler
	// runs; a stream without that key is checked against Hello.session_token.
	// Both compares are constant-time. A stream with a missing or wrong token
	// is refused UNAUTHENTICATED and never ends the session: the server keeps
	// accepting streams until one authenticates. ServeEnv sets it on Windows.
	SessionToken string
	// Attach and End are SDK lifecycle hooks. Attach publishes the Client to
	// the doorbell supervisor before Ready can trigger any host grants.
	Attach        func(*Client) error
	End           func(error)
	Connected     func(*Client)
	HandleRequest func(context.Context, *Client, *pb.HostMsg) (*pb.PluginMsg, error)
	OnMessage     func(context.Context, *Client, *pb.HostMsg) error
	// Subscribed state callbacks run on independent workers and may call the host.
	// Pending views are replaced; pending regions merge by ID, preserving resets.
	// Each callback owns its message. Without a typed callback, OnMessage receives
	// these pushes on those same workers, independently of ordinary notifications.
	ViewChanged    func(context.Context, *Client, *pb.ViewChanged) error
	RegionsChanged func(context.Context, *Client, *pb.RegionsChanged) error
	// WidgetGeometry hears how large an image widget is drawn (§8.4), on its
	// own worker: the newest per (panel, widget), so changes heard while it
	// runs reach it as the latest alone. It may call the host. Without it a
	// geometry is only recorded, for Client.WidgetGeometry.
	WidgetGeometry func(context.Context, *Client, *pb.WidgetGeometry) error
	// Appearance hears the host's theme and colors (§8.4): once after Ready,
	// before any panel opens, and again whenever the person switches theme.
	// It runs on its own worker with the newest heard, so several changes in a
	// row may reach it as the latest alone. It may call the host. Without it
	// an appearance is only recorded, for Client.Appearance.
	Appearance func(context.Context, *Client, *pb.HostAppearance) error
	// ChunksWanted applies every set synchronously in receive order. Keep it
	// bounded: sources and host reads belong on a separate worker.
	ChunksWanted func(*Client, *pb.ChunksWanted) error
	// FollowTransition is a bounded SDK hook, called in receive order for
	// FollowStarted/FollowEnded. Run application drivers on their own workers.
	// Without it, OnMessage receives these one-shot notices.
	FollowTransition func(*Client, *pb.HostMsg) error
	// ToolActivated/ToolDeactivated are optional ServeEnv application hooks.
	// They run serially on the doorbell listener after installation and ack
	// admission, and after queued cancellations on retirement. Keep them short;
	// hand blocking host calls to a worker. Replays and in-place binding updates
	// do not repeat engagement hooks. The binding is an independent snapshot.
	ToolActivated   func(context.Context, *Client, *pb.ToolBinding) error
	ToolDeactivated func(context.Context, *Client, *pb.ToolBinding) error
	Arrange         func(*Client, *pb.HostMsg) error
	// Admit applies ride-along meaning in stream order before delegating waits.
	Admit func(*Client, *pb.HostMsg) error
	// AdmitOutbound checks and counts sender loan uses at final queue admission.
	// This bounded SDK hook runs under the queue lock and must not call Client.
	// On error it must leave all counts unchanged. ServeEnv supplies Memory's hook.
	AdmitOutbound func(*pb.PluginMsg) error
	// SentOutbound ends publication claims after a successful transport send.
	// Pair Memory.SentOutbound with Memory.AdmitOutbound in custom servers.
	SentOutbound func(*pb.PluginMsg)
	// ReaderMemory supplies G loans for the volume-only ReadPlane fallback.
	// ServeEnv installs its Memory; custom servers use their outbound loan registry.
	ReaderMemory *Memory
	// Prepare holds descriptors unread until both installation halves land.
	// Workers are bounded and ordered per segment, never on the routing loop.
	// ErrDescriptorDropped disposes the descriptor and answers requests NOT_FOUND.
	Prepare  func(context.Context, *Client, *pb.HostMsg) error
	Release  func(*Client, *pb.HostMsg) error
	Shutdown func(context.Context, *Client, *pb.Shutdown) error
	// Diagnostic runs serially on its own bounded worker. The default writes to
	// stderr, retained by the host. A stalled diagnostic consumer faults the
	// session when its queue fills rather than blocking routing.
	// Normal shutdown is quiet; hook errors and deadline overruns are diagnosed.
	Diagnostic func(error)
	// RequestTimeout bounds ordinary outgoing host calls; zero defaults to 30 seconds.
	RequestTimeout time.Duration
	// PickerTimeout is the dead-session backstop for RequestAsset and RequestSavePath,
	// requests answered by a person, not a work budget. Zero defaults to 30 minutes.
	// A shorter caller context deadline still wins. Both timeouts must be nonnegative.
	PickerTimeout time.Duration
	// MaxPendingRequests defaults to 32. An outgoing Call refused at this
	// limit returns ErrQueueFull locally. The same limit bounds received
	// descriptors awaiting installation; exceeding that backlog faults the
	// session, because their ownership cannot be silently dropped.
	MaxPendingRequests int
	QueueCapacity      int // default 256; reliable queue overflow faults the session
	CoalescingCapacity int // default 4096 ordinary sender keys, plus 68 work and 4 shape keys; local refusal
}

// Server accepts exactly one session for its entire lifetime; streams refused
// for their session token do not count. The same memory, tool handles and
// transport cannot be reused on a new stream.
type Server struct {
	pb.UnimplementedPluginServer
	cfg       Config
	identity  string
	buildInfo string
	used      atomic.Bool
}

func NewServer(cfg Config) (*Server, error) {
	cfg.Panels = clonePanelLayouts(cfg.Panels)
	cfg.Shapers = maps.Clone(cfg.Shapers)
	if cfg.PluginID == "" || cfg.Handshake == nil {
		return nil, errors.New("PluginID and Handshake are required")
	}
	if cfg.Engines != "" {
		if _, _, err := manifest.EnginesMinimum(cfg.Engines); err != nil {
			return nil, err
		}
	}
	var err error
	cfg.Stages, err = CloneStageHandlers(cfg.Stages)
	if err != nil {
		return nil, err
	}
	identity, details, err := buildIdentity(cfg)
	if err != nil {
		return nil, err
	}
	if cfg.RequestTimeout == 0 {
		cfg.RequestTimeout = DefaultRequestTimeout
	}
	if cfg.PickerTimeout == 0 {
		cfg.PickerTimeout = DefaultPickerTimeout
	}
	if cfg.MaxPendingRequests == 0 {
		cfg.MaxPendingRequests = 32
	}
	if cfg.QueueCapacity == 0 {
		cfg.QueueCapacity = 256
	}
	if cfg.CoalescingCapacity == 0 {
		cfg.CoalescingCapacity = 4096
	}
	if cfg.StagePollInterval < 0 || cfg.CoalescingCapacity < 1 || cfg.RequestTimeout < 0 || cfg.PickerTimeout < 0 || cfg.MaxPendingRequests < 1 || cfg.QueueCapacity < 1 {
		return nil, errors.New("control resource bounds must be positive")
	}
	if cfg.Diagnostic == nil {
		cfg.Diagnostic = func(err error) { log.Print(err) }
	}
	if err := checkRoutes((&pb.HostMsg{}).ProtoReflect(), hostRoutes); err != nil {
		return nil, err
	}
	if err := checkRoutes((&pb.PluginMsg{}).ProtoReflect(), pluginRoutes); err != nil {
		return nil, err
	}
	return &Server{cfg: cfg, identity: identity, buildInfo: details}, nil
}

// GRPCServer installs the published wire-size ceiling in both directions.
func (s *Server) GRPCServer() *grpc.Server {
	g := grpc.NewServer(grpc.MaxRecvMsgSize(MaxMessageBytes), grpc.MaxSendMsgSize(MaxMessageBytes), grpc.StreamInterceptor(s.authenticateStream))
	pb.RegisterPluginServer(g, s)
	return g
}

type received struct {
	msg *pb.HostMsg
	err error
}

func (s *Server) Session(stream grpc.BidiStreamingServer[pb.HostMsg, pb.PluginMsg]) (result error) {
	// Only an authenticated stream claims the session; a stream that fails to
	// prove itself is refused and the server keeps waiting for the host.
	claimed := false
	claim := func() error {
		if !s.used.CompareAndSwap(false, true) {
			return status.Error(codes.AlreadyExists, "plugin already served its session")
		}
		claimed = true
		return nil
	}
	if s.cfg.End != nil {
		defer func() {
			if claimed {
				s.cfg.End(result)
			}
		}()
	}
	defer func() {
		if p := recover(); p != nil {
			result = status.Errorf(codes.Internal, "control dispatcher panic: %v", p)
		}
	}()
	authenticated := s.streamAuthenticated(stream.Context(), "")
	if authenticated {
		if err := claim(); err != nil {
			return err
		}
	}
	// Headers announce that the handler is running, not that Hello is accepted.
	// Send them before Recv so a peer awaiting headers can still send Hello.
	if err := stream.SendHeader(metadata.MD{}); err != nil {
		return err
	}
	first, err := stream.Recv()
	if err != nil {
		return err
	}
	if !authenticated {
		if !s.streamAuthenticated(stream.Context(), first.GetHello().GetSessionToken()) {
			return errBadToken
		}
		if err := claim(); err != nil {
			return err
		}
	}
	hello := first.GetHello()
	if hello == nil {
		return status.Error(codes.FailedPrecondition, "first host message must be Hello")
	}
	if !doorbell.CompatibleVersion(hello.ProtocolMajor, hello.ProtocolMinor) {
		return status.Errorf(codes.FailedPrecondition, "protocol mismatch: host %d.%d, Go SDK %d.%d", hello.ProtocolMajor, hello.ProtocolMinor, ProtocolMajor, ProtocolMinor)
	}
	identity := s.identity // composed and validated at construction
	if err := checkIdentity(first.Id, helloIdentityPrefix); err != nil {
		return status.Errorf(codes.FailedPrecondition, "Hello identity %s refused by %q: %v", identityPreview(first.Id), identity, err)
	}
	if err := ValidateSize(first); err != nil {
		return status.Error(codes.ResourceExhausted, err.Error())
	}
	readyMinor := ReadyMinor(hello.ProtocolMinor)
	if err := checkEngines(s.cfg.Engines, readyMinor); err != nil {
		return status.Error(codes.FailedPrecondition, err.Error())
	}
	ctx, cancel := context.WithCancelCause(stream.Context())
	defer cancel(ErrClosed)
	c := &Client{ctx: ctx, cancel: cancel, cfg: s.cfg, peerIdentity: first.Id, pluginIdentity: s.identity, buildInfo: s.buildInfo, peerHello: proto.Clone(hello).(*pb.Hello), readyMinor: readyMinor, out: make(chan outbound, s.cfg.QueueCapacity), waiters: make(map[string]*waiter), diagnostics: make(chan error, s.cfg.QueueCapacity)}
	stateCtx, stopState := context.WithCancel(ctx)
	defer stopState()
	shapes := newShapeRuntime(stateCtx, c)
	geometries := c.geometryWorker(stateCtx)
	appearances := c.appearanceWorker(stateCtx)
	defer func() {
		cancel(ErrClosed)
		c.endTasks(false)
		c.panelMu.Lock()
		clear(c.heldImages)
		c.panelMu.Unlock()
	}()
	c.afterReplySlots = make(chan struct{}, s.cfg.MaxPendingRequests)
	reporter := c.cfg.Reporter
	if reporter == nil {
		reporter, _ = violations.New(violations.Config{})
		defer reporter.Close()
	}
	c.cfg.Reporter = reporter
	c.stages = newStageRuntime(c)
	defer c.stages.cancel()
	var reportsAdmitted atomic.Uint64
	reportWake := make(chan struct{}, 1)
	go func() {
		err := reportViolations(ctx, c, reporter, func(count uint32) {
			reportsAdmitted.Add(uint64(count))
			select {
			case reportWake <- struct{}{}:
			default:
			}
		})
		if err != nil && ctx.Err() == nil && !errors.Is(err, ErrShuttingDown) {
			c.Fail(err)
		}
	}()
	reportsDrained := func() bool {
		var observed uint64
		for _, stats := range reporter.Stats() {
			observed += stats.Observed
		}
		return observed == reportsAdmitted.Load()
	}
	go func() {
		defer recoverFailure(c)
		for {
			select {
			case <-ctx.Done():
				return
			case err := <-c.diagnostics:
				c.cfg.Diagnostic(err)
			}
		}
	}()
	if s.cfg.Attach != nil {
		if err := s.cfg.Attach(c); err != nil {
			return err
		}
	}
	if err := s.cfg.Handshake(hello); err != nil {
		return status.Error(codes.Unauthenticated, err.Error())
	}
	if err := stream.Send(&pb.PluginMsg{Id: identity, Msg: &pb.PluginMsg_Ready{Ready: &pb.Ready{ProtocolMajor: ProtocolMajor, ProtocolMinor: readyMinor, PluginId: s.cfg.PluginID}}}); err != nil {
		return err
	}
	go func() {
		for {
			x, ok := c.nextOutbound()
			if !ok {
				return
			}
			var err error
			for _, m := range x.before {
				if !shapes.shouldSend(m) {
					continue
				}
				if err = stream.Send(m); err != nil {
					break
				}
			}
			if err == nil && x.msg != nil && shapes.shouldSend(x.msg) {
				err = stream.Send(x.msg)
				if err == nil && c.cfg.SentOutbound != nil {
					c.cfg.SentOutbound(x.msg)
				}
			}
			if x.sent != nil {
				x.sent <- err
			}
			if err != nil {
				c.Fail(err)
				return
			}
		}
	}()
	in := make(chan received, 1)
	go func() {
		for {
			m, err := stream.Recv()
			select {
			case in <- received{m, err}:
			case <-ctx.Done():
				return
			}
			if err != nil {
				return
			}
		}
	}()
	views := c.stateWorker(stateCtx, false)
	regions := c.stateWorker(stateCtx, true)
	requests := make(chan *pb.HostMsg, s.cfg.MaxPendingRequests)
	notifications := make(chan *pb.HostMsg, s.cfg.QueueCapacity)
	finished := make(chan struct{}, s.cfg.MaxPendingRequests+1)
	go c.requestWorker(requests, finished)
	go func() {
		defer recoverFailure(c)
		for {
			select {
			case <-ctx.Done():
				return
			case m := <-notifications:
				if opened := m.GetPanelOpened(); opened != nil {
					if err := c.flushPanelImages(opened.PanelId); err != nil {
						c.diagnostic(err)
					}
				}
				if ended := m.GetShapeEditEnded(); ended != nil {
					if c.cfg.ShapeEditEnded != nil {
						if err := c.cfg.ShapeEditEnded(ctx, c, ended); err != nil {
							c.diagnostic(err)
						}
					}
					continue
				}
				if c.cfg.OnMessage != nil {
					if err := c.cfg.OnMessage(ctx, c, m); err != nil {
						c.diagnostic(err)
					}
				} else {
					c.diagnostic(fmt.Errorf("unhandled host %s", kind(m.ProtoReflect())))
				}
			}
		}
	}()
	if c.cfg.Connected != nil {
		go func() { defer recoverFailure(c); c.cfg.Connected(c) }()
	}
	prepared := make(chan received, s.cfg.MaxPendingRequests)
	// Keep preparation/dispatch ordered within each segment while allowing
	// unrelated segments and control arrangements to make progress.
	preparationDone := make(map[*pb.HostMsg]chan struct{})
	segmentTail := make(map[uint64]*pb.HostMsg)
	preparing := 0
	active := 0
	var shutdownDone <-chan error
	var shutdownTimer <-chan time.Time
	var timer *time.Timer
	defer func() {
		if timer != nil {
			timer.Stop()
		}
	}()
	shutting, hookDone := false, false
	// The session's end for plugin requests, if Shutdown has not already
	// ended it: whichever way Serve returns (the host's stream end, a fault),
	// every request still pending fails now with SessionEnded, and every later
	// one fails unsent. Runs before the deferred cancel, so this reason wins.
	endReason := ""
	defer func() {
		if endReason == "" {
			endReason = c.causeReason()
		}
		if endReason == "" {
			endReason = "the session closed"
		}
		_ = c.endSession(endReason, "")
	}()
	outcome := &pb.ShutdownReport{Outcome: pb.ShutdownReport_NO_HOOK}
	finishHook := func(err error) {
		hookDone = true
		if c.cfg.Shutdown != nil {
			outcome.Outcome = pb.ShutdownReport_COMPLETED
		}
		if err != nil {
			outcome.Outcome = pb.ShutdownReport_RAISED
			outcome.Detail = err.Error()
			// A plugin error must not make the reserved diagnostic exceed the wire cap.
			if len(outcome.Detail) > 4096 {
				outcome.Detail = outcome.Detail[:4096]
				for !utf8.ValidString(outcome.Detail) {
					outcome.Detail = outcome.Detail[:len(outcome.Detail)-1]
				}
			}
			c.diagnostic(err)
		}
	}
	// The deadline remains authoritative. A small local final-send allowance
	// permits an overrun diagnostic, but never an unlimited wait on a stalled peer.
	overrun := func() error {
		c.endTasks(true)
		if !hookDone {
			select {
			case err := <-shutdownDone:
				finishHook(err)
			default:
				outcome.Outcome = pb.ShutdownReport_OVERRAN
			}
		}
		sent := c.sealWithReport(outcome)
		grace := time.NewTimer(50 * time.Millisecond)
		defer grace.Stop()
		select {
		case err := <-sent:
			return err
		case <-ctx.Done():
			return context.Cause(ctx)
		case <-grace.C:
			return nil
		}
	}

	var ids idLedger
	for {
		if shutting && hookDone && active == 0 && preparing == 0 && reportsDrained() {
			c.endTasks(true)
			// Seal admission after final handlers finish, then flush both queues
			// before returning from the RPC (the shutdown completion signal).
			sent := c.sealWithReport(outcome)
			select {
			case err := <-sent:
				return err
			case <-ctx.Done():
				return context.Cause(ctx)
			case <-shutdownTimer:
				return nil
			}
		}
		var m *pb.HostMsg
		isPrepared := false
		select {
		case <-ctx.Done():
			return context.Cause(ctx)
		case <-reportWake:
			continue // a report entered the output queue; recheck the shutdown barrier
		case <-shutdownTimer:
			c.diagnostic(errors.New("shutdown window elapsed"))
			return overrun()
		case err := <-shutdownDone:
			shutdownDone = nil
			finishHook(err)
			continue
		case <-finished:
			active--
			continue
		case r := <-prepared:
			preparing--
			m = r.msg
			delete(preparationDone, m)
			if d, _ := descriptor(m); d != nil && segmentTail[d.SegmentId] == m {
				delete(segmentTail, d.SegmentId)
			}
			isPrepared = true
			if r.err != nil {
				if HostClass(m) == Request {
					c.endWork(m.Id)
				}
				if s.cfg.Release != nil {
					if e := s.cfg.Release(c, m); e != nil {
						return e
					}
				}
				failureKind := pb.Error_INTERNAL
				if errors.Is(r.err, ErrDescriptorDropped) {
					failureKind = pb.Error_NOT_FOUND
				} else {
					c.diagnostic(r.err)
				}
				if HostClass(m) == Request {
					if err := c.enqueue(errorReply(m.Id, failureKind, r.err.Error()), nil); err != nil {
						return err
					}
				} else {
					_ = c.routeReply(&pb.HostMsg{Id: m.Id, Msg: &pb.HostMsg_HostError{HostError: &pb.Error{Kind: failureKind, Message: r.err.Error()}}})
				}
				continue
			}
		case r := <-in:
			if errors.Is(r.err, io.EOF) {
				if endReason == "" {
					endReason = "the host closed its side of the stream"
				}
				return nil
			}
			if r.err != nil {
				if endReason == "" {
					endReason = "the stream ended: " + r.err.Error()
				}
				return r.err
			}
			m = r.msg
			// Ids are never reused: a byte-identical re-send is dropped, and
			// the same id with different content faults the session.
			if duplicate, err := ids.observe(m); err != nil {
				c.diagnostic(err)
				return status.Error(codes.InvalidArgument, err.Error())
			} else if duplicate {
				// What a re-send names belongs to the first copy: nothing is
				// released for it.
				c.diagnostic(fmt.Errorf("re-sent message under id %q deduplicated", m.Id))
				continue
			}
			if err := ValidateSize(m); err != nil {
				var overflow *RepeatedFieldError
				if errors.As(err, &overflow) {
					if e := reporter.Observe(violations.Finding{Where: violations.RepeatedFieldOverflow, Detail: err.Error()}); e != nil {
						return e
					}
				} else {
					// Inline bytes/strings have no named violation site. Refuse
					// the message with PROTOCOL (§9), retaining the session.
					c.diagnostic(err)
				}
				if HostClass(m) == Request && m.Id != "" {
					if e := c.enqueue(errorReply(m.Id, pb.Error_PROTOCOL, err.Error()), nil); e != nil {
						return e
					}
				}
				if HostClass(m) == ReplyClass && m.Id != "" {
					_ = c.routeReply(&pb.HostMsg{Id: m.Id, Msg: &pb.HostMsg_HostError{HostError: &pb.Error{Kind: pb.Error_PROTOCOL, Message: err.Error()}}})
				}
				if c.cfg.Release != nil && hasDescriptor(m) {
					if e := c.cfg.Release(c, m); e != nil {
						c.diagnostic(e)
					}
				}
				continue
			}
		}
		k := kind(m.ProtoReflect())
		class := HostClass(m) // Always BEFORE looking at the id.
		if class == Unknown {
			if err := c.routeUnknown(m); err != nil {
				var fault *ErrWrongReplyKind
				if errors.As(err, &fault) {
					// Not the reply under a pending request's id: the waiter's
					// call fails with the fault, and so does the stream.
					c.diagnostic(err)
					c.Fail(err)
					return status.Error(codes.InvalidArgument, err.Error())
				}
				return err
			}
			continue
		}
		if class == Handshake {
			return status.Error(codes.FailedPrecondition, "repeated Hello")
		}
		if class == Request && m.Id == "" {
			return status.Error(codes.InvalidArgument, "host request has empty id")
		}
		if shutting && class == Request && !isPrepared {
			c.queueMu.Lock()
			c.queueStats.RefusedInTeardown++
			c.queueMu.Unlock()
			if err := c.enqueue(errorReply(m.Id, pb.Error_PROTOCOL, "session is shutting down"), nil); err != nil {
				return err
			}
			if c.cfg.Release != nil {
				if err := c.cfg.Release(c, m); err != nil {
					c.diagnostic(err)
				}
			}
			continue
		}
		if !isPrepared {
			if class == Request {
				if err := c.beginWork(m.Id); err != nil {
					return err
				}
			}
			if err := c.discardInlineInstall(m); err != nil {
				return err
			}
		}
		if !isPrepared && hasDescriptor(m) {
			if c.cfg.Prepare == nil || c.cfg.Release == nil {
				return status.Error(codes.Unimplemented, "descriptor traffic requires Prepare and Release integration")
			}
			if preparing >= s.cfg.MaxPendingRequests {
				return fmt.Errorf("descriptor preparation backlog exceeds %d pending messages: %w", s.cfg.MaxPendingRequests, ErrQueueFull)
			}
			preparing++
			var prior <-chan struct{}
			done := make(chan struct{})
			preparationDone[m] = done
			if d, _ := descriptor(m); d != nil {
				prior = preparationDone[segmentTail[d.SegmentId]]
				segmentTail[d.SegmentId] = m
			}
			var admissionErr error
			if c.cfg.Admit != nil {
				admissionErr = c.cfg.Admit(c, m)
			}
			go func(m *pb.HostMsg) {
				defer recoverFailure(c)
				defer close(done)
				if prior != nil {
					select {
					case <-prior:
					case <-ctx.Done():
						return
					}
				}
				err := admissionErr
				if err == nil {
					err = c.cfg.Prepare(ctx, c, m)
				}
				select {
				case prepared <- received{m, err}:
				case <-ctx.Done():
				}
			}(m)
			continue
		}
		switch class {
		case HostCoalescing:
			if shutting {
				continue
			}
			if edit := m.GetShapeDrag(); edit != nil {
				shapes.drag(edit)
				continue
			}
			if m.GetViewChanged() != nil {
				views.put(m)
				continue
			}
			if g := m.GetWidgetGeometry(); g != nil {
				geometries.put(g)
				continue
			}
			if a := m.GetAppearance(); a != nil {
				appearances.put(a)
				continue
			}
			if m.GetRegionsChanged() != nil {
				regions.put(m)
				continue
			}
			q := m.GetChunksWanted()
			if len(q.GetWants()) > 8 {
				c.diagnostic(fmt.Errorf("ChunksWanted exceeds host bound: %d wants", len(q.Wants)))
			}
			if c.cfg.ChunksWanted != nil {
				if err := c.cfg.ChunksWanted(c, q); err != nil {
					c.diagnostic(err)
				}
			} else {
				c.diagnostic(errors.New("unhandled ChunksWanted"))
			}
		case ReplyClass:
			// An unsolicited host_error is a diagnostic, never a lost reply.
			if k == "host_error" && m.Id == "" {
				c.diagnostic(&HostError{Kind: m.GetHostError().Kind, Message: m.GetHostError().Message})
			} else if err := c.routeReply(m); err != nil {
				// A known reply of the wrong kind under a pending request's id:
				// the waiter's call fails with the fault, and so does the stream.
				c.diagnostic(err)
				c.Fail(err)
				return status.Error(codes.InvalidArgument, err.Error())
			}
		case Request:
			select {
			case requests <- m:
				active++
			default:
				return ErrQueueFull
			}
		default:
			if k == "shape_commit" {
				shapes.commit(m.GetShapeCommit())
				continue
			}
			if k == "shape_edit_ended" {
				shapes.end(m.GetShapeEditEnded().DragId)
				if c.cfg.ShapeEditEnded != nil {
					select {
					case notifications <- m:
					default:
						return ErrQueueFull
					}
				}
				continue
			}
			if k == "cancel_work" {
				c.cancelWork(m.GetCancelWork())
				continue
			}
			if k == "shutdown" {
				if shutting {
					c.diagnostic(errors.New("repeated Shutdown ignored; first deadline retained"))
					continue
				}
				shutting = true
				stopState()
				c.mu.Lock()
				c.closing = true
				c.mu.Unlock()
				sh := m.GetShutdown()
				// §3.3: the host answers no request in its window, so every
				// plugin request still pending fails now, before the hook runs.
				endReason = "shutdown began (" + sh.GetReason().String() + "): the host answers no request in its window"
				_ = c.endSession(endReason, "")
				deadline := sh.GetDeadlineMs()
				if sh.DeadlineMs == nil {
					deadline = 5000
					if err := c.reportViolation(violations.MissingDeadline, "Shutdown omitted deadline_ms; using 5 s"); err != nil {
						return err
					}
				}
				timer = time.NewTimer(time.Duration(deadline) * time.Millisecond)
				shutdownTimer = timer.C
				c.stages.cancel()
				done := make(chan error, 1)
				shutdownDone = done
				go func() {
					var hookErr error
					defer func() {
						if p := recover(); p != nil {
							hookErr = fmt.Errorf("shutdown panic: %v", p)
						}
						<-c.stages.done
						done <- hookErr
					}()
					if c.cfg.Shutdown != nil {
						hookErr = c.cfg.Shutdown(ctx, c, sh)
					}
				}()
				continue
			}
			if (k == "follow_started" || k == "follow_ended") && c.cfg.FollowTransition != nil {
				if err := c.cfg.FollowTransition(c, m); err != nil {
					c.diagnostic(err)
				}
				continue
			}
			if k == "panel_opened" || k == "contract_violation" || k == "follow_started" || k == "follow_ended" {
				select {
				case notifications <- m:
				default:
					return ErrQueueFull
				}
			} else if k == "stage_arm" {
				if err := c.stages.arm(m.GetStageArm()); err != nil {
					return err
				}
			} else if c.cfg.Arrange != nil {
				if err := c.cfg.Arrange(c, m); err != nil {
					c.diagnostic(err)
				}
			} else {
				return status.Errorf(codes.Unimplemented, "%s requires arrangement integration", k)
			}
		}
	}
}

func recoverFailure(c *Client) {
	if p := recover(); p != nil {
		c.Fail(fmt.Errorf("SDK callback panic: %v", p))
	}
}
func errorReply(id string, k pb.Error_Kind, detail string) *pb.PluginMsg {
	return &pb.PluginMsg{Id: id, Msg: &pb.PluginMsg_Error{Error: &pb.Error{Kind: k, Message: detail}}}
}

// commandFailed is the failed CommandDone for an InvokeCommand.  Every
// CommandDone echoes the invoked command's id -- on success, on failure, and
// on an argument-validation failure alike (§8.3; a CommandDone without it is
// the sender's protocol error).
func commandFailed(m *pb.HostMsg, message string) *pb.PluginMsg {
	return &pb.PluginMsg{Id: m.Id, Msg: &pb.PluginMsg_CommandDone{CommandDone: &pb.CommandDone{CommandId: m.GetInvokeCommand().GetCommandId(), Ok: false, Message: message}}}
}

// UndeclaredCommand is what a HandleRequest returns for an InvokeCommand
// naming a command the plugin does not declare.  The session answers it
// Error{NOT_FOUND}, not a CommandDone: there is no command whose outcome it
// could be.  A declared command whose
// handler fails, panics or is missing is a CommandDone{ok: false}, and a
// canceled one Error{CANCELLED}.
type UndeclaredCommand struct{ CommandID string }

func (e *UndeclaredCommand) Error() string {
	return `command "` + e.CommandID + `" is not one this plugin declares`
}

func diagnosticMessage(detail string) *pb.PluginMsg { return errorReply("", pb.Error_INTERNAL, detail) }

func hasDescriptor(m *pb.HostMsg) bool {
	// Empty/hidden inclusions intentionally have no deposit to prepare.
	return m.GetDataVolume() != nil || m.GetMaskVoxels() != nil ||
		m.GetRoiInclusion().GetSegment() != nil ||
		(m.GetExportChunk() != nil && (m.GetExportChunk().GetSegment() != nil || m.GetExportChunk().Install != nil))
}

func (c *Client) requestWorker(queue <-chan *pb.HostMsg, finished chan<- struct{}) {
	defer recoverFailure(c)
	for {
		select {
		case <-c.ctx.Done():
			return
		case m := <-queue:
			if c.ctx.Err() != nil {
				return
			}
			scope := &afterReplyScope{client: c}
			ctx := context.WithValue(c.ctx, afterReplyKey{}, scope)
			w := c.requestWork(m.Id)
			if w != nil {
				ctx = context.WithValue(ctx, workKey{}, w)
			}
			var out *pb.PluginMsg
			if Cancelled(ctx) {
				out = errorReply(m.Id, pb.Error_CANCELLED, "canceled before it started")
			} else {
				out = c.handle(ctx, m)
			}
			c.endWork(m.Id)
			work := scope.finish()
			if work != nil && !successfulReply(out) {
				<-c.afterReplySlots
				work = nil
			}
			if c.cfg.Release != nil {
				if err := c.cfg.Release(c, m); err != nil {
					if work != nil {
						<-c.afterReplySlots
					}
					c.diagnostic(err)
					c.Fail(err)
					return
				}
			}
			var sent chan error
			if work != nil {
				sent = make(chan error, 1)
			}
			if err := c.enqueue(out, sent); err != nil {
				if work != nil {
					<-c.afterReplySlots
				}
				c.Fail(err)
				return
			}
			if work != nil {
				go c.continueAfterReply(sent, work)
			}
			select {
			case finished <- struct{}{}:
			case <-c.ctx.Done():
				return
			}
		}
	}
}

func (c *Client) handle(ctx context.Context, m *pb.HostMsg) (out *pb.PluginMsg) {
	defer func() {
		if p := recover(); p != nil {
			if stopped, ok := p.(workCancellation); ok {
				out = errorReply(m.Id, pb.Error_CANCELLED, stopped.message)
			} else if m.GetInvokeCommand() != nil {
				out = commandFailed(m, fmt.Sprintf("handler panic: %v", p))
			} else {
				out = errorReply(m.Id, pb.Error_INTERNAL, fmt.Sprintf("handler panic: %v", p))
			}
		}
		if w, _ := ctx.Value(workKey{}).(*requestWork); w != nil {
			if message := w.stopped.Load(); message != nil {
				out = errorReply(m.Id, pb.Error_CANCELLED, *message)
			}
		}
	}()
	if c.cfg.HandleRequest == nil {
		return errorReply(m.Id, pb.Error_UNSUPPORTED, "no handler registered for "+kind(m.ProtoReflect()))
	}
	// Snapshot before author callbacks can mutate the request. Provenance uses
	// wire handles, including those wrapped by the typed exporter router.
	openingExport := m.GetOpenExport() != nil
	filteredExport := m.GetOpenExport().GetFilter() != nil
	if err := c.checkExport(m); err != nil {
		return errorReply(m.Id, pb.Error_INVALID_ARGUMENT, err.Error())
	}
	var stageRequest *pb.PlanStage
	if q := m.GetPlanStage(); q != nil {
		stageRequest = proto.Clone(q).(*pb.PlanStage)
		if c.cfg.Stages != nil {
			if _, declared := c.cfg.Stages[q.StageId]; !declared {
				return errorReply(m.Id, pb.Error_NOT_FOUND, "undeclared processing stage: "+q.StageId)
			}
		}
		if err := stage.ValidateShape(stageRequest.Input); err != nil {
			if errors.Is(err, stage.ErrUnknownDType) {
				// A later minor's dtype: this plan alone is refused.
				c.diagnostic(fmt.Errorf("PlanStage %q refused: input %w", q.StageId, err))
			}
			return errorReply(m.Id, stage.ShapeErrorKind(err), "input: "+err.Error())
		}
		if _, err := stage.CheckParams(c.cfg.Stages[q.StageId].Params, stageRequest.Params); err != nil {
			return errorReply(m.Id, pb.Error_INVALID_ARGUMENT, err.Error())
		}
	}
	out, err := c.cfg.HandleRequest(ctx, c, m)
	if err != nil {
		if isWorkCancelled(err) {
			return errorReply(m.Id, pb.Error_CANCELLED, err.Error())
		}
		var undeclared *UndeclaredCommand
		if m.GetInvokeCommand() != nil && errors.As(err, &undeclared) {
			return errorReply(m.Id, pb.Error_NOT_FOUND, undeclared.Error())
		}
		if m.GetInvokeCommand() != nil {
			return commandFailed(m, err.Error())
		}
		var e *HostError
		if errors.As(err, &e) {
			return errorReply(m.Id, e.Kind, e.Message)
		}
		return errorReply(m.Id, pb.Error_INTERNAL, err.Error())
	}
	if out == nil {
		return errorReply(m.Id, pb.Error_INTERNAL, "handler returned no reply")
	}
	k := kind(out.ProtoReflect())
	if k != "error" && k != hostRoutes[kind(m.ProtoReflect())].reply && !(m.GetOpenReader() != nil && k == "series_list") {
		return errorReply(m.Id, pb.Error_INTERNAL, "handler returned wrong reply kind: "+k)
	}
	out = proto.Clone(out).(*pb.PluginMsg)
	out.Id = m.Id
	if done := out.GetCommandDone(); done != nil {
		// §8.3: CommandDone echoes the invoked command's id, always.
		done.CommandId = m.GetInvokeCommand().GetCommandId()
	}
	if err := c.validateOutbound(out); err != nil {
		return errorReply(m.Id, pb.Error_INTERNAL, err.Error())
	}
	if openingExport && out.GetExportOpened() != nil {
		if c.exports == nil {
			c.exports = make(map[string]exportState)
		}
		c.exports[out.GetExportOpened().Handle] = exportState{filtered: filteredExport}
	}
	if stageRequest != nil && out.GetStagePlanned() != nil && c.stages != nil {
		if err := c.stages.record(stageRequest, out.GetStagePlanned()); err != nil {
			return errorReply(m.Id, stage.PlanErrorKind(err), err.Error())
		}
	}
	return out
}
