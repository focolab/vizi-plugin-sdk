package control

import (
	"context"
	"sync/atomic"
	"testing"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/violations"
	"google.golang.org/protobuf/proto"
)

func TestRepeatedFieldRefusalReportsAndKeepsRouting(t *testing.T) {
	var applied atomic.Int32
	reporter, err := violations.New(violations.Config{})
	if err != nil {
		t.Fatal(err)
	}
	defer reporter.Close()
	p := openPeer(t, Config{Reporter: reporter, Arrange: func(c *Client, m *pb.HostMsg) error {
		if m.GetEpochChange() != nil {
			applied.Add(1)
		}
		return c.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_ToolBindingAck{ToolBindingAck: &pb.ToolBindingAck{Tool: m.GetToolBinding().GetTool(), Revision: 1}}})
	}}, nil)
	datasets := make([]*pb.DatasetState, 65537)
	for i := range datasets {
		datasets[i] = &pb.DatasetState{}
	}
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_EpochChange{EpochChange: &pb.EpochChange{Datasets: datasets}}})
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_ToolBinding{ToolBinding: &pb.ToolBinding{Tool: 8, Revision: 1}}})
	report, ack := false, false
	for range 2 {
		m := p.recv(t)
		if v := m.GetContractViolation(); v != nil {
			report = v.Where == pb.ViolationSite_VIOLATION_SITE_REPEATED_FIELD_OVERFLOW && v.Count == 1
		}
		if a := m.GetToolBindingAck(); a != nil {
			ack = a.Tool == 8
		}
	}
	if !report || !ack || applied.Load() != 0 {
		t.Fatal(report, ack, applied.Load())
	}
}

func TestShutdownRefusesBeforeRideAlongInstall(t *testing.T) {
	hookStarted := make(chan struct{})
	finish := make(chan struct{})
	var admitted, prepared, released atomic.Int32
	p := openPeer(t, Config{
		Shutdown: func(ctx context.Context, _ *Client, _ *pb.Shutdown) error {
			close(hookStarted)
			select {
			case <-finish:
				return nil
			case <-ctx.Done():
				return ctx.Err()
			}
		},
		Admit:   func(*Client, *pb.HostMsg) error { admitted.Add(1); return nil },
		Prepare: func(context.Context, *Client, *pb.HostMsg) error { prepared.Add(1); return nil },
		Release: func(*Client, *pb.HostMsg) error { released.Add(1); return nil },
	}, nil)
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_Shutdown{Shutdown: &pb.Shutdown{DeadlineMs: proto.Uint32(1000)}}})
	select {
	case <-hookStarted:
	case <-p.ctx.Done():
		t.Fatal(p.ctx.Err())
	}
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_Shutdown{Shutdown: &pb.Shutdown{DeadlineMs: proto.Uint32(10000)}}})
	p.send(t, &pb.HostMsg{Id: "late", Msg: &pb.HostMsg_ExportChunk{ExportChunk: &pb.ExportChunk{Source: &pb.ExportChunk_Segment{Segment: &pb.SegmentRef{SegmentId: 7, Len: 4}}, Install: &pb.InstallSegment{SegmentId: 7, Purpose: "X"}}}})
	response := p.recv(t)
	if response.Id != "late" || response.GetError().GetKind() != pb.Error_PROTOCOL {
		t.Fatal(response)
	}
	close(finish)
	if report := p.recv(t).GetShutdownReport(); report.GetOutcome() != pb.ShutdownReport_COMPLETED {
		t.Fatal(report)
	}
	if admitted.Load() != 0 || prepared.Load() != 0 || released.Load() != 1 {
		t.Fatal(admitted.Load(), prepared.Load(), released.Load())
	}
}

func TestShutdownLetsPreviouslyAdmittedDescriptorFinish(t *testing.T) {
	preparing := make(chan struct{})
	hook := make(chan struct{})
	release := make(chan struct{})
	p := openPeer(t, Config{
		Prepare: func(ctx context.Context, _ *Client, _ *pb.HostMsg) error {
			close(preparing)
			select {
			case <-release:
				return nil
			case <-ctx.Done():
				return ctx.Err()
			}
		},
		Release:  func(*Client, *pb.HostMsg) error { return nil },
		Shutdown: func(context.Context, *Client, *pb.Shutdown) error { close(hook); return nil },
		HandleRequest: func(context.Context, *Client, *pb.HostMsg) (*pb.PluginMsg, error) {
			return &pb.PluginMsg{Msg: &pb.PluginMsg_ChunkWritten{ChunkWritten: &pb.ChunkWritten{TotalBytes: 4}}}, nil
		},
	}, nil)
	p.send(t, &pb.HostMsg{Id: "accepted", Msg: &pb.HostMsg_ExportChunk{ExportChunk: &pb.ExportChunk{Source: &pb.ExportChunk_Segment{Segment: &pb.SegmentRef{SegmentId: 1, Len: 4}}}}})
	select {
	case <-preparing:
	case <-p.ctx.Done():
		t.Fatal(p.ctx.Err())
	}
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_Shutdown{Shutdown: &pb.Shutdown{DeadlineMs: proto.Uint32(1000)}}})
	select {
	case <-hook:
	case <-p.ctx.Done():
		t.Fatal(p.ctx.Err())
	}
	close(release)
	if reply := p.recv(t); reply.Id != "accepted" || reply.GetChunkWritten().GetTotalBytes() != 4 {
		t.Fatal("pre-existing request refused", reply)
	}
	if report := p.recv(t).GetShutdownReport(); report.GetOutcome() != pb.ShutdownReport_COMPLETED {
		t.Fatal(report)
	}
}
