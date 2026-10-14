package control

import (
	"context"
	"errors"
	"testing"
	"time"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func TestPickerTimeout(t *testing.T) {
	for _, picker := range []struct {
		name  string
		call  func(context.Context, *Client) (string, error)
		reply func(string) *pb.HostMsg
	}{
		{
			name: "request_asset",
			call: func(ctx context.Context, c *Client) (string, error) {
				r, err := c.RequestAsset(ctx, &pb.RequestAsset{Slot: "input"})
				return r.GetPath(), err
			},
			reply: func(id string) *pb.HostMsg {
				return &pb.HostMsg{Id: id, Msg: &pb.HostMsg_AssetResult{AssetResult: &pb.AssetResult{Slot: "input", Path: "/chosen/file"}}}
			},
		},
		{
			name: "request_save_path",
			call: func(ctx context.Context, c *Client) (string, error) {
				r, err := c.RequestSavePath(ctx, &pb.RequestSavePath{SuggestedName: "file"})
				return r.GetPath(), err
			},
			reply: func(id string) *pb.HostMsg {
				return &pb.HostMsg{Id: id, Msg: &pb.HostMsg_SavePathResult{SavePathResult: &pb.SavePathResult{Path: "/chosen/file"}}}
			},
		},
	} {
		t.Run(picker.name, func(t *testing.T) {
			t.Run("outlives_ordinary_request", func(t *testing.T) {
				p := openPeer(t, Config{RequestTimeout: 50 * time.Millisecond, PickerTimeout: 2 * time.Second}, nil)
				type result struct {
					path string
					err  error
				}
				done := make(chan result, 1)
				go func() {
					path, err := picker.call(p.ctx, p.client)
					done <- result{path, err}
				}()
				request := p.recv(t)
				if got := kind(request.ProtoReflect()); got != picker.name {
					t.Fatalf("request kind = %s", got)
				}
				// Start an ordinary request after the picker reaches the host. Its
				// timeout proves the picker has waited longer than RequestTimeout.
				ordinary := make(chan error, 1)
				go func() {
					_, err := p.client.GetViewState(p.ctx)
					ordinary <- err
				}()
				if request := p.recv(t); request.GetGetViewState() == nil {
					t.Fatalf("expected GetViewState, got %v", request)
				}
				if err := <-ordinary; !errors.Is(err, context.DeadlineExceeded) {
					t.Fatalf("ordinary request error = %v", err)
				}
				p.send(t, picker.reply(request.Id))
				got := <-done
				if got.err != nil || got.path != "/chosen/file" {
					t.Fatalf("picker result = %q, %v", got.path, got.err)
				}
			})
			t.Run("picker_backstop", func(t *testing.T) {
				p := openPeer(t, Config{RequestTimeout: 2 * time.Second, PickerTimeout: 50 * time.Millisecond}, nil)
				ctx, cancel := context.WithTimeout(p.ctx, time.Second)
				defer cancel()
				_, err := picker.call(ctx, p.client)
				if !errors.Is(err, context.DeadlineExceeded) || ctx.Err() != nil {
					t.Fatalf("picker backstop error = %v; caller context = %v", err, ctx.Err())
				}
			})
			t.Run("shorter_caller_deadline", func(t *testing.T) {
				p := openPeer(t, Config{RequestTimeout: 2 * time.Second, PickerTimeout: 2 * time.Second}, nil)
				ctx, cancel := context.WithTimeout(p.ctx, 50*time.Millisecond)
				defer cancel()
				done := make(chan error, 1)
				go func() {
					_, err := picker.call(ctx, p.client)
					done <- err
				}()
				select {
				case err := <-done:
					if !errors.Is(err, context.DeadlineExceeded) || ctx.Err() == nil {
						t.Fatalf("picker error = %v; caller context = %v", err, ctx.Err())
					}
				case <-time.After(time.Second):
					t.Fatal("picker ignored shorter caller deadline")
				}
			})
		})
	}
}

func TestPickerTimeoutConfig(t *testing.T) {
	cfg := Config{PluginID: "test.plugin", BuildIdentity: "test build", Handshake: func(*pb.Hello) error { return nil }}
	s, err := NewServer(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if s.cfg.RequestTimeout != 30*time.Second || s.cfg.PickerTimeout != 30*time.Minute {
		t.Fatalf("default timeouts: request=%v picker=%v", s.cfg.RequestTimeout, s.cfg.PickerTimeout)
	}
	cfg.PickerTimeout = -time.Second
	if _, err := NewServer(cfg); err == nil {
		t.Fatal("negative PickerTimeout accepted")
	}
}
