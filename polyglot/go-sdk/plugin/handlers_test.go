package plugin

import (
	"context"
	"errors"
	"net"
	"strings"
	"testing"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"
)

func TestTypedCommandCanCallHostAndPanelRunsIndependently(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	opened := make(chan struct{}, 1)
	h := Handlers{
		Commands: map[string]CommandHandler{"inspect": func(ctx context.Context, c *control.Client, args Args) (Result, error) {
			flag, err := args.Bool("flag")
			if err != nil {
				return Result{}, err
			}
			if flag {
				return Result{}, errors.New("expected false")
			}
			view, err := c.GetViewState(ctx)
			if err != nil {
				return Result{}, err
			}
			return Result{Message: view.DatasetName}, nil
		}},
		Panels: map[string]PanelOpenedHandler{"p": func(context.Context, *control.Client) error { opened <- struct{}{}; return nil }},
	}
	cfg, err := h.Configure(control.Config{BuildIdentity: "typed.test test build", PluginID: "typed.test", Handshake: func(*pb.Hello) error { return nil }})
	if err != nil {
		t.Fatal(err)
	}
	// Registrations are snapshots, not live maps read by a worker.
	delete(h.Commands, "inspect")
	delete(h.Panels, "p")
	server, err := control.NewServer(cfg)
	if err != nil {
		t.Fatal(err)
	}
	listener := bufconn.Listen(1 << 20)
	grpcServer := server.GRPCServer()
	go grpcServer.Serve(listener)
	defer grpcServer.Stop()
	defer listener.Close()
	conn, err := grpc.NewClient("passthrough:///test", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	stream, err := pb.NewPluginClient(conn).Session(ctx)
	if err != nil {
		t.Fatal(err)
	}
	send := func(m *pb.HostMsg) {
		t.Helper()
		if err := stream.Send(m); err != nil {
			t.Fatal(err)
		}
	}
	recv := func() *pb.PluginMsg {
		t.Helper()
		m, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		return m
	}
	send(&pb.HostMsg{Id: "Hello from test host 1.0", Msg: &pb.HostMsg_Hello{Hello: &pb.Hello{ProtocolMajor: 1, ProtocolMinor: 0}}})
	if recv().GetReady() == nil {
		t.Fatal("not ready")
	}
	send(&pb.HostMsg{Id: "cmd", Msg: &pb.HostMsg_InvokeCommand{InvokeCommand: &pb.InvokeCommand{CommandId: "inspect", Args: map[string]*pb.Value{"flag": Bool(false)}}}})
	request := recv()
	if request.GetGetViewState() == nil || request.Id == "" {
		t.Fatal(request)
	}
	send(&pb.HostMsg{Msg: &pb.HostMsg_PanelOpened{PanelOpened: &pb.PanelOpened{PanelId: "p"}}})
	select {
	case <-opened:
	case <-ctx.Done():
		t.Fatal("panel blocked behind command")
	}
	send(&pb.HostMsg{Id: request.Id, Msg: &pb.HostMsg_ViewState{ViewState: &pb.ViewState{DatasetName: "owned snapshot"}}})
	done := recv()
	if done.Id != "cmd" || done.GetCommandDone() == nil || !done.GetCommandDone().Ok || done.GetCommandDone().CommandId != "inspect" || done.GetCommandDone().Message != "owned snapshot" {
		t.Fatal(done)
	}
	send(&pb.HostMsg{Id: "missing-arg", Msg: &pb.HostMsg_InvokeCommand{InvokeCommand: &pb.InvokeCommand{CommandId: "inspect"}}})
	if done := recv(); done.Id != "missing-arg" || done.GetCommandDone() == nil || done.GetCommandDone().Ok || !strings.Contains(done.GetCommandDone().Message, "flag") {
		t.Fatal(done)
	}
	send(&pb.HostMsg{Id: "reader", Msg: &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{Uri: "x"}}})
	if reply := recv(); reply.Id != "reader" || reply.GetError() == nil || reply.GetError().Kind != pb.Error_UNSUPPORTED {
		t.Fatal(reply)
	}
}

func TestTypedRepliesAndErrors(t *testing.T) {
	original := &control.HostError{Kind: pb.Error_IO_ERROR, Message: "disk full"}
	h := Handlers{Reader: ReaderHandlers{
		Open: func(context.Context, *control.Client, *pb.OpenReader) (*pb.ReaderOpened, error) {
			return &pb.ReaderOpened{Handle: "r"}, nil
		},
		Volume: func(context.Context, *control.Client, *pb.ReadVolume) (*pb.SegmentRef, error) { return nil, original },
		Plane:  func(context.Context, *control.Client, *pb.ReadPlane) (*pb.SegmentRef, error) { return nil, nil },
	}, Export: ExportHandlers{Abort: func(_ context.Context, _ *control.Client, q *pb.AbortExport) (*pb.ExportDone, error) {
		return &pb.ExportDone{Handle: q.Handle}, nil
	}}}
	cfg, err := h.Configure(control.Config{})
	if err != nil {
		t.Fatal(err)
	}
	r, err := cfg.HandleRequest(context.Background(), nil, &pb.HostMsg{Msg: &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{Uri: "x"}}})
	if err != nil || r.GetReaderOpened().Handle != "r" {
		t.Fatal(r, err)
	}
	_, err = cfg.HandleRequest(context.Background(), nil, &pb.HostMsg{Msg: &pb.HostMsg_ReadVolume{ReadVolume: &pb.ReadVolume{Handle: "r"}}})
	if !errors.Is(err, original) {
		t.Fatal(err)
	}
	_, err = cfg.HandleRequest(context.Background(), nil, &pb.HostMsg{Msg: &pb.HostMsg_ReadPlane{ReadPlane: &pb.ReadPlane{Handle: "r"}}})
	if err == nil {
		t.Fatal("nil successful reply accepted")
	}
	r, err = cfg.HandleRequest(context.Background(), nil, &pb.HostMsg{Msg: &pb.HostMsg_AbortExport{AbortExport: &pb.AbortExport{Handle: "x"}}})
	if err != nil || r.GetExportDone().Handle != "x" {
		t.Fatal(r, err)
	}
	if _, err := h.Configure(cfg); err == nil {
		t.Fatal("overwrote dispatcher")
	}
}

func TestArgumentsPreserveAbsenceAndTypes(t *testing.T) {
	a := Args{"s": String(""), "i": Int(0), "f": Float(0), "b": Bool(false), "nil": nil}
	if a.Has("absent") || a.Has("nil") || !a.Has("b") {
		t.Fatal(a)
	}
	if v, err := a.String("s"); v != "" || err != nil {
		t.Fatal(v, err)
	}
	if v, err := a.Int("i"); v != 0 || err != nil {
		t.Fatal(v, err)
	}
	if v, err := a.Float("f"); v != 0 || err != nil {
		t.Fatal(v, err)
	}
	if v, err := a.Bool("b"); v || err != nil {
		t.Fatal(v, err)
	}
	if _, err := a.Float("i"); err == nil {
		t.Fatal("implicit numeric conversion")
	}
	if _, err := a.Int("absent"); err == nil {
		t.Fatal("missing value became zero")
	}
}

func TestPlaneHandlerPriorityAndMissingMemory(t *testing.T) {
	q := &pb.HostMsg{Msg: &pb.HostMsg_ReadPlane{ReadPlane: &pb.ReadPlane{Handle: "local", Z: 1}}}
	h := Handlers{Reader: ReaderHandlers{Volume: func(context.Context, *control.Client, *pb.ReadVolume) (*pb.SegmentRef, error) {
		t.Fatal("volume callback must not run without fallback memory or over an explicit plane handler")
		return nil, nil
	}}}
	if _, err := h.handle(context.Background(), nil, q); err == nil || !strings.Contains(err.Error(), "ReaderMemory") {
		t.Fatal(err)
	}
	h.Reader.Plane = func(_ context.Context, _ *control.Client, in *pb.ReadPlane) (*pb.SegmentRef, error) {
		if in.Handle != "local" || in.Z != 1 {
			t.Fatal(in)
		}
		return &pb.SegmentRef{SegmentId: 7}, nil
	}
	if out, err := h.handle(context.Background(), nil, q); err != nil || out.GetSegment().SegmentId != 7 {
		t.Fatal(out, err)
	}
	h.Reader = ReaderHandlers{}
	_, err := h.handle(context.Background(), nil, q)
	var hostErr *control.HostError
	if !errors.As(err, &hostErr) || hostErr.Kind != pb.Error_UNSUPPORTED {
		t.Fatal(err)
	}
}
