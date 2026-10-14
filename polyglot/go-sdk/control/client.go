package control

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

var ErrClosed = errors.New("control session is closed")
var ErrShuttingDown = errors.New("control session is shutting down")
var ErrQueueFull = errors.New("control session queue is full")

// ErrUnsupportedReply means a newer host replied with a message kind this SDK
// cannot decode. Only that call fails; the session remains usable.
var ErrUnsupportedReply = errors.New("unsupported host reply kind")

type HostError struct {
	Kind    pb.Error_Kind
	Message string
}

func (e *HostError) Error() string { return e.Kind.String() + ": " + e.Message }

type outbound struct {
	msg    *pb.PluginMsg
	sent   chan error
	before []*pb.PluginMsg
}
type waiter struct {
	expected string
	result   chan *Reply
}

// Reply owns any host reservation its Message names. Close it on EVERY path,
// including errors while decoding/copying its payload. Close is idempotent and
// must follow the final shared-memory read; message fields are read-only.
type Reply struct {
	Message *pb.HostMsg
	once    sync.Once
	release func() error
	err     error
}

func (r *Reply) Close() error {
	if r == nil {
		return nil
	}
	r.once.Do(func() {
		if r.release != nil {
			r.err = r.release()
		}
	})
	return r.err
}

// Client belongs to one stream. Send and Call are concurrency-safe; accepted
// messages are cloned, and only the session writer calls gRPC Send.
type Client struct {
	workMu               sync.Mutex
	requests             map[string]*requestWork
	tasks                map[string]*TaskHandle
	nextTask             uint64
	workEnded            bool
	shapeCoalescingCount int
	workCoalescingCount  int // subset of coalescingCount, with its own slack

	panelMu         sync.Mutex
	heldImages      map[PanelImageKey]heldPanelImage
	stages          *stageRuntime
	exports         map[string]exportState // owned by the serial request worker
	ctx             context.Context
	cancel          context.CancelCauseFunc
	cfg             Config
	peerHello       *pb.Hello // immutable snapshot, recorded before lifecycle hooks
	peerIdentity    string
	pluginIdentity  string
	buildInfo       string
	out             chan outbound
	queueMu         sync.Mutex
	wake            chan struct{}
	pending         map[pushKey]*pb.PluginMsg
	order           []pushKey
	coalescingCount int // includes snapshots waiting in out
	queueStats      QueueStats
	sealed          bool
	barrier         chan error
	finalReport     *pb.PluginMsg
	mu              sync.Mutex
	waiters         map[string]*waiter
	unmatchedCount  uint64 // guarded by mu
	next            uint64
	closing         bool
	diagnostics     chan error
	afterReplySlots chan struct{}
}

func (c *Client) Context() context.Context { return c.ctx }

// UnmatchedReplies returns the cumulative number of replies received without a
// waiting caller in this session. These may be late, duplicate or unknown-ID
// replies; the counter does not distinguish causes or imply a protocol fault.
// It is safe to call concurrently. Counting does not emit diagnostics or change
// descriptor cleanup, and the count is not reset by reading it.
func (c *Client) UnmatchedReplies() uint64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.unmatchedCount
}

// Diagnose submits an application worker error to the session's diagnostic sink.
func (c *Client) Diagnose(err error) {
	if err != nil {
		c.diagnostic(err)
	}
}
func (c *Client) Fail(err error) {
	if err != nil {
		c.cancel(err)
	}
}

func (c *Client) enqueue(m *pb.PluginMsg, sent chan error) error {
	if err := c.validateOutbound(m); err != nil {
		return err
	}
	return c.enqueueOrdered(m, sent, true)
}

// Send admits a push for reliable delivery without waiting on network I/O.
// One-shot overflow faults the session. Descriptor-free coalescing pushes merge
// separately by key. Other coalescing admission failures leave the session alive
// and consume no new uses; a failed Send has accepted none of the message.
// Loans named by earlier accepted messages remain the host's to release.
func (c *Client) Send(m *pb.PluginMsg) error {
	if m.GetShapeProposal() != nil || m.GetShapeAnswer() != nil {
		return errors.New("shape results are sent by registered Shapers, not Send")
	}
	if patch := m.GetPanelPatch(); patch != nil {
		c.panelMu.Lock()
		defer c.panelMu.Unlock()
		err := c.send(m)
		if err == nil {
			c.prunePanelImages(patch)
		}
		return err
	}
	return c.send(m)
}

func (c *Client) send(m *pb.PluginMsg) error {
	if m == nil {
		return errors.New("nil push")
	}
	k := kind(m.ProtoReflect())
	class := pluginRoutes[k].class
	if class != Push && class != CoalescingPush {
		return fmt.Errorf("%s is not a push; use Call for requests", k)
	}
	if m.Id != "" && k != "grant_state" {
		return errors.New("unsolicited push must have an empty id")
	}
	if class == CoalescingPush {
		return c.enqueueCoalescing(m)
	}
	return c.enqueue(m, nil)
}

// Call allocates an id and waits independently of host requests and handlers.
// Config.RequestTimeout defaults to 30 seconds. RequestAsset and RequestSavePath
// instead use Config.PickerTimeout, a 30-minute dead-session backstop for requests
// answered by a person, not a work budget. An earlier caller deadline wins. A timeout
// removes its waiter atomically with reply delivery, and a late reply is released.
func (c *Client) Call(ctx context.Context, m *pb.PluginMsg) (*Reply, error) {
	return c.call(ctx, m, nil, nil)
}

// CallWithAdmission also reports whether the request entered the outbound queue.
// A true result transfers every declared loan use to the host even if the call
// subsequently times out or returns a host error. Only a false result permits
// local rollback of freshly reserved, never-previously-admitted loans. The flag
// is historical: it stays true if the host has already released the bytes.
func (c *Client) CallWithAdmission(ctx context.Context, m *pb.PluginMsg) (reply *Reply, admitted bool, err error) {
	reply, err = c.call(ctx, m, &admitted, nil)
	return
}

// CallTracked calls onAdmitted with the SDK-assigned correlation ID after queue
// admission, before waiting for the reply. The callback runs synchronously and
// must return promptly; hand the ID to another goroutine to query RequestStatus.
// Admission is not evidence the host has received the request yet. The usual
// Call ownership and timeout rules apply; caller-supplied message IDs are ignored.
func (c *Client) CallTracked(ctx context.Context, m *pb.PluginMsg, onAdmitted func(string)) (*Reply, error) {
	return c.call(ctx, m, nil, onAdmitted)
}

func (c *Client) call(ctx context.Context, m *pb.PluginMsg, admitted *bool, onAdmitted func(string)) (*Reply, error) {
	if m == nil {
		return nil, errors.New("nil request")
	}
	k := kind(m.ProtoReflect())
	route := pluginRoutes[k]
	if route.class != Request {
		return nil, fmt.Errorf("%s is not a plugin request", k)
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	timeout := c.cfg.RequestTimeout
	if k == "request_asset" || k == "request_save_path" {
		timeout = c.cfg.PickerTimeout
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	c.mu.Lock()
	if c.closing {
		c.mu.Unlock()
		return nil, ErrShuttingDown
	}
	if len(c.waiters) >= c.cfg.MaxPendingRequests {
		c.mu.Unlock()
		return nil, ErrQueueFull
	}
	c.next++
	if c.next == 0 {
		c.mu.Unlock()
		c.Fail(errors.New("request id space exhausted"))
		return nil, ErrClosed
	}
	id := fmt.Sprintf("q%d", c.next)
	w := &waiter{route.reply, make(chan *Reply, 1)}
	c.waiters[id] = w
	c.mu.Unlock()
	defer c.abandon(id, w)
	req := proto.Clone(m).(*pb.PluginMsg)
	req.Id = id
	if err := c.enqueue(req, nil); err != nil {
		return nil, err
	}
	if admitted != nil {
		*admitted = true
	}
	if onAdmitted != nil {
		onAdmitted(id)
	}
	select {
	case reply := <-w.result:
		if HostClass(reply.Message) == Unknown {
			_ = reply.Close()
			return nil, ErrUnsupportedReply
		}
		if e := reply.Message.GetHostError(); e != nil {
			_ = reply.Close()
			return nil, &HostError{e.Kind, e.Message}
		}
		return reply, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-c.ctx.Done():
		return nil, context.Cause(c.ctx)
	}
}

func (c *Client) abandon(id string, w *waiter) {
	c.mu.Lock()
	delete(c.waiters, id)
	var r *Reply
	select {
	case r = <-w.result:
	default:
	}
	c.mu.Unlock()
	if r != nil {
		c.release(r)
	}
}

func (c *Client) release(r *Reply) {
	if err := r.Close(); err != nil {
		c.diagnostic(err)
		c.Fail(err)
	}
}

func (c *Client) routeReply(m *pb.HostMsg) {
	r := &Reply{Message: m}
	if c.cfg.Release != nil {
		r.release = func() error { return c.cfg.Release(c, m) }
	}
	c.mu.Lock()
	w := c.waiters[m.Id]
	if w != nil {
		k := kind(m.ProtoReflect())
		if k != w.expected && k != "host_error" {
			c.mu.Unlock()
			c.release(r)
			c.Fail(fmt.Errorf("reply %s for %s, expected %s", k, m.Id, w.expected))
			return
		}
		delete(c.waiters, m.Id)
		w.result <- r
	} else {
		c.unmatchedCount++
	}
	c.mu.Unlock()
	if w == nil {
		// A timed-out caller may no longer care; descriptor ownership still ends here.
		c.release(r)
	}
}

// Unknown kinds are the sole exception to classification before correlation:
// a live waiter identifies a reply; any other nonempty id identifies a request.
// Match and deliver under the same lock used by cancellation, without counting
// unknown requests as unmatched replies or faulting on an unrecognized kind.
func (c *Client) routeUnknown(m *pb.HostMsg) error {
	if m.Id == "" {
		c.diagnostic(errors.New("unknown HostMsg push skipped"))
		return nil
	}
	c.mu.Lock()
	w := c.waiters[m.Id]
	if w != nil {
		delete(c.waiters, m.Id)
		w.result <- &Reply{Message: m}
	}
	c.mu.Unlock()
	if w != nil {
		return nil
	}
	return c.enqueue(errorReply(m.Id, pb.Error_UNSUPPORTED, "unknown HostMsg kind"), nil)
}

func (c *Client) diagnostic(err error) {
	// A descriptor overtaken by normal retirement has already been disposed.
	if errors.Is(err, ErrDescriptorDropped) {
		return
	}
	if c.diagnostics == nil {
		return
	}
	select {
	case c.diagnostics <- err:
	default:
		c.Fail(fmt.Errorf("diagnostic consumer stopped draining: %w", ErrQueueFull))
	}
}

// Keep the default in one place; this is the plugin-owned timeout in §9.
const DefaultRequestTimeout = 30 * time.Second

// DefaultPickerTimeout is the dead-session backstop for requests answered by a
// person (RequestAsset and RequestSavePath), not a work budget.
const DefaultPickerTimeout = 30 * time.Minute
