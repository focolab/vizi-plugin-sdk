package control

import (
	"context"
	"errors"
	"maps"
	"testing"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

func TestRouteTablesCoverIDLAndReplyTargets(t *testing.T) {
	for _, side := range []struct {
		message          proto.Message
		routes, opposite map[string]route
	}{
		{&pb.HostMsg{}, hostRoutes, pluginRoutes}, {&pb.PluginMsg{}, pluginRoutes, hostRoutes},
	} {
		if err := checkRoutes(side.message.ProtoReflect(), side.routes); err != nil {
			t.Fatal(err)
		}
		for name, r := range side.routes {
			if r.class == Request && side.opposite[r.reply].class != ReplyClass {
				t.Fatalf("%s expects non-reply %s", name, r.reply)
			}
			broken := maps.Clone(side.routes)
			delete(broken, name)
			if checkRoutes(side.message.ProtoReflect(), broken) == nil {
				t.Fatalf("missing %s accepted", name)
			}
			broken["invented_arm"] = r // same count must not bypass structural coverage
			if checkRoutes(side.message.ProtoReflect(), broken) == nil {
				t.Fatalf("replacement for %s accepted", name)
			}
		}
	}
}

func TestTrackedAssetStatusAndLateReply(t *testing.T) {
	p := openPeer(t, Config{}, nil)
	ctx, cancel := context.WithCancel(p.ctx)
	defer cancel()
	ids := make(chan string, 1)
	finished := make(chan error, 1)
	original := &pb.PluginMsg{Id: "caller-id", Msg: &pb.PluginMsg_RequestAsset{RequestAsset: &pb.RequestAsset{Slot: "model"}}}
	go func() {
		r, err := p.client.CallTracked(ctx, original, func(id string) { ids <- id })
		if r != nil {
			r.Close()
		}
		finished <- err
	}()
	asset := p.recv(t)
	if id := <-ids; id != asset.Id || id == "" || original.Id != "caller-id" {
		t.Fatalf("id %q, request %v", id, asset)
	}
	statusDone := make(chan *pb.RequestStatus, 1)
	statusErr := make(chan error, 1)
	go func() { r, err := p.client.RequestStatus(p.ctx, asset.Id); statusDone <- r; statusErr <- err }()
	query := p.recv(t)
	if query.Id == asset.Id || query.GetRequestStatusQuery().GetSubjectId() != asset.Id {
		t.Fatal(query)
	}
	expected := &pb.RequestStatus{SubjectId: asset.Id, Disposition: pb.RequestStatus_PENDING, AwaitingUser: true}
	p.send(t, &pb.HostMsg{Id: query.Id, Msg: &pb.HostMsg_RequestStatus{RequestStatus: expected}})
	if got, err := <-statusDone, <-statusErr; err != nil || !proto.Equal(got, expected) {
		t.Fatal(got, err)
	}
	if got := p.client.UnmatchedReplies(); got != 0 {
		t.Fatalf("matched reply counted as unmatched: %d", got)
	}
	cancel()
	if err := <-finished; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	p.send(t, &pb.HostMsg{Id: asset.Id, Msg: &pb.HostMsg_AssetResult{AssetResult: &pb.AssetResult{Declined: true}}})
	// A further request/reply proves the receive loop survives the late pick.
	go func() { r, err := p.client.RequestStatus(p.ctx, asset.Id); statusDone <- r; statusErr <- err }()
	query = p.recv(t)
	p.send(t, &pb.HostMsg{Id: query.Id, Msg: &pb.HostMsg_RequestStatus{RequestStatus: &pb.RequestStatus{SubjectId: asset.Id, Disposition: pb.RequestStatus_UNKNOWN}}})
	if got, err := <-statusDone, <-statusErr; err != nil || got.GetDisposition() != pb.RequestStatus_UNKNOWN {
		t.Fatal(got, err)
	}
	if got := p.client.UnmatchedReplies(); got != 1 {
		t.Fatalf("unmatched replies = %d, want 1", got)
	}
}

func TestLateReplyFloodDoesNotFillDiagnostics(t *testing.T) {
	c := stateClient(t)
	c.diagnostics = make(chan error, 1)
	released := 0
	c.cfg.Release = func(*Client, *pb.HostMsg) error { released++; return nil }
	observed := make(chan struct{})
	go func() {
		defer close(observed)
		var previous uint64
		for i := 0; i < 100; i++ {
			got := c.UnmatchedReplies()
			if got < previous || got > 100 {
				t.Errorf("invalid counter snapshot: previous=%d current=%d", previous, got)
			}
			previous = got
		}
	}()
	for i := 0; i < 100; i++ {
		c.routeReply(&pb.HostMsg{Id: "retired", Msg: &pb.HostMsg_AssetResult{AssetResult: &pb.AssetResult{}}})
	}
	<-observed
	if got := c.UnmatchedReplies(); got != 100 || c.UnmatchedReplies() != got {
		t.Fatalf("unmatched replies = %d, want persistent count 100", got)
	}
	if got := stateClient(t).UnmatchedReplies(); got != 0 {
		t.Fatalf("fresh session inherited %d unmatched replies", got)
	}
	if c.ctx.Err() != nil || len(c.diagnostics) != 0 || released != 100 {
		t.Fatalf("fault=%v diagnostics=%d released=%d", c.ctx.Err(), len(c.diagnostics), released)
	}
}

func TestRoiExtensionAndWindowHelpers(t *testing.T) {
	p := openPeer(t, Config{}, nil)
	request := &pb.ExtendRoiPoses{Extensions: []*pb.RoiPoseExtension{{ContribId: "region", TStart: 5, Span: 1, PoseTranslation: []float64{1, 2, 3}}}}
	results := make(chan *pb.RoiPosesExtended, 1)
	errs := make(chan error, 1)
	go func() { r, err := p.client.ExtendRoiPoses(p.ctx, request); results <- r; errs <- err }()
	m := p.recv(t)
	if !proto.Equal(m.GetExtendRoiPoses(), request) {
		t.Fatal(m)
	}
	p.send(t, &pb.HostMsg{Id: m.Id, Msg: &pb.HostMsg_RoiPosesExtended{RoiPosesExtended: &pb.RoiPosesExtended{RegionCount: 1, FrameCount: 1}}})
	if got, err := <-results, <-errs; err != nil || got.RegionCount != 1 || got.FrameCount != 1 {
		t.Fatal(got, err)
	}
	// Host validation errors remain typed and do not become a zero success result.
	go func() { r, err := p.client.ExtendRoiPoses(p.ctx, request); results <- r; errs <- err }()
	m = p.recv(t)
	p.send(t, &pb.HostMsg{Id: m.Id, Msg: &pb.HostMsg_HostError{HostError: &pb.Error{Kind: pb.Error_INVALID_ARGUMENT, Message: "gap"}}})
	got, err := <-results, <-errs
	var hostErr *HostError
	if got != nil || !errors.As(err, &hostErr) || hostErr.Kind != pb.Error_INVALID_ARGUMENT {
		t.Fatal(got, err)
	}
	rois := make(chan *pb.RoiList, 1)
	for _, window := range []bool{false, true} {
		go func() {
			var r *pb.RoiList
			var err error
			if window {
				r, err = p.client.GetRoisWindow(p.ctx, 0, 5)
			} else {
				r, err = p.client.GetRois(p.ctx)
			}
			rois <- r
			errs <- err
		}()
		m = p.recv(t)
		q := m.GetGetRois()
		if q == nil || (q.PoseTStart != nil) != window || (q.PoseTEnd != nil) != window || (window && (q.GetPoseTStart() != 0 || q.GetPoseTEnd() != 5)) {
			t.Fatal(m)
		}
		expected := &pb.RoiList{Rois: []*pb.RoiRecord{
			{ContribId: "region", PoseDomainTStart: proto.Int32(0), PoseDomainTEnd: proto.Int32(100), Roi: &pb.Roi{TStart: proto.Int32(0), TEnd: proto.Int32(100)}},
			{ContribId: "static", Roi: &pb.Roi{}},
		}}
		if window {
			expected.Rois[0].Roi.TEnd = proto.Int32(5)
			expected.Rois[0].Roi.PoseTranslation = make([]float64, 3*6)
		}
		p.send(t, &pb.HostMsg{Id: m.Id, Msg: &pb.HostMsg_Rois{Rois: expected}})
		if got, err := <-rois, <-errs; err != nil || !proto.Equal(got, expected) {
			t.Fatal(got, err)
		}
	}
}

func TestProgressCoalescingRetainsWholeState(t *testing.T) {
	c := stateClient(t)
	for _, state := range []*pb.Progress{{Value: .2}, {Indeterminate: true}, {Value: .8}} {
		if err := c.Send(patch("p", &pb.PatchOp{WidgetId: "bar", Op: &pb.PatchOp_SetProgress{SetProgress: state}})); err != nil {
			t.Fatal(err)
		}
	}
	batch := nextBatch(t, c)
	if len(batch) != 1 || len(batch[0].GetPanelPatch().Ops) != 1 {
		t.Fatal(batch)
	}
	got := batch[0].GetPanelPatch().Ops[0].GetSetProgress()
	if got == nil || got.Value != .8 || got.Indeterminate {
		t.Fatal(got)
	}
}

func TestTrackedCallbackAdmissionAndPanicCleanup(t *testing.T) {
	p := openPeer(t, Config{}, nil)
	request := &pb.PluginMsg{Msg: &pb.PluginMsg_GetViewState{GetViewState: &pb.GetViewState{}}}
	ctx, cancel := context.WithCancel(p.ctx)
	cancel()
	called := false
	if _, err := p.client.CallTracked(ctx, request, func(string) { called = true }); !errors.Is(err, context.Canceled) || called {
		t.Fatal(err, called)
	}
	func() {
		defer func() {
			if recover() != "callback panic" {
				t.Error("callback panic not propagated")
			}
		}()
		_, _ = p.client.CallTracked(p.ctx, request, func(string) { panic("callback panic") })
	}()
	p.client.mu.Lock()
	pending := len(p.client.waiters)
	p.client.mu.Unlock()
	if pending != 0 {
		t.Fatalf("callback panic leaked %d waiters", pending)
	}
	sent := p.recv(t)
	p.send(t, &pb.HostMsg{Id: sent.Id, Msg: &pb.HostMsg_ViewState{ViewState: &pb.ViewState{}}})
}
