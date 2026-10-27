package plugin

import (
	"context"
	"errors"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func TestReadersSelectContributionAndIsolateLocalHandles(t *testing.T) {
	calls := map[string]int{}
	readers := map[string]ReaderHandlers{}
	for _, id := range []string{"a", "b"} {
		readers[id] = ReaderHandlers{
			Open: func(_ context.Context, _ *control.Client, q *pb.OpenReader) (*pb.ReaderOpened, error) {
				if q.ReaderId != id {
					t.Fatal("wrong reader", id, q)
				}
				return &pb.ReaderOpened{Handle: "local"}, nil
			},
			Metadata: func(_ context.Context, _ *control.Client, q *pb.Metadata) (*pb.MetadataResult, error) {
				if q.Handle != "local" {
					t.Fatal("wire handle leaked into callback", q)
				}
				calls[id]++
				return &pb.MetadataResult{}, nil
			},
			Close: func(_ context.Context, _ *control.Client, q *pb.CloseReader) (*pb.Closed, error) {
				return &pb.Closed{Handle: q.Handle}, nil
			},
		}
	}
	cfg, err := (Handlers{Readers: readers}).Configure(control.Config{})
	if err != nil {
		t.Fatal(err)
	}
	delete(readers, "b") // configured registry is a snapshot
	call := func(q *pb.HostMsg) (*pb.PluginMsg, error) { return cfg.HandleRequest(context.Background(), nil, q) }
	for _, id := range []string{"", "missing"} {
		_, err := call(&pb.HostMsg{Msg: &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{ReaderId: id}}})
		var hostErr *control.HostError
		if !errors.As(err, &hostErr) {
			t.Fatal("ambiguous/unknown reader accepted", id, err)
		}
	}
	handles := []string{}
	for _, id := range []string{"a", "b"} {
		out, err := call(&pb.HostMsg{Msg: &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{ReaderId: id}}})
		if err != nil {
			t.Fatal(err)
		}
		handles = append(handles, out.GetReaderOpened().Handle)
		if _, err := call(&pb.HostMsg{Msg: &pb.HostMsg_Metadata{Metadata: &pb.Metadata{Handle: handles[len(handles)-1]}}}); err != nil {
			t.Fatal(err)
		}
	}
	if handles[0] == handles[1] {
		t.Fatal("local handles collided")
	}
	for _, handle := range handles {
		out, err := call(&pb.HostMsg{Msg: &pb.HostMsg_CloseReader{CloseReader: &pb.CloseReader{Handle: handle}}})
		if err != nil || out.GetClosed().Handle != handle {
			t.Fatal(out, err)
		}
		if _, err := call(&pb.HostMsg{Msg: &pb.HostMsg_Metadata{Metadata: &pb.Metadata{Handle: handle}}}); err == nil {
			t.Fatal("closed reader still routed")
		}
	}
	if calls["a"] != 1 || calls["b"] != 1 {
		t.Fatal(calls)
	}
	single, err := (Handlers{Readers: readers}).Configure(control.Config{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := single.HandleRequest(context.Background(), nil, &pb.HostMsg{Msg: &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{}}}); err != nil {
		t.Fatal("single-reader fallback", err)
	}
}

func TestEmptyOpenHandleClosesRejectedReaderAndPreservesPrevious(t *testing.T) {
	for _, closeMode := range []string{"ok", "error", "panic"} {
		t.Run(closeMode, func(t *testing.T) {
			live := map[string]string{}
			closes := []string{}
			h := ReaderHandlers{
				Open: func(_ context.Context, _ *control.Client, q *pb.OpenReader) (*pb.ReaderOpened, error) {
					handle := "A"
					if q.Uri == "B" {
						handle = ""
					}
					live[handle] = q.Uri
					return &pb.ReaderOpened{Handle: handle}, nil
				},
				Close: func(_ context.Context, _ *control.Client, q *pb.CloseReader) (*pb.Closed, error) {
					closes = append(closes, live[q.Handle])
					delete(live, q.Handle)
					if q.Handle == "" {
						if closeMode == "panic" {
							panic("B close failed")
						}
						if closeMode == "error" {
							return nil, errors.New("B close failed")
						}
					}
					return &pb.Closed{Handle: q.Handle}, nil
				},
				Metadata: func(_ context.Context, _ *control.Client, q *pb.Metadata) (*pb.MetadataResult, error) {
					return &pb.MetadataResult{Text: live[q.Handle]}, nil
				},
			}
			cfg, err := (Handlers{Readers: map[string]ReaderHandlers{"r": h}}).Configure(control.Config{})
			if err != nil {
				t.Fatal(err)
			}
			call := func(q *pb.HostMsg) (*pb.PluginMsg, error) { return cfg.HandleRequest(context.Background(), nil, q) }
			first, err := call(&pb.HostMsg{Msg: &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{Uri: "A"}}})
			if err != nil {
				t.Fatal(err)
			}
			if _, err := call(&pb.HostMsg{Msg: &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{Uri: "B"}}}); err == nil {
				t.Fatal("empty handle accepted")
			}
			if len(live) != 1 || live["A"] != "A" || len(closes) != 1 || closes[0] != "B" {
				t.Fatal(live, closes)
			}
			meta, err := call(&pb.HostMsg{Msg: &pb.HostMsg_Metadata{Metadata: &pb.Metadata{Handle: first.GetReaderOpened().Handle}}})
			if err != nil || meta.GetMetadata().Text != "A" {
				t.Fatal(meta, err)
			}
			if err := cfg.Shutdown(context.Background(), nil, &pb.Shutdown{}); err != nil {
				t.Fatal(err)
			}
			if len(live) != 0 || len(closes) != 2 || closes[1] != "A" {
				t.Fatal(live, closes)
			}
		})
	}
}
