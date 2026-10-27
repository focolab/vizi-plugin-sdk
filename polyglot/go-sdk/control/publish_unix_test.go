//go:build linux || darwin

package control

import (
	"errors"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func TestPublishLoanAdmissionAndRelease(t *testing.T) {
	for _, purpose := range []string{memory.PurposePanel, memory.PurposeOverlay} {
		t.Run(purpose, func(t *testing.T) {
			m, c, ref := memoryPeerFor(t, true, purpose, pb.RegionProducer_REGION_PRODUCER_PLUGIN)
			session := &Session{Memory: m, Client: c, Shortfalls: NewShortfalls()}
			seg, err := m.Registry.Segment(ref.SegmentId)
			if err != nil {
				t.Fatal(err)
			}
			arena, err := seg.Arena()
			if err != nil || arena.Index != 0 || seg.Purpose() != purpose {
				t.Fatal(arena, err)
			}
			build := func(d *pb.SegmentRef) (*pb.PluginMsg, error) {
				if purpose == memory.PurposePanel {
					return patch("p", &pb.PatchOp{WidgetId: "image", Op: &pb.PatchOp_SetImage{SetImage: &pb.ImageRef{SegmentId: d.SegmentId, Offset: d.Offset, Len: d.Len}}}), nil
				}
				return &pb.PluginMsg{Msg: &pb.PluginMsg_OverlayUpdate{OverlayUpdate: &pb.OverlayUpdate{OverlayId: "o", Revision: 1, Items: []*pb.OverlayItem{{ItemId: "points", Geometry: &pb.OverlayItem_Marker{Marker: &pb.OverlayMarker{PositionsZyx: &pb.GeomBuffer{Source: &pb.GeomBuffer_Segment{Segment: d}}}}}}}}}, nil
			}
			for _, bad := range []func(*pb.SegmentRef) (*pb.PluginMsg, error){
				func(*pb.SegmentRef) (*pb.PluginMsg, error) { return nil, errors.New("builder failed") },
				func(*pb.SegmentRef) (*pb.PluginMsg, error) { panic("builder panic") },
				func(d *pb.SegmentRef) (*pb.PluginMsg, error) { d.Offset++; return build(d) },
			} {
				if err := session.PublishLoan(17, purpose, []byte("bytes"), bad); err == nil {
					t.Fatal("bad builder accepted")
				}
				if stats, _ := seg.LoanStats(0); stats.Outstanding != 0 {
					t.Fatal("failed publication leaked", stats)
				}
			}
			if err := session.PublishLoan(17, purpose, []byte("bytes"), build); err != nil {
				t.Fatal(err)
			}
			msg := nextBatch(t, c)[0]
			if err := c.Send(msg); !errors.Is(err, memory.ErrUsesExceeded) {
				t.Fatal("published reservation admitted twice", err)
			}
			var offset, length uint64
			if purpose == memory.PurposePanel {
				d := msg.GetPanelPatch().Ops[0].GetSetImage()
				offset, length = d.Offset, d.Len
			} else {
				d := msg.GetOverlayUpdate().Items[0].GetMarker().PositionsZyx.GetSegment()
				offset, length = d.Offset, d.Len
			}
			if stats, _ := seg.LoanStats(0); stats.Outstanding != 1 {
				t.Fatal("accepted loan reclaimed", stats)
			}
			if err := m.Apply(c, &pb.HostMsg{Msg: &pb.HostMsg_ReleaseLoan{ReleaseLoan: &pb.ReleaseLoan{SegmentId: 17, Region: 0, Offset: offset, Len: length}}}); err != nil {
				t.Fatal(err)
			}
			if stats, _ := seg.LoanStats(0); stats.Outstanding != 0 {
				t.Fatal(stats)
			}
			c.out = make(chan outbound, 1)
			if err := c.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_OverlayRemove{OverlayRemove: &pb.OverlayRemove{OverlayId: "block"}}}); err != nil {
				t.Fatal(err)
			}
			if err := session.PublishLoan(17, purpose, []byte("refused"), build); !errors.Is(err, ErrQueueFull) {
				t.Fatal(err)
			}
			if stats, _ := seg.LoanStats(0); stats.Outstanding != 0 {
				t.Fatal("admission refusal leaked", stats)
			}
			if c.ctx.Err() != nil {
				t.Fatal("push refusal faulted session")
			}
		})
	}
}

func TestRawLoanRollbackAfterDefiniteNonAdmission(t *testing.T) {
	for _, refusal := range []string{"queue_full", "duplicate_reservation"} {
		t.Run(refusal, func(t *testing.T) {
			m, c, ref := memoryPeerFor(t, true, memory.PurposePanel, pb.RegionProducer_REGION_PRODUCER_PLUGIN)
			session := &Session{Memory: m, Client: c, Shortfalls: NewShortfalls()}
			seg, err := m.Registry.Segment(ref.SegmentId)
			if err != nil {
				t.Fatal(err)
			}
			c.out = make(chan outbound, 1)
			if refusal == "queue_full" {
				if err := c.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_OverlayRemove{OverlayRemove: &pb.OverlayRemove{OverlayId: "block"}}}); err != nil {
					t.Fatal(err)
				}
			}
			d, err := session.WriteLoan(17, 0, []byte("not yet sent"))
			if err != nil {
				t.Fatal(err)
			}
			op := func(widget string) *pb.PatchOp {
				return &pb.PatchOp{WidgetId: widget, Op: &pb.PatchOp_SetImage{SetImage: &pb.ImageRef{SegmentId: d.SegmentID, Offset: d.Offset, Len: d.Length}}}
			}
			msg := patch("p", op("a"))
			want := ErrQueueFull
			if refusal == "duplicate_reservation" {
				msg = patch("p", op("a"), op("b"))
				want = ErrDuplicateReservation
			}
			if err := c.Send(msg); !errors.Is(err, want) {
				t.Fatal(err)
			}
			if stats, _ := seg.LoanStats(0); stats.Outstanding != 1 {
				t.Fatal("raw API unexpectedly disposed caller's loan", stats)
			}
			// This is a LOCAL call, made once because no descriptor use was admitted.
			// No ReleaseLoan is sent or expected from the host.
			if err := seg.ReleaseLoan(d); err != nil {
				t.Fatal(err)
			}
			again, err := session.WriteLoan(17, 0, []byte("reclaimed"))
			if err != nil || again.Offset != d.Offset {
				t.Fatal("arena leaked", again, err)
			}
			if err := seg.ReleaseLoan(again); err != nil {
				t.Fatal(err)
			}
			if c.ctx.Err() != nil {
				t.Fatal("local refusal faulted session")
			}
		})
	}
}

func TestRawSharedLoanRetainedUntilEveryUseAdmittedAndHostReleases(t *testing.T) {
	m, c, ref := memoryPeerFor(t, true, memory.PurposeOverlay, pb.RegionProducer_REGION_PRODUCER_PLUGIN)
	session := &Session{Memory: m, Client: c, Shortfalls: NewShortfalls()}
	seg, err := m.Registry.Segment(ref.SegmentId)
	if err != nil {
		t.Fatal(err)
	}
	c.out = make(chan outbound, 1)
	d, err := session.WriteLoan(17, 0, []byte("shared bytes"))
	if err != nil {
		t.Fatal(err)
	}
	makeMsg := func(rev uint64) *pb.PluginMsg {
		return &pb.PluginMsg{Msg: &pb.PluginMsg_OverlayUpdate{OverlayUpdate: &pb.OverlayUpdate{OverlayId: "o", Revision: rev, Items: []*pb.OverlayItem{{ItemId: "item", Geometry: &pb.OverlayItem_Marker{Marker: &pb.OverlayMarker{PositionsZyx: &pb.GeomBuffer{Source: &pb.GeomBuffer_Segment{Segment: &pb.SegmentRef{SegmentId: d.SegmentID, Offset: d.Offset, Len: d.Length, Uses: 2}}}}}}}}}}
	}
	if err := c.Send(makeMsg(1)); err != nil {
		t.Fatal(err)
	}
	if err := c.Send(makeMsg(2)); !errors.Is(err, ErrQueueFull) {
		t.Fatal(err)
	}
	// One use is already visible to the host. Rolling back now would free live
	// bytes; the caller must retain and retry the other declared use unchanged.
	if err := seg.ReleaseLoan(d); !errors.Is(err, memory.ErrLoanAdmitted) {
		t.Fatal("partially admitted loan reclaimed", err)
	}
	if stats, _ := seg.LoanUseStats(0); stats.Named != 1 || stats.PendingUses != 1 || stats.Stranded != 1 {
		t.Fatal("failed admission consumed a use or reclaim lost accounting", stats)
	}
	if stats, _ := seg.LoanStats(0); stats.Outstanding != 1 {
		t.Fatal(stats)
	}
	if got := nextBatch(t, c)[0].GetOverlayUpdate(); got.Revision != 1 {
		t.Fatal(got)
	}
	if err := c.Send(makeMsg(2)); err != nil {
		t.Fatal(err)
	}
	if got := nextBatch(t, c)[0].GetOverlayUpdate(); got.Revision != 2 || got.Items[0].GetMarker().PositionsZyx.GetSegment().Uses != 2 {
		t.Fatal(got)
	}
	if stats, _ := seg.LoanStats(0); stats.Outstanding != 1 {
		t.Fatal("admission released bytes before the host", stats)
	}
	if err := c.Send(makeMsg(3)); !errors.Is(err, memory.ErrUsesExceeded) {
		t.Fatal("third use admitted", err)
	}
	if len(c.out) != 0 {
		t.Fatal("excess use entered queue")
	}
	if err := m.Apply(c, &pb.HostMsg{Msg: &pb.HostMsg_ReleaseLoan{ReleaseLoan: &pb.ReleaseLoan{SegmentId: d.SegmentID, Region: d.Region, Offset: d.Offset, Len: d.Length}}}); err != nil {
		t.Fatal(err)
	}
	if stats, _ := seg.LoanStats(0); stats.Outstanding != 0 {
		t.Fatal("host release failed", stats)
	}
}

func TestChunkAndTableDescriptorOwnership(t *testing.T) {
	for _, table := range []bool{false, true} {
		m, c, _ := memoryPeerFor(t, true, memory.PurposeOverlay, pb.RegionProducer_REGION_PRODUCER_PLUGIN)
		s := &Session{Memory: m, Client: c, Shortfalls: NewShortfalls()}
		build := func(ref *pb.SegmentRef) (*pb.PluginMsg, error) {
			b := &pb.GeomBuffer{Source: &pb.GeomBuffer_Segment{Segment: ref}}
			if table {
				return &pb.PluginMsg{Msg: &pb.PluginMsg_OverlayLabelTable{OverlayLabelTable: &pb.OverlayLabelTable{OverlayId: "o", Colormap: b}}}, nil
			}
			return &pb.PluginMsg{Msg: &pb.PluginMsg_OverlayChunk{OverlayChunk: &pb.OverlayChunk{OverlayId: "o", Labels: b}}}, nil
		}
		if err := s.PublishLoan(17, memory.PurposeOverlay, []byte{1}, build); err != nil {
			t.Fatal(err)
		}
		msg := nextBatch(t, c)[0]
		if err := c.Send(msg); !errors.Is(err, memory.ErrUsesExceeded) {
			t.Fatal("descriptor not walked for loan uses", err)
		}
		var d *pb.SegmentRef
		if table {
			d = msg.GetOverlayLabelTable().Colormap.GetSegment()
		} else {
			d = msg.GetOverlayChunk().Labels.GetSegment()
		}
		wake := m.LoanChanges()
		if err := m.Apply(c, &pb.HostMsg{Msg: &pb.HostMsg_ReleaseLoan{ReleaseLoan: &pb.ReleaseLoan{SegmentId: d.SegmentId, Region: 0, Offset: d.Offset, Len: d.Len}}}); err != nil {
			t.Fatal(err)
		}
		select {
		case <-wake:
		default:
			t.Fatal("release did not wake deferred publishers")
		}
		seg, _ := m.Registry.Segment(17)
		if stats, _ := seg.LoanStats(0); stats.Outstanding != 0 {
			t.Fatal(stats)
		}
	}
}

func TestRetirementWaitsForOverlayPublicationAdmission(t *testing.T) {
	for _, rollback := range []bool{false, true} {
		t.Run(map[bool]string{false: "admit", true: "rollback"}[rollback], func(t *testing.T) {
			m, c, _ := memoryPeerFor(t, true, memory.PurposeOverlay, pb.RegionProducer_REGION_PRODUCER_PLUGIN)
			session := &Session{Memory: m, Client: c, Shortfalls: NewShortfalls()}
			entered, resume := make(chan struct{}), make(chan struct{})
			defer func() {
				select {
				case <-resume:
				default:
					close(resume)
				}
			}()
			result := make(chan error, 1)
			buildErr := errors.New("builder refused")
			go func() {
				result <- session.PublishLoan(17, memory.PurposeOverlay, []byte("copy"), func(d *pb.SegmentRef) (*pb.PluginMsg, error) {
					close(entered)
					<-resume
					if rollback {
						return nil, buildErr
					}
					return &pb.PluginMsg{Msg: &pb.PluginMsg_OverlayChunk{OverlayChunk: &pb.OverlayChunk{OverlayId: "o", Labels: &pb.GeomBuffer{Source: &pb.GeomBuffer_Segment{Segment: d}}}}}, nil
				})
			}()
			<-entered
			if err := m.Apply(c, &pb.HostMsg{Msg: &pb.HostMsg_RetireSegment{RetireSegment: &pb.RetireSegment{SegmentId: 17}}}); err != nil {
				t.Fatal("normal retirement reported an error", err)
			}
			// Apply returned with retirement latched. There can be no release yet.
			select {
			case msg := <-c.out:
				t.Fatal("release overtook unfinished publication", msg.msg)
			default:
			}
			close(resume)
			err := <-result
			if rollback {
				if !errors.Is(err, buildErr) {
					t.Fatal(err)
				}
			} else if err != nil {
				t.Fatal(err)
			}
			if !rollback {
				if msg := nextBatch(t, c)[0]; msg.GetOverlayChunk() == nil {
					t.Fatal("release overtook admitted descriptor", msg)
				}
			}
			if msg := nextBatch(t, c)[0]; msg.GetSegmentReleased().GetSegmentId() != 17 {
				t.Fatal(msg)
			}
			if got := m.Registry.Status(17).State; got != memory.Released {
				t.Fatal(got)
			}
			if err := session.PublishLoan(17, memory.PurposeOverlay, []byte("late"), func(*pb.SegmentRef) (*pb.PluginMsg, error) { t.Error("builder ran on retired arena"); return nil, nil }); err != nil {
				t.Fatal(err)
			}
			if m.DroppedPublications() != 1 {
				t.Fatal(m.DroppedPublications())
			}
		})
	}
}
