package control

import (
	"context"
	"errors"
	"testing"
	"testing/synctest"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func TestDroppedDescriptorReplyUnblocksCaller(t *testing.T) {
	r, _ := memory.New(memory.Config{})
	defer r.Close()
	m := NewMemory(r)
	p := openPeer(t, Config{Admit: m.Admit, Prepare: m.WaitDescriptor, Release: m.Release, Arrange: m.Apply}, nil)
	result := make(chan error, 1)
	go func() {
		reply, err := p.client.Call(p.ctx, &pb.PluginMsg{Msg: &pb.PluginMsg_ReadDataVolume{ReadDataVolume: &pb.ReadDataVolume{}}})
		if reply != nil {
			_ = reply.Close()
		}
		result <- err
	}()
	request := p.recv(t)
	p.send(t, &pb.HostMsg{Id: request.Id, Msg: &pb.HostMsg_DataVolume{DataVolume: &pb.DataVolume{Segment: &pb.SegmentRef{SegmentId: 17, Offset: 128, Len: 4}}}})
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_RetireSegment{RetireSegment: &pb.RetireSegment{SegmentId: 17}}})
	if got := p.recv(t); got.GetSegmentReleased() == nil {
		t.Fatal(got)
	}
	select {
	case err := <-result:
		var hostErr *HostError
		if !errors.As(err, &hostErr) || hostErr.Kind != pb.Error_NOT_FOUND {
			t.Fatal("discarded reply left caller waiting", err)
		}
	case <-p.ctx.Done():
		t.Fatal(p.ctx.Err())
	}
	if m.DroppedDescriptors() != 1 {
		t.Fatal(m.DroppedDescriptors())
	}
}

func TestPreparationPreservesSegmentOrderWithoutBlockingOtherSegments(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		unblock := make(chan struct{})
		p := openPeer(t, Config{
			Prepare: func(ctx context.Context, _ *Client, m *pb.HostMsg) error {
				if m.Id == "first" {
					select {
					case <-unblock:
					case <-ctx.Done():
						return ctx.Err()
					}
				}
				return nil
			},
			Release: func(*Client, *pb.HostMsg) error { return nil },
			HandleRequest: func(context.Context, *Client, *pb.HostMsg) (*pb.PluginMsg, error) {
				return &pb.PluginMsg{Msg: &pb.PluginMsg_ChunkWritten{ChunkWritten: &pb.ChunkWritten{}}}, nil
			},
		}, nil)
		for _, x := range []struct {
			id      string
			segment uint64
		}{{"first", 1}, {"second", 1}, {"unrelated", 2}} {
			p.send(t, &pb.HostMsg{Id: x.id, Msg: &pb.HostMsg_ExportChunk{ExportChunk: &pb.ExportChunk{Source: &pb.ExportChunk_Segment{Segment: &pb.SegmentRef{SegmentId: x.segment}}}}})
		}
		synctest.Wait() // first is blocked, all runnable preparations have finished
		if got := p.recv(t); got.Id != "unrelated" {
			t.Fatal("blocked segment prevented independent progress or reordered", got)
		}
		close(unblock)
		for _, id := range []string{"first", "second"} {
			if got := p.recv(t); got.Id != id {
				t.Fatal("same-segment descriptors reordered", got)
			}
		}
	})
}
