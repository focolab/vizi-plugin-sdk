//go:build linux || darwin

package control

import (
	"bytes"
	"context"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	"testing"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func TestRawHeldImageResolvesListAndHonorsPanelByteBudget(t *testing.T) {
	registry, err := memory.New(memory.Config{})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { registry.Close() })
	m, c := NewMemory(registry), stateClient(t)
	c.cfg.Panels = map[string]PanelLayout{"p": {Lists: map[string]int{"rows": 8, "other": 8}, Cells: map[string]string{"image": "rows", "other-image": "other"}}}
	s := &Session{Client: c, Memory: m, Shortfalls: NewShortfalls()}
	one := PanelImageKey{Panel: "p", Widget: "image", Row: "same"}
	two := PanelImageKey{Panel: "p", Widget: "other-image", Row: "same"}
	if err := s.PublishPanelImage(one, 0, bytes.Repeat([]byte{1}, HeldImageBytes)); err != nil {
		t.Fatal(err)
	}
	if err := s.PublishPanelImage(two, 0, []byte{2}); err == nil {
		t.Fatal("exceeded panel budget")
	}
	if err := s.PublishPanelImage(one, 0, []byte{3}); err != nil {
		t.Fatal(err)
	}
	if err := s.PublishPanelImage(two, 0, []byte{2}); err != nil {
		t.Fatal(err)
	}
	if err := c.Send(patch("p", rowKeys())); err != nil {
		t.Fatal(err)
	}
	if len(c.heldImages) != 1 || c.heldImages[two].list != "other" {
		t.Fatal("removing rows discarded another list", c.heldImages)
	}
	// Refused replacements leave the previous owned image intact.
	if err := s.PublishPanelImage(two, 0, make([]byte, HeldImageBytes+1)); err == nil {
		t.Fatal("oversized image held")
	}
	if !bytes.Equal(c.heldImages[two].data, []byte{2}) {
		t.Fatal("refusal destroyed previous image")
	}
}

func TestPanelImageHeldUntilOpenBeforeHandler(t *testing.T) {
	memoryPeerFor(t, false, "P", pb.RegionProducer_REGION_PRODUCER_PLUGIN, func(m *Memory, install *pb.InstallSegment, grant func(*Client)) {
		var session *Session
		key := PanelImageKey{Panel: "p", Widget: "image"}
		p := openPeer(t, Config{AdmitOutbound: m.AdmitOutbound, SentOutbound: m.SentOutbound, Arrange: m.Apply,
			OnMessage: func(_ context.Context, _ *Client, in *pb.HostMsg) error {
				if in.GetPanelOpened() != nil {
					return session.PublishPanelImage(key, 0, []byte("handler"))
				}
				return nil
			}}, nil)
		session = &Session{Client: p.client, Memory: m, Shortfalls: NewShortfalls()}
		data := []byte("latest")
		if err := session.PublishPanelImage(key, 0, []byte("old")); err != nil {
			t.Fatal(err)
		}
		if err := session.PublishPanelImage(key, 0, data); err != nil {
			t.Fatal(err)
		}
		data[0] = 'X'
		if got := string(p.client.heldImages[key].data); got != "latest" {
			t.Fatal("held image aliases caller's bytes", got)
		}
		// A synchronous query is a wire barrier proving the unopened image sent no patch.
		p.send(t, &pb.HostMsg{Id: "before-open", Msg: &pb.HostMsg_GrantStateQuery{GrantStateQuery: &pb.GrantStateQuery{SegmentId: 17}}})
		if got := p.recv(t); got.Id != "before-open" {
			t.Fatal(got)
		}
		p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_InstallSegment{InstallSegment: install}})
		grant(p.client)
		if p.recv(t).GetGrantAck().GetInstalled() == nil {
			t.Fatal("missing grant")
		}
		p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_PanelOpened{PanelOpened: &pb.PanelOpened{PanelId: "p"}}})
		seg, _ := m.Registry.Segment(17)
		for i, want := range []string{"latest", "handler"} {
			msg := p.recv(t).GetPanelPatch()
			if msg == nil || len(msg.Ops) != 1 {
				t.Fatal(msg)
			}
			ref := msg.Ops[0].GetSetImage()
			if ref == nil || ref.Len != uint64(len(want)) {
				t.Fatal(ref)
			}
			// The allocator's offsets prove there was no hidden allocation for "old".
			if ref.Offset != uint64(64+i*64) {
				t.Fatal(ref)
			}
		}
		if stats, _ := seg.LoanStats(0); stats.Outstanding != 2 {
			t.Fatal(stats)
		}
		p.client.panelMu.Lock()
		defer p.client.panelMu.Unlock()
		if len(p.client.heldImages) != 0 {
			t.Fatal("held images were not drained")
		}
	})
}

func TestSetRowsDropsOnlyRemovedHeldImageCells(t *testing.T) {
	memoryPeerFor(t, false, "P", pb.RegionProducer_REGION_PRODUCER_PLUGIN, func(m *Memory, install *pb.InstallSegment, grant func(*Client)) {
		p := openPeer(t, Config{AdmitOutbound: m.AdmitOutbound, SentOutbound: m.SentOutbound, Arrange: m.Apply, OnMessage: func(context.Context, *Client, *pb.HostMsg) error { return nil }}, nil)
		session := &Session{Client: p.client, Memory: m, Shortfalls: NewShortfalls()}
		for _, key := range []PanelImageKey{{"p", "cell", "gone", "rows"}, {"p", "other", "same", "other-list"}, {"p", "cell", "kept", "rows"}} {
			if err := session.PublishPanelImage(key, 0, []byte("image")); err != nil {
				t.Fatal(err)
			}
		}
		if err := p.client.Send(patch("p", &pb.PatchOp{WidgetId: "rows", Op: &pb.PatchOp_SetRows{SetRows: &pb.RowKeys{Keys: []string{"kept"}}}})); err != nil {
			t.Fatal(err)
		}
		if p.recv(t).GetPanelPatch().Ops[0].GetSetRows() == nil {
			t.Fatal("missing rows")
		}
		p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_InstallSegment{InstallSegment: install}})
		grant(p.client)
		if p.recv(t).GetGrantAck().GetInstalled() == nil {
			t.Fatal("missing grant")
		}
		p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_PanelOpened{PanelOpened: &pb.PanelOpened{PanelId: "p"}}})
		seen := map[string]bool{}
		for range 2 {
			msg := p.recv(t).GetPanelPatch()
			if msg == nil || len(msg.Ops) != 1 || msg.Ops[0].GetSetImage() == nil {
				t.Fatal(msg)
			}
			seen[msg.Ops[0].RowKey] = true
		}
		if seen["gone"] || !seen["same"] || !seen["kept"] {
			t.Fatal(seen)
		}
	})
}
