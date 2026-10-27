package plugin

import (
	"context"
	"sync"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

// Wrap the volume callback itself so the volume-only plane fallback checks the
// complete source geometry, including Z, before slicing it into a valid plane.
func checkedReaderReplies(h ReaderHandlers, geometry func(string) *pb.ReaderOpened) ReaderHandlers {
	volume, plane := h.Volume, h.Plane
	if volume != nil {
		h.Volume = func(ctx context.Context, c *control.Client, q *pb.ReadVolume) (*pb.SegmentRef, error) {
			opened := geometry(q.Handle)
			d, err := volume(ctx, c, q)
			if err == nil {
				err = c.ValidateReaderReply(d, opened, false)
			}
			if err != nil {
				return nil, err
			}
			return d, nil
		}
	}
	if plane != nil {
		h.Plane = func(ctx context.Context, c *control.Client, q *pb.ReadPlane) (*pb.SegmentRef, error) {
			opened := geometry(q.Handle)
			d, err := plane(ctx, c, q)
			if err == nil {
				err = c.ValidateReaderReply(d, opened, true)
			}
			if err != nil {
				return nil, err
			}
			return d, nil
		}
	}
	return h
}

// The singular Reader API keeps application-owned handles. Remember its open
// geometry without changing those handles or taking over its lifecycle.
func trackReaderGeometry(h ReaderHandlers) ReaderHandlers {
	var mu sync.Mutex
	var opened *pb.ReaderOpened
	open, close := h.Open, h.Close
	openSeries := h.OpenSeries
	if openSeries != nil {
		var track func(ReaderOpenHandler) ReaderOpenHandler
		track = func(f ReaderOpenHandler) ReaderOpenHandler {
			return func(ctx context.Context, c *control.Client, q *pb.OpenReader) (ReaderOpenResult, error) {
				out, err := f(ctx, c, q)
				if err == nil && out.Opened != nil && readerNamesError(out.Opened) == nil {
					mu.Lock()
					opened = proto.Clone(out.Opened).(*pb.ReaderOpened)
					mu.Unlock()
				}
				if out.Choice != nil && out.Choice.Open != nil {
					choice := *out.Choice
					choice.Open = track(choice.Open)
					out.Choice = &choice
				}
				return out, err
			}
		}
		h.OpenSeries = track(openSeries)
	}
	if open != nil {
		h.Open = func(ctx context.Context, c *control.Client, q *pb.OpenReader) (*pb.ReaderOpened, error) {
			out, err := open(ctx, c, q)
			if err == nil && out != nil && readerNamesError(out) == nil {
				mu.Lock()
				opened = proto.Clone(out).(*pb.ReaderOpened)
				mu.Unlock()
			}
			return out, err
		}
	}
	if close != nil {
		h.Close = func(ctx context.Context, c *control.Client, q *pb.CloseReader) (*pb.Closed, error) {
			mu.Lock()
			if opened.GetHandle() == q.Handle {
				opened = nil
			}
			mu.Unlock()
			return close(ctx, c, q)
		}
	}
	return checkedReaderReplies(h, func(handle string) *pb.ReaderOpened {
		mu.Lock()
		defer mu.Unlock()
		if opened.GetHandle() != handle {
			return nil
		}
		return opened
	})
}
