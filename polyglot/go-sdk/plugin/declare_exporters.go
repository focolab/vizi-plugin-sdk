package plugin

import (
	"context"
	"errors"
	"fmt"
	"math"
	"sync"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

type exportBinding struct {
	handlers ExportHandlers
	local    string
}
type exportRouter struct {
	mu        sync.Mutex
	exporters map[string]ExportHandlers
	handles   map[string]exportBinding
	next      uint64
}

func configureExporters(cfg control.Config, exporters map[string]ExportHandlers) control.Config {
	r := &exportRouter{exporters: exporters, handles: map[string]exportBinding{}}
	prior := cfg.HandleRequest
	cfg.HandleRequest = func(ctx context.Context, c *control.Client, in *pb.HostMsg) (*pb.PluginMsg, error) {
		switch in.GetMsg().(type) {
		case *pb.HostMsg_OpenExport, *pb.HostMsg_ExportChunk, *pb.HostMsg_FinishExport, *pb.HostMsg_AbortExport:
			return r.handle(ctx, c, in)
		}
		return prior(ctx, c, in)
	}
	shutdown := cfg.Shutdown
	cfg.Shutdown = func(ctx context.Context, c *control.Client, q *pb.Shutdown) error {
		err := r.closeAll(ctx, c)
		if shutdown != nil {
			err = errors.Join(err, shutdown(ctx, c, q))
		}
		return err
	}
	return cfg
}
func (r *exportRouter) handle(ctx context.Context, c *control.Client, in *pb.HostMsg) (*pb.PluginMsg, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if q := in.GetOpenExport(); q != nil {
		h, ok := r.exporters[q.ExporterId]
		if !ok {
			return nil, notFound("exporter", q.ExporterId)
		}
		if r.next == math.MaxUint64 {
			return nil, errors.New("export handle capacity exhausted")
		}
		out, err := (Handlers{Export: h}).handle(ctx, c, in)
		if err != nil {
			return nil, err
		}
		local := out.GetExportOpened().GetHandle()
		if local == "" {
			return nil, errors.New("export Open returned empty handle")
		}
		r.next++
		wire := fmt.Sprintf("go-export-%d", r.next)
		r.handles[wire] = exportBinding{h, local}
		out = proto.Clone(out).(*pb.PluginMsg)
		out.GetExportOpened().Handle = wire
		return out, nil
	}
	var wire string
	switch q := in.Msg.(type) {
	case *pb.HostMsg_ExportChunk:
		wire = q.ExportChunk.GetHandle()
	case *pb.HostMsg_FinishExport:
		wire = q.FinishExport.GetHandle()
	case *pb.HostMsg_AbortExport:
		wire = q.AbortExport.GetHandle()
	}
	b, ok := r.handles[wire]
	if !ok {
		return nil, notFound("export handle", wire)
	}
	request := proto.Clone(in).(*pb.HostMsg)
	terminal := false
	switch q := request.Msg.(type) {
	case *pb.HostMsg_ExportChunk:
		q.ExportChunk.Handle = b.local
		// The received descriptor identifies the arrival-time deposit claim.
		// Clone the envelope/handle, but retain that identity for Chunk's copy.
		if ref := in.GetExportChunk().GetSegment(); ref != nil {
			q.ExportChunk.Source = &pb.ExportChunk_Segment{Segment: ref}
		}
	case *pb.HostMsg_FinishExport:
		q.FinishExport.Handle = b.local
		terminal = true
	case *pb.HostMsg_AbortExport:
		q.AbortExport.Handle = b.local
		terminal = true
	}
	// A terminal callback may release its resources before returning an error.
	if terminal {
		delete(r.handles, wire)
	}
	out, err := (Handlers{Export: b.handlers}).handle(ctx, c, request)
	if err != nil {
		return nil, err
	}
	out = proto.Clone(out).(*pb.PluginMsg)
	if q := out.GetExportDone(); q != nil {
		q.Handle = wire
	}
	if q := out.GetChunkWritten(); q != nil {
		q.Handle = wire
	}
	return out, nil
}
func (r *exportRouter) closeAll(ctx context.Context, c *control.Client) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	var result error
	for wire, b := range r.handles {
		delete(r.handles, wire)
		result = errors.Join(result, abortBinding(ctx, c, b))
	}
	return result
}
func abortBinding(ctx context.Context, c *control.Client, b exportBinding) (err error) {
	defer func() {
		if p := recover(); p != nil {
			err = fmt.Errorf("export Abort panic: %v", p)
		}
	}()
	_, err = (Handlers{Export: b.handlers}).handle(ctx, c, &pb.HostMsg{Msg: &pb.HostMsg_AbortExport{AbortExport: &pb.AbortExport{Handle: b.local}}})
	return err
}
