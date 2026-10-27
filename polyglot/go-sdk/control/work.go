package control

import (
	"context"
	"errors"
	"fmt"
	"math"
	"strings"
	"sync/atomic"
	"unicode/utf8"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

const MaxOpenTasks = 64
const MaxWorkTextBytes = 256
const workCoalescingSlack = MaxOpenTasks + 4

// ErrCancelled is returned by a handler that cooperatively stops its work.
// Wrap it with %w to add an outcome. The SDK recognizes it before ordinary
// command-failure conversion and answers Error{CANCELLED}, without diagnostics.
// CancelWork only sets a flag; ignoring it and returning normally is permitted.
var ErrCancelled = errors.New("canceled")

type workKey struct{}
type requestWork struct {
	client    *Client
	id        string
	cancelled atomic.Bool
	stopped   atomic.Pointer[string]
	ended     bool // guarded by client.workMu
}

// StopWork unwinds the current handler and answers CANCELLED with message. Its
// private panic value is deliberately not an error, so ordinary error handling
// cannot mistake it for failure. Deferred cleanup runs normally. The outcome is
// also recorded on ctx, so even a broad recovery wrapper cannot turn it into an
// ordinary failure. Call only on the goroutine serving this request; background
// workers use their TaskHandle and return ErrCancelled instead.
func StopWork(ctx context.Context, message string) {
	if w, _ := ctx.Value(workKey{}).(*requestWork); w != nil {
		w.stopped.Store(&message)
	}
	panic(workCancellation{message: message})
}

type workCancellation struct{ message string }

// Cancelled reports whether the host asked this request to stop. Outside a
// request handler it is false. Session teardown is separately ctx.Done().
func Cancelled(ctx context.Context) bool {
	w, _ := ctx.Value(workKey{}).(*requestWork)
	return w != nil && w.cancelled.Load()
}

// ReportProgress reports on the current request, without blocking on network
// I/O. nil means indeterminate; finite fractions are clamped to [0,1], nonfinite
// ones become zero. Outside a handler (or after its reply) this is a no-op.
func ReportProgress(ctx context.Context, fraction *float64, text string) error {
	w, _ := ctx.Value(workKey{}).(*requestWork)
	if w == nil {
		return nil
	}
	w.client.workMu.Lock()
	defer w.client.workMu.Unlock()
	if w.ended {
		return nil
	}
	return w.client.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_WorkProgress{WorkProgress: &pb.WorkProgress{Subject: &pb.WorkProgress_RequestId{RequestId: w.id}, Progress: workProgress(fraction), Text: clipWorkText(text)}}})
}

func workProgress(fraction *float64) *pb.Progress {
	if fraction == nil {
		return &pb.Progress{Indeterminate: true}
	}
	v := *fraction
	if math.IsNaN(v) || math.IsInf(v, 0) {
		v = 0
	}
	return &pb.Progress{Value: min(1, max(0, v))}
}
func clipWorkText(text string) string {
	text = strings.ToValidUTF8(text, "�")
	if len(text) <= MaxWorkTextBytes {
		return text
	}
	text = text[:MaxWorkTextBytes]
	for !utf8.ValidString(text) {
		text = text[:len(text)-1]
	}
	return text
}

func (c *Client) beginWork(id string) error {
	c.workMu.Lock()
	defer c.workMu.Unlock()
	if c.requests == nil {
		c.requests = make(map[string]*requestWork)
	}
	if c.requests[id] != nil {
		return fmt.Errorf("duplicate active host request id %q", id)
	}
	c.requests[id] = &requestWork{client: c, id: id}
	return nil
}
func (c *Client) requestWork(id string) *requestWork {
	c.workMu.Lock()
	defer c.workMu.Unlock()
	return c.requests[id]
}
func (c *Client) endWork(id string) {
	c.workMu.Lock()
	defer c.workMu.Unlock()
	if w := c.requests[id]; w != nil {
		w.ended = true
		delete(c.requests, id)
	}
}
func (c *Client) cancelWork(q *pb.CancelWork) bool {
	c.workMu.Lock()
	defer c.workMu.Unlock()
	switch s := q.GetSubject().(type) {
	case *pb.CancelWork_RequestId:
		if w := c.requests[s.RequestId]; w != nil {
			w.cancelled.Store(true)
			return true
		}
	case *pb.CancelWork_TaskId:
		if t := c.tasks[s.TaskId]; t != nil {
			t.cancelled.Store(true)
			return true
		}
	}
	return false
}

// TaskHandle tracks background work; it does not start a goroutine. Methods are
// concurrency-safe. The application owns the worker and must stop it on session
// context cancellation. Done/Finish ends reporting, not the worker itself.
type TaskHandle struct {
	client      *Client
	id, title   string
	cancellable bool
	cancelled   atomic.Bool
	ended       bool // guarded by client.workMu
}

func (t *TaskHandle) ID() string      { return t.id }
func (t *TaskHandle) Cancelled() bool { return t.cancelled.Load() }

// StartTask begins a task, cancellable by default. An optional false hides the
// host's Cancel action. At most 64 tasks may be open; Done makes room for another.
// Cancellable is UI advice: a CancelWork still sets the flag when false, without
// refusal or diagnostics. The task may ignore the flag and complete normally.
func (c *Client) StartTask(title string, cancellable ...bool) (*TaskHandle, error) {
	if len(cancellable) > 1 {
		return nil, errors.New("StartTask accepts at most one cancellable flag")
	}
	c.workMu.Lock()
	defer c.workMu.Unlock()
	c.mu.Lock()
	closing := c.closing
	c.mu.Unlock()
	if closing || c.workEnded {
		return nil, ErrShuttingDown
	}
	if err := c.ctx.Err(); err != nil {
		return nil, context.Cause(c.ctx)
	}
	if len(c.tasks) >= MaxOpenTasks {
		return nil, fmt.Errorf("%d background tasks are already open; end one first", MaxOpenTasks)
	}
	if c.nextTask == math.MaxUint64 {
		return nil, errors.New("task id space exhausted")
	}
	c.nextTask++
	task := &TaskHandle{client: c, id: fmt.Sprintf("t%d", c.nextTask), title: clipWorkText(title), cancellable: len(cancellable) == 0 || cancellable[0]}
	if err := task.send(workProgress(nil), "", false); err != nil {
		return nil, err
	}
	if c.tasks == nil {
		c.tasks = make(map[string]*TaskHandle)
	}
	c.tasks[task.id] = task
	return task, nil
}
func (t *TaskHandle) send(progress *pb.Progress, text string, done bool) error {
	return t.client.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_WorkProgress{WorkProgress: &pb.WorkProgress{Subject: &pb.WorkProgress_TaskId{TaskId: t.id}, Title: t.title, Progress: progress, Text: clipWorkText(text), Done: done, Cancellable: t.cancellable}}})
}
func (t *TaskHandle) Progress(fraction *float64, text string) error {
	t.client.workMu.Lock()
	defer t.client.workMu.Unlock()
	if t.ended {
		return nil
	}
	return t.send(workProgress(fraction), text, false)
}

// Done reports the outcome and ends the task. Repeated calls do nothing. An
// admission failure leaves it open so the application can retry.
func (t *TaskHandle) Done(message string) error {
	t.client.workMu.Lock()
	defer t.client.workMu.Unlock()
	if t.ended {
		return nil
	}
	if err := t.send(nil, message, true); err != nil {
		return err
	}
	t.ended = true
	delete(t.client.tasks, t.id)
	return nil
}

// Finish is the deferred-cleanup counterpart of Done: nil is success,
// ErrCancelled is "canceled", any other error is "failed: <message>".
// Use defer func() { _ = task.Finish(err) }() with a named worker error result.
func (t *TaskHandle) Finish(err error) error {
	message := ""
	if isWorkCancelled(err) {
		message = "canceled"
	} else if err != nil {
		message = "failed: " + err.Error()
	}
	return t.Done(message)
}
func isWorkCancelled(err error) bool {
	var e *HostError
	return errors.Is(err, ErrCancelled) || (errors.As(err, &e) && e.Kind == pb.Error_CANCELLED)
}

// Before sealing a graceful shutdown, end task reports; on an abrupt stream
// end just retire local handles. No user callbacks run under this lock.
func (c *Client) endTasks(report bool) {
	c.workMu.Lock()
	defer c.workMu.Unlock()
	c.workEnded = true
	for id, t := range c.tasks {
		t.cancelled.Store(true)
		if report {
			_ = t.send(nil, "session ended", true)
		}
		t.ended = true
		delete(c.tasks, id)
	}
	if !report {
		for id, w := range c.requests {
			w.cancelled.Store(true)
			w.ended = true
			delete(c.requests, id)
		}
	}
}

// ShutdownReasonName uses the protocol's lowercase spelling, retaining unknown
// numeric values. USER_STOPPED follows the same shutdown path as NOT_NEEDED.
func ShutdownReasonName(reason pb.ShutdownReason) string {
	return strings.ToLower(strings.TrimPrefix(reason.String(), "SHUTDOWN_REASON_"))
}
