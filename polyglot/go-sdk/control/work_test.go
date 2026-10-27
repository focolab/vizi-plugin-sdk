package control

import (
	"context"
	"errors"
	"fmt"
	"io"
	"math"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
	"unicode/utf8"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

func waitWork(t *testing.T, ctx context.Context, predicate func() bool) {
	t.Helper()
	ticker := time.NewTicker(time.Millisecond)
	defer ticker.Stop()
	for !predicate() {
		select {
		case <-ctx.Done():
			t.Fatal(ctx.Err())
		case <-ticker.C:
		}
	}
}
func invokeWork(id string) *pb.HostMsg {
	return &pb.HostMsg{Id: id, Msg: &pb.HostMsg_InvokeCommand{InvokeCommand: &pb.InvokeCommand{CommandId: "slow"}}}
}
func cancelRequest(id string) *pb.HostMsg {
	return &pb.HostMsg{Msg: &pb.HostMsg_CancelWork{CancelWork: &pb.CancelWork{Subject: &pb.CancelWork_RequestId{RequestId: id}}}}
}
func commandDone() *pb.PluginMsg {
	return &pb.PluginMsg{Msg: &pb.PluginMsg_CommandDone{CommandDone: &pb.CommandDone{Ok: true, Message: "done"}}}
}

func TestWorkCommandProgressAndNoHandlerDeadline(t *testing.T) {
	step, release := make(chan struct{}), make(chan struct{})
	contexts := make(chan context.Context, 1)
	p := openPeer(t, Config{RequestTimeout: time.Nanosecond, HandleRequest: func(ctx context.Context, c *Client, m *pb.HostMsg) (*pb.PluginMsg, error) {
		contexts <- ctx
		// The test host puts a five-second deadline on the entire gRPC
		// stream. The SDK's nanosecond outgoing timeout must not replace it.
		if deadline, ok := ctx.Deadline(); ok && time.Until(deadline) < time.Second {
			t.Error("handler inherited the SDK's outgoing request deadline")
		}
		if err := ReportProgress(ctx, proto.Float64(.25), "a quarter"); err != nil {
			return nil, err
		}
		select {
		case <-step:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
		if err := ReportProgress(ctx, nil, strings.Repeat("é", 200)); err != nil {
			return nil, err
		}
		select {
		case <-release:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
		return commandDone(), nil
	}}, nil)
	p.send(t, invokeWork("c1"))
	first := p.recv(t).GetWorkProgress()
	if first.GetRequestId() != "c1" || first.GetProgress().GetValue() != .25 || first.GetText() != "a quarter" {
		t.Fatal(first)
	}
	close(step)
	second := p.recv(t).GetWorkProgress()
	if !second.GetProgress().GetIndeterminate() || len(second.GetText()) != 256 || !utf8.ValidString(second.GetText()) {
		t.Fatal(second)
	}
	close(release)
	if got := p.recv(t); got.Id != "c1" || !got.GetCommandDone().GetOk() {
		t.Fatal(got)
	}
	if err := ReportProgress(<-contexts, nil, "late"); err != nil {
		t.Fatal(err)
	}
	if p.client.cancelWork(cancelRequest("c1").GetCancelWork()) {
		t.Fatal("finished request is still registered")
	}
	if err := ReportProgress(context.Background(), nil, "outside"); err != nil || Cancelled(context.Background()) {
		t.Fatal(err)
	}
}

func TestWorkCancelReachesBusyHandlersAndKeepsSlot(t *testing.T) {
	requests := []*pb.HostMsg{
		invokeWork("work"),
		{Id: "work", Msg: &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{}}},
		{Id: "work", Msg: &pb.HostMsg_ReadVolume{ReadVolume: &pb.ReadVolume{}}},
		{Id: "work", Msg: &pb.HostMsg_ReadPlane{ReadPlane: &pb.ReadPlane{}}},
		{Id: "work", Msg: &pb.HostMsg_Metadata{Metadata: &pb.Metadata{}}},
		{Id: "work", Msg: &pb.HostMsg_ExportChunk{ExportChunk: &pb.ExportChunk{Handle: "e", Source: &pb.ExportChunk_Inline{Inline: []byte{1}}}}},
	}
	for _, q := range requests {
		t.Run(kind(q.ProtoReflect()), func(t *testing.T) {
			observed, release := make(chan struct{}), make(chan struct{})
			var secondRan, diagnosed, cleaned atomic.Bool
			p := openPeer(t, Config{Diagnostic: func(error) { diagnosed.Store(true) }, HandleRequest: func(ctx context.Context, _ *Client, m *pb.HostMsg) (*pb.PluginMsg, error) {
				if m.GetOpenExport() != nil {
					return &pb.PluginMsg{Msg: &pb.PluginMsg_ExportOpened{ExportOpened: &pb.ExportOpened{Handle: "e"}}}, nil
				}
				if m.Id == "second" {
					secondRan.Store(true)
					return commandDone(), nil
				}
				defer cleaned.Store(true)
				if err := ReportProgress(ctx, proto.Float64(.5), "working"); err != nil {
					return nil, err
				}
				waitWork(t, ctx, func() bool { return Cancelled(ctx) })
				close(observed)
				select {
				case <-release:
				case <-ctx.Done():
					return nil, ctx.Err()
				}
				StopWork(ctx, "stopped at step 3")
				return nil, errors.New("unreachable")
			}}, nil)
			if q.GetExportChunk() != nil {
				p.send(t, &pb.HostMsg{Id: "open", Msg: &pb.HostMsg_OpenExport{OpenExport: &pb.OpenExport{}}})
				p.recv(t)
			}
			p.send(t, q)
			if got := p.recv(t).GetWorkProgress(); got.GetRequestId() != "work" || got.GetText() != "working" {
				t.Fatal(got)
			}
			p.send(t, cancelRequest("work"))
			p.send(t, invokeWork("second"))
			select {
			case <-observed:
			case <-p.ctx.Done():
				t.Fatal(p.ctx.Err())
			}
			if secondRan.Load() {
				t.Fatal("cancel freed the request slot")
			}
			close(release)
			got := p.recv(t)
			if got.Id != "work" || got.GetError().GetKind() != pb.Error_CANCELLED || got.GetError().GetMessage() != "stopped at step 3" {
				t.Fatal(got)
			}
			if got = p.recv(t); got.Id != "second" || !got.GetCommandDone().GetOk() {
				t.Fatal(got)
			}
			if diagnosed.Load() || !cleaned.Load() {
				t.Fatal("cancellation diagnosed or cleanup skipped")
			}
		})
	}
}

func TestWorkCancelledBeforeHandlerStarts(t *testing.T) {
	release := make(chan struct{})
	var secondRan atomic.Bool
	p := openPeer(t, Config{HandleRequest: func(ctx context.Context, _ *Client, m *pb.HostMsg) (*pb.PluginMsg, error) {
		if m.Id == "first" {
			_ = ReportProgress(ctx, nil, "blocked")
			select {
			case <-release:
			case <-ctx.Done():
			}
		} else {
			secondRan.Store(true)
		}
		return commandDone(), nil
	}}, nil)
	p.send(t, invokeWork("first"))
	p.recv(t)
	p.send(t, invokeWork("second"))
	p.send(t, cancelRequest("second"))
	waitWork(t, p.ctx, func() bool { w := p.client.requestWork("second"); return w != nil && w.cancelled.Load() })
	close(release)
	if got := p.recv(t); got.Id != "first" || got.GetCommandDone() == nil {
		t.Fatal(got)
	}
	if got := p.recv(t); got.Id != "second" || got.GetError().GetKind() != pb.Error_CANCELLED {
		t.Fatal(got)
	}
	if secondRan.Load() {
		t.Fatal("canceled queued handler ran")
	}
}

func TestWorkCancelledDuringDescriptorPreparation(t *testing.T) {
	ready := make(chan struct{})
	var ran, released atomic.Bool
	p := openPeer(t, Config{Prepare: func(ctx context.Context, _ *Client, _ *pb.HostMsg) error {
		select {
		case <-ready:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}, Release: func(*Client, *pb.HostMsg) error { released.Store(true); return nil }, HandleRequest: func(context.Context, *Client, *pb.HostMsg) (*pb.PluginMsg, error) { ran.Store(true); return nil, nil }}, nil)
	p.send(t, &pb.HostMsg{Id: "chunk", Msg: &pb.HostMsg_ExportChunk{ExportChunk: &pb.ExportChunk{Source: &pb.ExportChunk_Segment{Segment: &pb.SegmentRef{SegmentId: 1}}}}})
	p.send(t, cancelRequest("chunk"))
	waitWork(t, p.ctx, func() bool { w := p.client.requestWork("chunk"); return w != nil && w.cancelled.Load() })
	close(ready)
	if got := p.recv(t); got.Id != "chunk" || got.GetError().GetKind() != pb.Error_CANCELLED {
		t.Fatal(got)
	}
	if ran.Load() || !released.Load() {
		t.Fatal("handler ran or deposit was not released")
	}
}

func TestWorkCancellationCannotBecomeCommandFailure(t *testing.T) {
	for _, mode := range []string{"sentinel", "host error", "recovered stop"} {
		t.Run(mode, func(t *testing.T) {
			p := openPeer(t, Config{HandleRequest: func(ctx context.Context, _ *Client, _ *pb.HostMsg) (_ *pb.PluginMsg, err error) {
				switch mode {
				case "sentinel":
					return nil, fmt.Errorf("step 3: %w", ErrCancelled)
				case "host error":
					return nil, &HostError{Kind: pb.Error_CANCELLED, Message: "stopped"}
				default:
					defer func() {
						if p := recover(); p != nil {
							err = fmt.Errorf("ordinary wrapper: %v", p)
						}
					}()
					StopWork(ctx, "cancel survived recovery")
					return nil, nil
				}
			}}, nil)
			p.send(t, invokeWork("cancelled"))
			got := p.recv(t)
			if got.GetError().GetKind() != pb.Error_CANCELLED || got.GetCommandDone() != nil {
				t.Fatal(got)
			}
			if mode == "recovered stop" && got.GetError().GetMessage() != "cancel survived recovery" {
				t.Fatal(got)
			}
		})
	}
}

func TestBackgroundWorkLifecycle(t *testing.T) {
	p := openPeer(t, Config{}, nil)
	task, err := p.client.StartTask(strings.Repeat("界", 100))
	if err != nil {
		t.Fatal(err)
	}
	begun := p.recv(t).GetWorkProgress()
	if begun.GetTaskId() != task.ID() || !begun.Cancellable || !begun.GetProgress().GetIndeterminate() || len(begun.Title) != 255 {
		t.Fatal(begun)
	}
	if err = task.Progress(proto.Float64(.5), "half"); err != nil {
		t.Fatal(err)
	}
	if got := p.recv(t).GetWorkProgress(); got.GetProgress().GetValue() != .5 || got.Title != begun.Title {
		t.Fatal(got)
	}
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_CancelWork{CancelWork: &pb.CancelWork{Subject: &pb.CancelWork_TaskId{TaskId: task.ID()}}}})
	waitWork(t, p.ctx, task.Cancelled)
	if err = task.Done("stopped early"); err != nil {
		t.Fatal(err)
	}
	if got := p.recv(t).GetWorkProgress(); !got.GetDone() || got.Text != "stopped early" {
		t.Fatal(got)
	}
	if err = task.Progress(nil, "late"); err != nil {
		t.Fatal(err)
	}
	if err = task.Done("again"); err != nil {
		t.Fatal(err)
	}
	if p.client.cancelWork(&pb.CancelWork{Subject: &pb.CancelWork_TaskId{TaskId: task.ID()}}) {
		t.Fatal("finished task retained")
	}
}

func TestBackgroundWorkDeferredOutcomeAndBound(t *testing.T) {
	c := stateClient(t)
	for _, err := range []error{nil, ErrCancelled, errors.New("no convergence")} {
		task, e := c.StartTask("Fit", false)
		if e != nil {
			t.Fatal(e)
		}
		begun := nextBatch(t, c)[0].GetWorkProgress()
		if begun.Cancellable {
			t.Fatal(begun)
		}
		func() (result error) {
			defer func() {
				if e := task.Finish(result); e != nil {
					t.Error(e)
				}
			}()
			return err
		}()
		got := nextBatch(t, c)[0].GetWorkProgress()
		want := ""
		if err == ErrCancelled {
			want = "canceled"
		} else if err != nil {
			want = "failed: no convergence"
		}
		if !got.Done || got.Text != want {
			t.Fatal(got)
		}
	}
	var tasks []*TaskHandle
	for i := 0; i < 64; i++ {
		task, err := c.StartTask("task")
		if err != nil {
			t.Fatal(i, err)
		}
		tasks = append(tasks, task)
		nextBatch(t, c)
	}
	if _, err := c.StartTask("65th"); err == nil || !strings.Contains(err.Error(), "already open") {
		t.Fatal(err)
	}
	if err := tasks[0].Done(""); err != nil {
		t.Fatal(err)
	}
	nextBatch(t, c)
	if _, err := c.StartTask("room again"); err != nil {
		t.Fatal(err)
	}
}

func TestUnknownCancellationAndWorkCoalescing(t *testing.T) {
	c := stateClient(t)
	c.cfg.CoalescingCapacity = 1
	for _, q := range []*pb.CancelWork{nil, {}, {Subject: &pb.CancelWork_RequestId{RequestId: "gone"}}, {Subject: &pb.CancelWork_TaskId{TaskId: "t99"}}} {
		if c.cancelWork(q) {
			t.Fatal(q)
		}
	}
	if err := c.Send(overlay("existing", 1)); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 100; i++ {
		for _, task := range []bool{false, true} {
			w := &pb.WorkProgress{Subject: &pb.WorkProgress_RequestId{RequestId: "same"}, Text: fmt.Sprint(i), Title: strings.Repeat("🙂", 100)}
			if task {
				w.Subject = &pb.WorkProgress_TaskId{TaskId: "same"}
			}
			if err := c.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_WorkProgress{WorkProgress: w}}); err != nil {
				t.Fatal(err)
			}
		}
	}
	batch := nextBatch(t, c)
	if len(batch) != 3 || c.workCoalescingCount != 0 || c.coalescingCount != 0 {
		t.Fatal(len(batch), c.workCoalescingCount, c.coalescingCount)
	}
	for _, m := range batch[1:] {
		w := m.GetWorkProgress()
		if w.Text != "99" || len(w.Title) != 256 || !utf8.ValidString(w.Title) {
			t.Fatal(w)
		}
	}
	if batch[1].GetWorkProgress().GetRequestId() != "same" || batch[2].GetWorkProgress().GetTaskId() != "same" {
		t.Fatal(batch)
	}
	for i := 0; i < 68; i++ {
		if err := c.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_WorkProgress{WorkProgress: &pb.WorkProgress{Subject: &pb.WorkProgress_TaskId{TaskId: fmt.Sprint(i)}}}}); err != nil {
			t.Fatal(i, err)
		}
	}
	if err := c.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_WorkProgress{WorkProgress: &pb.WorkProgress{Subject: &pb.WorkProgress_TaskId{TaskId: "overflow"}}}}); !errors.Is(err, ErrQueueFull) {
		t.Fatal(err)
	}
}

func TestWorkProgressFractionsAndClip(t *testing.T) {
	for _, tc := range []struct{ in, want float64 }{{-1, 0}, {2, 1}, {.3, .3}, {math.NaN(), 0}, {math.Inf(1), 0}} {
		if got := workProgress(&tc.in); got.Value != tc.want || got.Indeterminate {
			t.Fatal(got)
		}
	}
	for _, s := range []string{strings.Repeat("a", 256), strings.Repeat("a", 255) + "é", strings.Repeat("界", 100), "a\xffb"} {
		got := clipWorkText(s)
		if len(got) > 256 || !utf8.ValidString(got) {
			t.Fatal(got)
		}
	}
}

func TestUserStoppedEndsTasksAndIgnoredCancelWaitsForShutdown(t *testing.T) {
	tasks := make(chan *TaskHandle, 1)
	p := openPeer(t, Config{HandleRequest: func(ctx context.Context, c *Client, _ *pb.HostMsg) (*pb.PluginMsg, error) {
		task, err := c.StartTask("background")
		if err != nil {
			return nil, err
		}
		tasks <- task
		<-ctx.Done()
		return commandDone(), nil
	}, Shutdown: func(_ context.Context, _ *Client, q *pb.Shutdown) error {
		if ShutdownReasonName(q.Reason) != "user_stopped" {
			t.Error(q)
		}
		return nil
	}}, nil)
	p.send(t, invokeWork("ignored"))
	p.recv(t)
	task := <-tasks
	p.send(t, cancelRequest("ignored"))
	waitWork(t, p.ctx, func() bool { w := p.client.requestWork("ignored"); return w != nil && w.cancelled.Load() })
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_Shutdown{Shutdown: &pb.Shutdown{Reason: pb.ShutdownReason_SHUTDOWN_REASON_USER_STOPPED, DeadlineMs: proto.Uint32(20)}}})
	ended := false
	for {
		m, err := p.stream.Recv()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		if w := m.GetWorkProgress(); w != nil && w.GetTaskId() == task.ID() && w.Done {
			ended = true
		}
	}
	if !ended || !task.Cancelled() {
		t.Fatal("task did not end at shutdown")
	}
	if _, err := p.client.StartTask("too late"); err == nil {
		t.Fatal("task started after shutdown")
	}
}

func TestBackgroundProgressCannotResurrectDone(t *testing.T) {
	c := stateClient(t)
	task, err := c.StartTask("race")
	if err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Go(func() { _ = task.Progress(proto.Float64(.5), "working") })
	}
	if err = task.Done("done"); err != nil {
		t.Fatal(err)
	}
	wg.Wait()
	if got := nextBatch(t, c)[0].GetWorkProgress(); !got.Done || got.Text != "done" {
		t.Fatal(got)
	}
}

func TestBackgroundWorkEndsOnGracefulAndAbruptSessionEnd(t *testing.T) {
	for _, abrupt := range []bool{false, true} {
		t.Run(fmt.Sprint(abrupt), func(t *testing.T) {
			p := openPeer(t, Config{}, nil)
			task, err := p.client.StartTask("left open")
			if err != nil {
				t.Fatal(err)
			}
			p.recv(t)
			if abrupt {
				if err = p.stream.CloseSend(); err != nil {
					t.Fatal(err)
				}
			} else {
				p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_Shutdown{Shutdown: &pb.Shutdown{Reason: pb.ShutdownReason_SHUTDOWN_REASON_NOT_NEEDED, DeadlineMs: proto.Uint32(1000)}}})
			}
			ended := false
			for {
				m, e := p.stream.Recv()
				if e == io.EOF {
					break
				}
				if e != nil {
					t.Fatal(e)
				}
				if w := m.GetWorkProgress(); w != nil && w.GetTaskId() == task.ID() && w.Done {
					ended = true
				}
			}
			if !abrupt && !ended {
				t.Fatal("graceful shutdown omitted task outcome")
			}
			if !task.Cancelled() {
				t.Fatal("session end retained task")
			}
			if err = task.Progress(nil, "late"); err != nil {
				t.Fatal(err)
			}
			p.client.workMu.Lock()
			remaining := len(p.client.tasks)
			p.client.workMu.Unlock()
			if remaining != 0 {
				t.Fatal(remaining)
			}
		})
	}
}

func TestWorkCanIgnoreCancellationAndReturnNormally(t *testing.T) {
	p := openPeer(t, Config{HandleRequest: func(ctx context.Context, _ *Client, _ *pb.HostMsg) (*pb.PluginMsg, error) {
		_ = ReportProgress(ctx, nil, "working")
		waitWork(t, ctx, func() bool { return Cancelled(ctx) })
		return commandDone(), nil
	}}, nil)
	p.send(t, invokeWork("normal"))
	p.recv(t)
	p.send(t, cancelRequest("normal"))
	if got := p.recv(t); !got.GetCommandDone().GetOk() {
		t.Fatal(got)
	}
}

func TestCancelledAfterReplyWorkerIsQuiet(t *testing.T) {
	c := stateClient(t)
	c.diagnostics = make(chan error, 1)
	c.afterReplySlots = make(chan struct{}, 1)
	for _, err := range []error{ErrCancelled, fmt.Errorf("background: %w", ErrCancelled), &HostError{Kind: pb.Error_CANCELLED, Message: "canceled"}} {
		c.afterReplySlots <- struct{}{}
		sent := make(chan error, 1)
		sent <- nil
		c.continueAfterReply(sent, func(context.Context, *Client) error { return err })
		if len(c.diagnostics) != 0 || len(c.afterReplySlots) != 0 {
			t.Fatal("canceled continuation diagnosed or retained worker slot")
		}
	}
}

// Cancellable is UI advice, not an admission rule.
func TestNonCancellableTaskAcceptsCancelWithoutFault(t *testing.T) {
	var diagnostics atomic.Int32
	p := openPeer(t, Config{Diagnostic: func(error) { diagnostics.Add(1) }}, nil)
	task, err := p.client.StartTask("Cannot interrupt this step", false)
	if err != nil {
		t.Fatal(err)
	}
	begun := p.recv(t).GetWorkProgress()
	if begun.GetTaskId() != task.ID() || begun.GetCancellable() {
		t.Fatal("task did not advertise cancellable=false", begun)
	}
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_CancelWork{CancelWork: &pb.CancelWork{Subject: &pb.CancelWork_TaskId{TaskId: task.ID()}}}})
	waitWork(t, p.ctx, task.Cancelled)
	// The task may ignore the flag and complete normally. No refusal or report
	// may appear ahead of its progress, completion, or final shutdown report.
	if err := task.Progress(proto.Float64(.75), "Continuing"); err != nil {
		t.Fatal(err)
	}
	if got := p.recv(t).GetWorkProgress(); got.GetTaskId() != task.ID() || got.GetText() != "Continuing" || got.GetDone() || got.GetCancellable() {
		t.Fatal(got)
	}
	if err := task.Done("Completed"); err != nil {
		t.Fatal(err)
	}
	if got := p.recv(t).GetWorkProgress(); got.GetTaskId() != task.ID() || !got.GetDone() || got.GetText() != "Completed" {
		t.Fatal(got)
	}
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_Shutdown{Shutdown: &pb.Shutdown{Reason: pb.ShutdownReason_SHUTDOWN_REASON_NOT_NEEDED, DeadlineMs: proto.Uint32(1000)}}})
	if got := p.recv(t); got.GetShutdownReport() == nil {
		t.Fatal("unexpected refusal or report", got)
	}
	if got, err := p.stream.Recv(); err != io.EOF {
		t.Fatal("unexpected message after shutdown", got, err)
	}
	if diagnostics.Load() != 0 || len(p.client.diagnostics) != 0 {
		t.Fatal("non-cancellable task cancel was diagnosed")
	}
	for _, stats := range p.client.cfg.Reporter.Stats() {
		if stats.Observed != 0 {
			t.Fatal("non-cancellable task cancel was reported", stats)
		}
	}
}
