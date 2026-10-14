//go:build linux || darwin

package control

import (
	"encoding/binary"
	"errors"
	"hash/crc32"
	"os"
	"runtime"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"golang.org/x/sys/unix"
)

func TestRetirementClaimDropsBeforeDrainWaitIsRegistered(t *testing.T) {
	m, c, _ := memoryPeerFor(t, true, "P", pb.RegionProducer_REGION_PRODUCER_PLUGIN)
	s, err := m.Registry.Segment(17)
	if err != nil {
		t.Fatal(err)
	}
	loan, err := s.WriteLoan(0, []byte("pending"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err = m.Registry.Retire(17); !errors.Is(err, memory.ErrLoanActive) {
		t.Fatal(err)
	}
	// Deliberately close the final claim between Retire's check and the
	// adapter's registration of its eventual-release waiter.
	if err = s.ReleaseLoan(loan); err != nil {
		t.Fatal(err)
	}
	if err = m.sendUpdate(c, memory.Update{PendingReleases: []uint64{17}}, nil); err != nil {
		t.Fatal(err)
	}
	if msg := nextBatch(t, c)[0]; msg.GetSegmentReleased().GetSegmentId() != 17 {
		t.Fatal(msg)
	}
	if m.Registry.Status(17).State != memory.Released {
		t.Fatal("retirement stuck")
	}
}

// Grant another real mapping to the same session through the capability channel.
func grantSuccessor(t *testing.T, m *Memory, c *Client, purpose, subject string, supersedes uint64) {
	t.Helper()
	f, err := os.CreateTemp(t.TempDir(), "successor")
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	data := make([]byte, 256)
	binary.LittleEndian.PutUint64(data, 18)
	copy(data[12:28], "identity-proof!!")
	binary.LittleEndian.PutUint32(data[28:32], crc32.ChecksumIEEE(data[:28]))
	if _, err = f.Write(data); err != nil {
		t.Fatal(err)
	}
	kind := unix.SOCK_SEQPACKET
	if runtime.GOOS == "darwin" {
		kind = unix.SOCK_DGRAM
	}
	pair, err := unix.Socketpair(unix.AF_UNIX, kind, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer unix.Close(pair[0])
	granted := make(chan error, 1)
	db, err := doorbell.Adopt(pair[1], doorbell.Config{Handler: func(e doorbell.Event) {
		if e.Frame != nil {
			granted <- m.Grant(c, e.Frame)
		}
	}})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { db.Stop(); db.Wait(c.Context()) }()
	raw, err := doorbell.EncodeMemory(doorbell.Memory{SegmentID: 18, Size: 256, Permissions: doorbell.PluginRead | doorbell.PluginWrite}, 2)
	if err != nil {
		t.Fatal(err)
	}
	if err = unix.Sendmsg(pair[0], raw[:], unix.UnixRights(int(f.Fd())), nil, 0); err != nil {
		t.Fatal(err)
	}
	select {
	case err = <-granted:
		if err != nil {
			t.Fatal(err)
		}
	case <-c.Context().Done():
		t.Fatal(c.Context().Err())
	}
	if err = m.Install(c, &pb.InstallSegment{SegmentId: 18, Purpose: purpose, Subject: subject, Supersedes: supersedes, Regions: []*pb.Region{{Index: 0, Offset: 64, Extent: 192, Discipline: pb.RegionDiscipline_REGION_DISCIPLINE_LOANED, Producer: pb.RegionProducer_REGION_PRODUCER_PLUGIN}}}); err != nil {
		t.Fatal(err)
	}
}

func TestOverlaySupersessionWaitsForActualSend(t *testing.T) {
	m, c, _ := memoryPeerFor(t, true, "O", pb.RegionProducer_REGION_PRODUCER_PLUGIN)
	session := &Session{Client: c, Memory: m, Shortfalls: NewShortfalls()}
	entered, resume := make(chan struct{}), make(chan struct{})
	defer func() {
		select {
		case <-resume:
		default:
			close(resume)
		}
	}()
	published := make(chan error, 1)
	build := func(d *pb.SegmentRef) (*pb.PluginMsg, error) {
		return &pb.PluginMsg{Msg: &pb.PluginMsg_OverlayChunk{OverlayChunk: &pb.OverlayChunk{OverlayId: "o", Labels: &pb.GeomBuffer{Source: &pb.GeomBuffer_Segment{Segment: d}}}}}, nil
	}
	go func() {
		published <- session.PublishLoan(17, "O", []byte("parked"), func(d *pb.SegmentRef) (*pb.PluginMsg, error) { close(entered); <-resume; return build(d) })
	}()
	<-entered
	grantSuccessor(t, m, c, "O", "o", 17)
	if msg := nextBatch(t, c)[0]; msg.GetGrantAck().GetSegmentId() != 18 {
		t.Fatal(msg)
	}
	if m.Registry.Status(17).State != memory.Installed {
		t.Fatal("retirement passed the builder")
	}
	close(resume)
	if err := <-published; err != nil {
		t.Fatal(err)
	}
	// Dequeue without completing the send, modeling a blocked transport.
	x, ok := c.nextOutbound()
	if !ok || x.msg.GetOverlayChunk() == nil {
		t.Fatal(x, ok)
	}
	if m.Registry.Status(17).State != memory.Installed {
		t.Fatal("admission unmapped the arena")
	}
	select {
	case released := <-c.out:
		t.Fatal("release overtook Send", released.msg)
	default:
	}
	// Host drops this retiring push, and returns its loan after SegmentReleased.
	m.SentOutbound(x.msg)
	if msg := nextBatch(t, c)[0]; msg.GetSegmentReleased().GetSegmentId() != 17 {
		t.Fatal(msg)
	}
	ref := x.msg.GetOverlayChunk().Labels.GetSegment()
	if err := m.Apply(c, &pb.HostMsg{Msg: &pb.HostMsg_ReleaseLoan{ReleaseLoan: &pb.ReleaseLoan{SegmentId: 17, Region: 0, Offset: ref.Offset, Len: ref.Len}}}); err != nil {
		t.Fatal("late release", err)
	}
	if err := session.PublishLoan(17, "O", []byte("next"), build); err != nil {
		t.Fatal(err)
	}
	if msg := nextBatch(t, c)[0]; msg.GetOverlayChunk().GetLabels().GetSegment().GetSegmentId() != 18 {
		t.Fatal("push missed successor", msg)
	}
	if m.DroppedPublications() != 0 {
		t.Fatal("completed work counted as dropped")
	}
}

func TestArrivedReplyRemainsReadableUntilClosed(t *testing.T) {
	m, _, ref := memoryPeer(t, true)
	p := openPeer(t, Config{Admit: m.Admit, Prepare: m.WaitDescriptor, Release: m.Release, Arrange: m.Apply}, nil)
	result := make(chan *Reply, 1)
	go func() {
		r, err := p.client.Call(p.ctx, &pb.PluginMsg{Msg: &pb.PluginMsg_ReadDataVolume{ReadDataVolume: &pb.ReadDataVolume{}}})
		if err != nil {
			t.Error(err)
		}
		result <- r
	}()
	request := p.recv(t)
	p.send(t, &pb.HostMsg{Id: request.Id, Msg: &pb.HostMsg_DataVolume{DataVolume: &pb.DataVolume{Segment: ref}}})
	reply := <-result
	if reply == nil {
		t.Fatal("missing reply")
	}
	defer reply.Close()
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_RetireSegment{RetireSegment: &pb.RetireSegment{SegmentId: 17}}})
	// Stream-ordered query proves retirement was processed, without a sleep.
	p.send(t, &pb.HostMsg{Id: "barrier", Msg: &pb.HostMsg_GrantStateQuery{GrantStateQuery: &pb.GrantStateQuery{SegmentId: 17}}})
	if out := p.recv(t); out.Id != "barrier" || out.GetGrantState().GetState() != pb.GrantStateKind_GRANT_STATE_INSTALLED {
		t.Fatal("release passed arrived reply", out)
	}
	data := make([]byte, 4)
	if err := m.CopyDescriptor(reply.Message.GetDataVolume().Segment, data); err != nil || data[0] != 1 || data[3] != 7 {
		t.Fatal(data, err)
	}
	reply.Close()
	if out := p.recv(t); out.GetReleaseDeposit() == nil {
		t.Fatal(out)
	}
	if out := p.recv(t); out.GetSegmentReleased().GetSegmentId() != 17 {
		t.Fatal("last close did not promptly settle", out)
	}
	p.send(t, &pb.HostMsg{Id: "after", Msg: &pb.HostMsg_GrantStateQuery{GrantStateQuery: &pb.GrantStateQuery{SegmentId: 17}}})
	if out := p.recv(t); out.Id != "after" {
		t.Fatal("duplicate automatic release", out)
	}
}

func TestPanelImageDuringRetirementHeldForNextSegment(t *testing.T) {
	m, c, _ := memoryPeerFor(t, true, "P", pb.RegionProducer_REGION_PRODUCER_PLUGIN)
	s := &Session{Client: c, Memory: m, Shortfalls: NewShortfalls()}
	seg, _ := m.Registry.Segment(17)
	key, err := seg.WriteLoan(0, []byte("unfinished"))
	if err != nil {
		t.Fatal(err)
	}
	if err = m.Apply(c, &pb.HostMsg{Msg: &pb.HostMsg_RetireSegment{RetireSegment: &pb.RetireSegment{SegmentId: 17}}}); err != nil {
		t.Fatal(err)
	}
	imageKey := PanelImageKey{Panel: "p", Widget: "plot"}
	for _, id := range []uint64{0, 17} {
		if err = s.PublishPanelImage(imageKey, id, []byte("held")); err != nil {
			t.Fatal(err)
		}
	}
	if len(c.heldImages) != 1 || s.Memory.DroppedPublications() != 0 {
		t.Fatal("panel image dropped", c.heldImages)
	}
	if err = seg.ReleaseLoan(key); err != nil {
		t.Fatal(err)
	}
	if msg := nextBatch(t, c)[0]; msg.GetSegmentReleased().GetSegmentId() != 17 {
		t.Fatal(msg)
	}
	grantSuccessor(t, m, c, "P", "p", 0) // reopening need not declare supersedes
	if msg := nextBatch(t, c)[0]; msg.GetGrantAck().GetSegmentId() != 18 {
		t.Fatal(msg)
	}
	msg := nextBatch(t, c)[0]
	if got := msg.GetPanelPatch().GetOps(); len(got) != 1 || got[0].GetSetImage().GetSegmentId() != 18 {
		t.Fatal(msg)
	}
	if len(c.heldImages) != 0 {
		t.Fatal("held image not drained")
	}
}
