package control

import (
	"context"
	"errors"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"testing"
)

func TestCallWithAdmission(t *testing.T) {
	for _, mode := range []string{"cancel-before", "admission-refusal", "cancel-after", "host-error", "success"} {
		t.Run(mode, func(t *testing.T) {
			refused := errors.New("local refusal")
			cfg := Config{}
			if mode == "admission-refusal" {
				cfg.AdmitOutbound = func(m *pb.PluginMsg) error {
					if m.GetGetViewState() != nil {
						return refused
					}
					return nil
				}
			}
			p := openPeer(t, cfg, nil)
			ctx, cancel := context.WithCancel(p.ctx)
			defer cancel()
			if mode == "cancel-before" {
				cancel()
			}
			type result struct {
				reply    *Reply
				admitted bool
				err      error
			}
			done := make(chan result, 1)
			go func() {
				r, a, e := p.client.CallWithAdmission(ctx, &pb.PluginMsg{Msg: &pb.PluginMsg_GetViewState{GetViewState: &pb.GetViewState{}}})
				done <- result{r, a, e}
			}()
			if mode != "cancel-before" && mode != "admission-refusal" {
				q := p.recv(t)
				switch mode {
				case "cancel-after":
					cancel()
				case "host-error":
					p.send(t, &pb.HostMsg{Id: q.Id, Msg: &pb.HostMsg_HostError{HostError: &pb.Error{Kind: pb.Error_INVALID_ARGUMENT, Message: "refused"}}})
				default:
					p.send(t, &pb.HostMsg{Id: q.Id, Msg: &pb.HostMsg_ViewState{ViewState: &pb.ViewState{}}})
				}
			}
			got := <-done
			if got.admitted != (mode != "cancel-before" && mode != "admission-refusal") {
				t.Fatalf("wrong admission: %+v", got)
			}
			if mode == "success" {
				if got.err != nil || got.reply == nil {
					t.Fatal(got)
				}
				_ = got.reply.Close()
			} else if got.err == nil {
				t.Fatal("missing refusal")
			}
		})
	}
}
