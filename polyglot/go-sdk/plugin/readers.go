package plugin

import (
	"context"
	"errors"
	"fmt"
	"math"
	"slices"
	"sync"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
)

type readerBinding struct {
	handlers ReaderHandlers
	uri      string
	local    string
	shape    []uint32
	dtype    pb.DType
}
type readerRouter struct {
	series  seriesState
	mu      sync.Mutex
	readers map[string]ReaderHandlers
	handles map[string]readerBinding
	next    uint64
}

// handle serializes reader operations, as the control request worker does. Each
// contribution owns its local handle namespace; wire handles never collide.
func (r *readerRouter) handle(ctx context.Context, c *control.Client, in *pb.HostMsg) (*pb.PluginMsg, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if q := in.GetOpenReader(); q != nil {
		// Every other open disposes the listing, including invalid routing.
		// The selected contribution below still receives its normalized ID.
		if q.ReaderId != "" {
			if _, ok := r.readers[q.ReaderId]; !ok {
				r.series.take(c, nil)
				return nil, notFound("reader", q.ReaderId)
			}
		}
		id := q.ReaderId
		if id == "" {
			if len(r.readers) != 1 {
				r.series.take(c, nil)
				return nil, &control.HostError{Kind: pb.Error_INVALID_ARGUMENT, Message: "reader_id is required with multiple reader contributions"}
			}
			for key := range r.readers {
				id = key
			}
		}
		handlers, ok := r.readers[id]
		if !ok {
			return nil, notFound("reader", id)
		}
		if r.next == math.MaxUint64 {
			return nil, errors.New("reader handle capacity exhausted")
		}
		// A reload must release exclusive access first. Different URIs keep the
		// previous reader intact until the new Open has succeeded (§1.2 H2).
		beforeOpen := func() {
			for _, b := range r.handles {
				if b.uri == q.Uri {
					r.replaceClose(ctx, c)
					break
				}
			}
		}
		// Keep the exact wire reader_id as the listing key, while callbacks see
		// the selected contribution even for the single-reader empty-ID fallback.
		opener := handlers
		if handlers.OpenSeries != nil {
			opener.OpenSeries = readerIDOpener(id, handlers.OpenSeries)
		} else {
			opener.Open = func(ctx context.Context, c *control.Client, request *pb.OpenReader) (*pb.ReaderOpened, error) {
				request = proto.Clone(request).(*pb.OpenReader)
				request.ReaderId = id
				return handlers.Open(ctx, c, request)
			}
		}
		out, err := r.series.open(ctx, c, opener, q, beforeOpen)
		if err != nil {
			return nil, err
		}
		if out.GetSeriesList() != nil {
			return out, nil
		}
		opened := out.GetReaderOpened()
		if opened.GetHandle() == "" {
			return nil, errors.Join(errors.New("reader Open returned empty handle"),
				closeReaderBinding(ctx, c, readerBinding{handlers: handlers, local: ""}))
		}
		out = proto.Clone(out).(*pb.PluginMsg)
		opened = out.GetReaderOpened()
		r.replaceClose(ctx, c)
		r.next++
		wire := fmt.Sprintf("go-reader-%d", r.next)
		r.handles[wire] = readerBinding{handlers: handlers, uri: q.Uri, local: opened.Handle, shape: slices.Clone(opened.ShapeTczyx), dtype: opened.Dtype}
		out.GetReaderOpened().Handle = wire
		return out, nil
	}
	var wire string
	switch q := in.Msg.(type) {
	case *pb.HostMsg_ReadVolume:
		wire = q.ReadVolume.GetHandle()
	case *pb.HostMsg_ReadPlane:
		wire = q.ReadPlane.GetHandle()
	case *pb.HostMsg_Metadata:
		wire = q.Metadata.GetHandle()
	case *pb.HostMsg_CloseReader:
		wire = q.CloseReader.GetHandle()
	default:
		return nil, unsupported("reader request")
	}
	b, ok := r.handles[wire]
	if !ok {
		if in.GetCloseReader() != nil {
			return &pb.PluginMsg{Msg: &pb.PluginMsg_Closed{Closed: &pb.Closed{Handle: wire}}}, nil
		}
		err := notFound("reader handle", wire)
		if c != nil {
			c.Diagnose(fmt.Errorf("host out of step: request names an inactive or replaced reader handle: %w", err))
		}
		return nil, err
	}
	if len(b.shape) == 5 {
		bad := false
		if q := in.GetReadVolume(); q != nil {
			bad = q.T >= b.shape[0] || q.C >= b.shape[1]
		}
		if q := in.GetReadPlane(); q != nil {
			bad = q.T >= b.shape[0] || q.C >= b.shape[1] || q.Z >= b.shape[2]
		}
		if bad {
			return nil, &control.HostError{Kind: pb.Error_OUT_OF_BOUNDS, Message: "reader indices outside shape_tczyx"}
		}
	}
	if in.GetCloseReader() != nil {
		delete(r.handles, wire)
		if err := closeReaderBinding(ctx, c, b); err != nil {
			return nil, err
		}
		return &pb.PluginMsg{Msg: &pb.PluginMsg_Closed{Closed: &pb.Closed{Handle: wire}}}, nil
	}
	request := proto.Clone(in).(*pb.HostMsg)
	body := request.ProtoReflect().Get(request.ProtoReflect().WhichOneof(request.ProtoReflect().Descriptor().Oneofs().ByName("msg"))).Message()
	body.Set(body.Descriptor().Fields().ByName("handle"), protoreflect.ValueOfString(b.local))
	checked := checkedReaderReplies(b.handlers, func(string) *pb.ReaderOpened {
		return &pb.ReaderOpened{Handle: b.local, ShapeTczyx: b.shape, Dtype: b.dtype}
	})
	return (Handlers{Reader: checked}).handle(ctx, c, request)
}

func readerIDOpener(id string, f ReaderOpenHandler) ReaderOpenHandler {
	return func(ctx context.Context, c *control.Client, q *pb.OpenReader) (ReaderOpenResult, error) {
		q = proto.Clone(q).(*pb.OpenReader)
		q.ReaderId = id
		result, err := f(ctx, c, q)
		if result.Choice != nil && result.Choice.Open != nil {
			choice := *result.Choice
			choice.Open = readerIDOpener(id, choice.Open)
			result.Choice = &choice
		}
		return result, err
	}
}

func (r *readerRouter) closeAll(ctx context.Context, c *control.Client) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.series.take(c, nil)
	return r.closeAllLocked(ctx, c)
}

// A replacement close must never turn a successful Open into an Error that
// falsely promises the old reader is unchanged. Cleanup errors are diagnosed.
func (r *readerRouter) replaceClose(ctx context.Context, c *control.Client) {
	if err := r.closeAllLocked(ctx, c); err != nil && c != nil {
		c.Diagnose(fmt.Errorf("replaced reader close failed: %w", err))
	}
}

// Forget before closing: a callback may release its file and then fail, so
// neither explicit close nor a same-URI reload may revive or retry that binding.
func (r *readerRouter) closeAllLocked(ctx context.Context, c *control.Client) error {
	var result error
	for wire, b := range r.handles {
		delete(r.handles, wire)
		result = errors.Join(result, closeReaderBinding(ctx, c, b))
	}
	return result
}

func closeReaderBinding(ctx context.Context, c *control.Client, b readerBinding) (err error) {
	if b.handlers.Close == nil {
		return nil
	}
	defer func() {
		if p := recover(); p != nil {
			err = fmt.Errorf("reader Close panic: %v", p)
		}
	}()
	_, err = invoke(ctx, c, &pb.CloseReader{Handle: b.local}, b.handlers.Close, "close_reader", func(q *pb.Closed) *pb.PluginMsg {
		return &pb.PluginMsg{Msg: &pb.PluginMsg_Closed{Closed: q}}
	})
	return err
}
