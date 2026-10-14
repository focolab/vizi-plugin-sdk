//go:build linux || darwin

package control

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"hash/crc32"
	"os"
	"runtime"
	"sync/atomic"
	"testing"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/violations"
	"golang.org/x/sys/unix"
	"google.golang.org/protobuf/proto"
)

// The wire counterpart runs through ServeEnv in plugin/reader_plane_unix_test.go.
func TestReleaseLoanCrossingRetirement(t *testing.T) {
	for _, releaseFirst := range []bool{false, true} {
		t.Run(fmt.Sprint("host-release-first=", releaseFirst), func(t *testing.T) {
			m, c, _ := memoryPeerFor(t, true, memory.PurposeOverlay, pb.RegionProducer_REGION_PRODUCER_PLUGIN)
			s, err := m.Registry.Segment(17)
			if err != nil {
				t.Fatal(err)
			}
			key, err := s.WriteLoan(0, []byte{1, 2, 3, 4})
			if err != nil {
				t.Fatal(err)
			}
			sent, err := m.Registry.AdmitLoans([]memory.LoanUse{{SegmentID: key.SegmentID, Offset: key.Offset, Length: key.Length}})
			if err != nil {
				t.Fatal(err)
			}
			sent()
			release := func(k memory.Deposit) error {
				return m.Apply(c, &pb.HostMsg{Msg: &pb.HostMsg_ReleaseLoan{ReleaseLoan: &pb.ReleaseLoan{SegmentId: k.SegmentID, Region: k.Region, Offset: k.Offset, Len: k.Length}}})
			}
			unknown := func(k memory.Deposit) {
				t.Helper()
				var violation *memory.Violation
				if err := release(k); !errors.As(err, &violation) || violation.Where != "unknown_release" {
					t.Fatalf("unknown key %+v: %v", k, err)
				}
			}
			if releaseFirst {
				if err := release(key); err != nil {
					t.Fatal(err)
				}
			}
			if _, err := m.Registry.Retire(17); err != nil {
				t.Fatal(err)
			}
			// A repeated retire must not discard the outstanding crossing keys.
			if _, err := m.Registry.Retire(17); err != nil {
				t.Fatal(err)
			}
			for _, wrong := range []memory.Deposit{
				{SegmentID: 999, Region: key.Region, Offset: key.Offset, Length: key.Length},
				{SegmentID: 17, Region: key.Region + 1, Offset: key.Offset, Length: key.Length},
				{SegmentID: 17, Region: key.Region, Offset: key.Offset + 1, Length: key.Length},
				{SegmentID: 17, Region: key.Region, Offset: key.Offset, Length: key.Length + 1},
			} {
				unknown(wrong)
			}
			want := uint64(0)
			if !releaseFirst {
				if err := release(key); err != nil {
					t.Fatal(err)
				}
				want = 1
			}
			unknown(key) // duplicate or already settled before retirement
			if got := m.CrossedReleases(); got != want {
				t.Fatalf("crossings %d, want %d", got, want)
			}
			if got := m.Registry.Status(17).State; got != memory.Released {
				t.Fatal(got)
			}
			select {
			case out := <-c.out:
				t.Fatal("crossing emitted a message", out.msg)
			default:
			}
		})
	}
}

// This host fixture is built directly from M/preamble/region documentation. It
// grants a real shared file over SCM_RIGHTS; no existing SDK generates its data.
func memoryPeer(t *testing.T, capabilityFirst bool) (*Memory, *Client, *pb.SegmentRef) {
	return memoryPeerFor(t, capabilityFirst, "test.data", pb.RegionProducer_REGION_PRODUCER_HOST)
}

func memoryPeerFor(t *testing.T, capabilityFirst bool, purpose string, producer pb.RegionProducer, setup ...func(*Memory, *pb.InstallSegment, func(*Client))) (*Memory, *Client, *pb.SegmentRef) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	t.Cleanup(cancel)
	r, err := memory.New(memory.Config{Purposes: []string{purpose}, Contributions: map[string][]string{"O": {"o"}, "P": {"p"}}})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { r.Close() })
	m := NewMemory(r)
	cctx, stop := context.WithCancelCause(ctx)
	t.Cleanup(func() { stop(ErrClosed) })
	c := &Client{ctx: cctx, cancel: stop, cfg: Config{Diagnostic: func(e error) { t.Log(e) }, AdmitOutbound: m.AdmitOutbound, SentOutbound: m.SentOutbound}, out: make(chan outbound, 16)}
	file, err := os.CreateTemp(t.TempDir(), "mapping")
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	data := make([]byte, 256)
	binary.LittleEndian.PutUint64(data, 17)
	copy(data[12:28], "identity-proof!!")
	binary.LittleEndian.PutUint32(data[28:32], crc32.ChecksumIEEE(data[:28]))
	copy(data[128:132], []byte{1, 3, 5, 7})
	if _, err := file.Write(data); err != nil {
		t.Fatal(err)
	}
	sockType := unix.SOCK_SEQPACKET
	if runtime.GOOS == "darwin" {
		sockType = unix.SOCK_DGRAM
	}
	pair, err := unix.Socketpair(unix.AF_UNIX, sockType, 0)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { unix.Close(pair[0]) })
	installed := make(chan error, 1)
	var grantClient atomic.Pointer[Client]
	grantClient.Store(c)
	db, err := doorbell.Adopt(pair[1], doorbell.Config{Handler: func(e doorbell.Event) {
		if e.Frame != nil {
			installed <- m.Grant(grantClient.Load(), e.Frame)
		}
	}})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Stop(); db.Wait(ctx) })
	install := &pb.InstallSegment{SegmentId: 17, Purpose: purpose, Regions: []*pb.Region{{Index: 0, Offset: 64, Extent: 192, Discipline: pb.RegionDiscipline_REGION_DISCIPLINE_LOANED, Producer: producer, SlotBytes: 0}}}
	if purpose == memory.PurposePanel {
		install.Subject = "p"
	}
	if purpose == memory.PurposeOverlay {
		install.Subject = "o"
	}
	raw, err := doorbell.EncodeMemory(doorbell.Memory{SegmentID: 17, Size: 256, Permissions: doorbell.PluginRead | doorbell.PluginWrite}, 1)
	if err != nil {
		t.Fatal(err)
	}
	grant := func(target *Client) {
		grantClient.Store(target)
		if err := unix.Sendmsg(pair[0], raw[:], unix.UnixRights(int(file.Fd())), nil, 0); err != nil {
			t.Fatal(err)
		}
		select {
		case err := <-installed:
			if err != nil {
				t.Fatal(err)
			}
		case <-ctx.Done():
			t.Fatal(ctx.Err())
		}
	}
	if len(setup) != 0 {
		setup[0](m, install, grant)
		return m, c, nil
	}
	if !capabilityFirst {
		if err := m.Install(c, install); err != nil {
			t.Fatal(err)
		}
	}
	grant(c)
	if capabilityFirst {
		select {
		case <-c.out:
			t.Fatal("ack before meaning")
		default:
		}
		if err := m.Install(c, install); err != nil {
			t.Fatal(err)
		}
	}
	select {
	case out := <-c.out:
		ack := out.msg.GetGrantAck()
		if ack.GetInstalled() == nil || string(ack.GetInstalled().IdentityProof) != "identity-proof!!" {
			t.Fatal(out.msg)
		}
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
	return m, c, &pb.SegmentRef{SegmentId: 17, Offset: 128, Len: 4, ShapeZyx: []uint32{1, 1, 4}, Dtype: pb.DType_DTYPE_U8}
}

func TestMemoryBridgeBothArrivalOrdersAndUses(t *testing.T) {
	for _, first := range []bool{false, true} {
		t.Run(map[bool]string{false: "install_first", true: "capability_first"}[first], func(t *testing.T) {
			m, c, d := memoryPeer(t, first)
			d.Uses = 2
			data := make([]byte, 4)
			if err := m.CopyDescriptor(d, data); err != nil {
				t.Fatal(err)
			}
			if string(data) != string([]byte{1, 3, 5, 7}) {
				t.Fatal(data)
			}
			if err := m.Release(c, &pb.HostMsg{Msg: &pb.HostMsg_DataVolume{DataVolume: &pb.DataVolume{Segment: d}}}); err != nil {
				t.Fatal(err)
			}
			select {
			case <-c.out:
				t.Fatal("released before second use arrived")
			default:
			}
			if err := m.Release(c, &pb.HostMsg{Msg: &pb.HostMsg_MaskVoxels{MaskVoxels: &pb.MaskVoxels{Segment: d}}}); err != nil {
				t.Fatal(err)
			}
			release := (<-c.out).msg.GetReleaseDeposit()
			if release.Offset != 128 || release.Len != 4 || release.Region != 0 {
				t.Fatal(release)
			}
		})
	}
}

func TestMemoryBridgeReleaseAfterFailedCopyAndRetirement(t *testing.T) {
	m, c, d := memoryPeer(t, false)
	if err := m.CopyDescriptor(d, make([]byte, 2)); !errors.Is(err, memory.ErrBounds) {
		t.Fatal(err)
	}
	if err := m.Release(c, &pb.HostMsg{Msg: &pb.HostMsg_DataVolume{DataVolume: &pb.DataVolume{Segment: d}}}); err != nil {
		t.Fatal(err)
	}
	if (<-c.out).msg.GetReleaseDeposit() == nil {
		t.Fatal("failed copy leaked deposit")
	}
	if err := m.Apply(c, &pb.HostMsg{Msg: &pb.HostMsg_RetireSegment{RetireSegment: &pb.RetireSegment{SegmentId: 17}}}); err != nil {
		t.Fatal(err)
	}
	if (<-c.out).msg.GetSegmentReleased().GetSegmentId() != 17 {
		t.Fatal("missing release")
	}
	if err := m.Apply(c, &pb.HostMsg{Id: "query", Msg: &pb.HostMsg_GrantStateQuery{GrantStateQuery: &pb.GrantStateQuery{SegmentId: 17}}}); err != nil {
		t.Fatal(err)
	}
	answer := (<-c.out).msg
	state := answer.GetGrantState()
	if answer.Id != "query" || state.GetSegmentId() != 17 || state.GetState() != pb.GrantStateKind_GRANT_STATE_RELEASED {
		t.Fatal(answer)
	}
	if state.HaveCapability != nil || state.HaveInstall != nil || state.IdentityProof != nil || state.FailureClass != nil || state.Detail != nil {
		t.Fatal("released answer carries facts from another state", state)
	}
}

func TestUsesKeepsFirstReceivedCountDespiteReverseCompletion(t *testing.T) {
	m, c, d := memoryPeer(t, true)
	reporter, err := violations.New(violations.Config{})
	if err != nil {
		t.Fatal(err)
	}
	defer reporter.Close()
	c.cfg.Reporter = reporter
	d.Uses = 2
	first := &pb.HostMsg{Msg: &pb.HostMsg_DataVolume{DataVolume: &pb.DataVolume{Segment: d}}}
	other := proto.Clone(first).(*pb.HostMsg)
	other.GetDataVolume().Segment.Uses = 3
	if err := m.Admit(c, first); err != nil {
		t.Fatal(err)
	}
	if err := m.Admit(c, other); err != nil {
		t.Fatal(err)
	}
	if err := m.Release(c, other); err != nil {
		t.Fatal(err)
	}
	if len(c.out) != 0 {
		t.Fatal("released early")
	}
	if err := m.Release(c, first); err != nil {
		t.Fatal(err)
	}
	if release := nextBatch(t, c)[0].GetReleaseDeposit(); release.GetLen() != 4 {
		t.Fatal("did not use first declared count", release)
	}
	if count := reporter.Stats()[violations.UsesDisagreement].Observed; count != 1 {
		t.Fatal("mismatch must be counted once across admission and release", count)
	}
}

func TestUsesDisagreementReportsAndReleasesOverWire(t *testing.T) {
	m, _, d := memoryPeer(t, true)
	p := openPeer(t, Config{Admit: m.Admit, Prepare: m.WaitDescriptor, Release: m.Release}, nil)
	d.Uses = 2
	first := &pb.HostMsg{Id: "late-first", Msg: &pb.HostMsg_DataVolume{DataVolume: &pb.DataVolume{Segment: d}}}
	other := proto.Clone(first).(*pb.HostMsg)
	other.Id = "late-other"
	other.GetDataVolume().Segment.Uses = 3
	// Unmatched replies still own uses. Routing must report the mismatch and
	// dispose both without waiting for a nonexistent third use.
	p.send(t, first)
	p.send(t, other)
	var reported, released bool
	for range 2 {
		out := p.recv(t)
		if report := out.GetContractViolation(); report != nil {
			if reported || report.Where != pb.ViolationSite_VIOLATION_SITE_USES_DISAGREEMENT || report.Count != 1 {
				t.Fatal(out)
			}
			reported = true
		} else if release := out.GetReleaseDeposit(); release != nil {
			if released || release.SegmentId != 17 || release.Offset != 128 || release.Len != 4 {
				t.Fatal(out)
			}
			released = true
		} else {
			t.Fatal(out)
		}
	}
	if !reported || !released {
		t.Fatal("missing report or release")
	}
	if count := p.client.cfg.Reporter.Stats()[violations.UsesDisagreement].Observed; count != 1 {
		t.Fatal("disposition reported the admission twice", count)
	}
}

func TestReaderGeometryRejectionReclaimsOnlyUnpublishedLoan(t *testing.T) {
	for _, admitted := range []bool{false, true} {
		t.Run(map[bool]string{false: "unpublished", true: "admitted"}[admitted], func(t *testing.T) {
			m, c, _ := memoryPeerFor(t, true, memory.PurposeReader, pb.RegionProducer_REGION_PRODUCER_PLUGIN)
			c.cfg.ReaderMemory = m
			seg, err := m.Registry.Segment(17)
			if err != nil {
				t.Fatal(err)
			}
			key, err := seg.WriteLoan(0, []byte{1, 2, 3, 4})
			if err != nil {
				t.Fatal(err)
			}
			d := &pb.SegmentRef{SegmentId: 17, Offset: key.Offset, Len: key.Length, Dtype: pb.DType_DTYPE_U8, ShapeZyx: []uint32{1, 1, 4}}
			if admitted {
				if err := m.AdmitOutbound(&pb.PluginMsg{Id: "read", Msg: &pb.PluginMsg_Segment{Segment: d}}); err != nil {
					t.Fatal(err)
				}
			}
			err = c.ValidateReaderReply(d, &pb.ReaderOpened{Handle: "r", ShapeTczyx: []uint32{1, 1, 1, 2, 2}, Dtype: pb.DType_DTYPE_U8}, false)
			if err == nil {
				t.Fatal("mismatched geometry accepted")
			}
			stats, errStats := seg.LoanStats(0)
			if errStats != nil {
				t.Fatal(errStats)
			}
			if admitted {
				if !errors.Is(err, memory.ErrLoanAdmitted) || stats.Outstanding != 1 {
					t.Fatal("host-owned loan reclaimed", err, stats)
				}
			} else if stats.Outstanding != 0 {
				t.Fatal("rejected unpublished loan leaked", stats)
			}
		})
	}
}

func TestRetiredDepositDispositionIsQuiet(t *testing.T) {
	m, c, ref := memoryPeer(t, true)
	if err := m.Apply(c, &pb.HostMsg{Msg: &pb.HostMsg_RetireSegment{RetireSegment: &pb.RetireSegment{SegmentId: 17}}}); err != nil {
		t.Fatal(err)
	}
	if msg := nextBatch(t, c)[0]; msg.GetSegmentReleased() == nil {
		t.Fatal(msg)
	}
	err := m.CopyDescriptor(ref, make([]byte, 4))
	if !errors.Is(err, ErrDescriptorDropped) || !errors.Is(err, memory.ErrRetired) {
		t.Fatal(err)
	}
	c.diagnostics = make(chan error, 1)
	c.Diagnose(err)
	select {
	case e := <-c.diagnostics:
		t.Fatal("normal retirement was diagnosed", e)
	default:
	}
	if err := m.Release(c, &pb.HostMsg{Msg: &pb.HostMsg_DataVolume{DataVolume: &pb.DataVolume{Segment: ref}}}); err != nil {
		t.Fatal(err)
	}
	if m.DroppedDescriptors() != 1 {
		t.Fatal(m.DroppedDescriptors())
	}
	c.Diagnose(memory.ErrBounds)
	select {
	case e := <-c.diagnostics:
		if !errors.Is(e, memory.ErrBounds) {
			t.Fatal(e)
		}
	default:
		t.Fatal("ordinary errors were silenced")
	}
}
