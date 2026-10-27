package plugin

import (
	"context"
	"fmt"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func TestTypedWorkCancellationSurvivesAdapters(t *testing.T) {
	for _, mode := range []string{"command", "reader", "series", "export"} {
		t.Run(mode, func(t *testing.T) {
			handlers := Handlers{}
			request := &pb.HostMsg{Id: "cancel"}
			switch mode {
			case "command":
				handlers.Commands = map[string]CommandHandler{"slow": func(ctx context.Context, _ *control.Client, _ Args) (Result, error) {
					control.StopWork(ctx, "stopped")
					return Result{}, nil
				}}
				request.Msg = &pb.HostMsg_InvokeCommand{InvokeCommand: &pb.InvokeCommand{CommandId: "slow"}}
			case "reader":
				handlers.Readers = map[string]ReaderHandlers{"r": {Open: func(ctx context.Context, _ *control.Client, _ *pb.OpenReader) (*pb.ReaderOpened, error) {
					return nil, fmt.Errorf("reader: %w", control.ErrCancelled)
				}}}
				request.Msg = &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{ReaderId: "r"}}
			case "series":
				// The series adapter has its own panic recovery and resource cleanup.
				handlers.Readers = map[string]ReaderHandlers{"r": {OpenSeries: func(ctx context.Context, _ *control.Client, _ *pb.OpenReader) (ReaderOpenResult, error) {
					control.StopWork(ctx, "stopped")
					return ReaderOpenResult{}, nil
				}}}
				request.Msg = &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{ReaderId: "r"}}
			case "export":
				handlers.Export = ExportHandlers{Open: func(ctx context.Context, _ *control.Client, _ *pb.OpenExport) (*pb.ExportOpened, error) {
					control.StopWork(ctx, "stopped")
					return nil, nil
				}}
				request.Msg = &pb.HostMsg_OpenExport{OpenExport: &pb.OpenExport{}}
			}
			_, _, call, diagnostics := seriesPeer(t, handlers)
			out := call(request)
			if out.Id != "cancel" || out.GetError().GetKind() != pb.Error_CANCELLED {
				t.Fatal(out)
			}
			select {
			case err := <-diagnostics:
				t.Fatal("cancellation diagnosed", err)
			default:
			}
		})
	}
}
