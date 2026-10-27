package plugin

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

// SeriesEntry describes one image. ID is opaque and may be empty. A nil shape
// and DTYPE_UNSPECIFIED omit information that is expensive to discover.
type SeriesEntry struct {
	ID, Label   string
	ShapeTCZYX  []uint32
	DType       pb.DType
	Description string
}

// ReaderOpenResult contains exactly one of Opened or Choice. After Opened,
// ReaderHandlers owns its local handle and Close releases it as usual.
type ReaderOpenResult struct {
	Opened *pb.ReaderOpened
	Choice *SeriesChoice
}

type ReaderOpenHandler func(context.Context, *control.Client, *pb.OpenReader) (ReaderOpenResult, error)

// SeriesChoice offers images without replacing an open reader. Open optionally
// continues on the instance that produced the listing; otherwise OpenSeries is
// called again. Close releases any listing-only resources if it is abandoned,
// refused or expires. With Open, a successful Opened transfers ownership to the
// normal reader Close callback. Without Open, the listing is closed when the
// fresh OpenSeries returns its own handle (or before replacing the listing). Both callbacks may
// capture an instance. A fresh OpenSeries must also handle a named series.
type SeriesChoice struct {
	Entries []SeriesEntry
	Open    ReaderOpenHandler
	Close   func() error
}

func seriesList(choice *SeriesChoice) (*pb.SeriesList, error) {
	if len(choice.Entries) == 0 {
		return nil, errors.New("a SeriesChoice lists no series; a file with no image is refused")
	}
	seen := make(map[string]bool, len(choice.Entries))
	for _, e := range choice.Entries {
		if seen[e.ID] {
			return nil, fmt.Errorf("series id '%s' is listed twice", e.ID)
		}
		seen[e.ID] = true
	}
	out := &pb.SeriesList{}
	used := 0
	for _, e := range choice.Entries {
		if e.ShapeTCZYX != nil {
			if len(e.ShapeTCZYX) != 5 || slices.Contains(e.ShapeTCZYX, uint32(0)) {
				return nil, fmt.Errorf("series '%s': shape_tczyx must be five positive sizes", e.ID)
			}
		}
		switch e.DType {
		case pb.DType_DTYPE_UNSPECIFIED, pb.DType_DTYPE_U8, pb.DType_DTYPE_U16, pb.DType_DTYPE_F32:
		default:
			return nil, fmt.Errorf("series '%s': dtype %s is not one of ['f32', 'u16', 'u8']", e.ID, e.DType)
		}
		for _, s := range []string{e.ID, e.Label, e.Description} {
			if !utf8.ValidString(s) || len(s) > control.MaxInlineBytes {
				return nil, errors.New("series text must be UTF-8 and at most 8 MiB per field")
			}
		}
		wire := &pb.SeriesEntry{Id: e.ID, Label: e.Label, ShapeTczyx: slices.Clone(e.ShapeTCZYX), Dtype: e.DType, Description: e.Description}
		size := proto.Size(wire)
		cost := 1 + protowire.SizeVarint(uint64(size)) + size
		if used+cost > control.MaxMessageBytes-64 || len(out.Entries) == control.MaxSeries {
			out.Truncated = true
			break
		}
		out.Entries = append(out.Entries, wire)
		used += cost
	}
	return out, nil
}

const seriesHoldTime = 10 * time.Minute

type pendingSeries struct {
	uri, readerID string
	choice        *SeriesChoice
	timer         *time.Timer
	stopSession   func() bool
}

type seriesState struct {
	mu      sync.Mutex
	pending *pendingSeries
}

func closeChoice(c *control.Client, choice *SeriesChoice) {
	if choice == nil || choice.Close == nil {
		return
	}
	var err error
	func() {
		defer func() {
			if p := recover(); p != nil {
				err = fmt.Errorf("panic: %v", p)
			}
		}()
		err = choice.Close()
	}()
	if err != nil && c != nil {
		c.Diagnose(fmt.Errorf("closing a listing reader failed: %w", err))
	}
}

func (s *seriesState) take(c *control.Client, q *pb.OpenReader) *SeriesChoice {
	s.mu.Lock()
	defer s.mu.Unlock()
	p := s.pending
	s.pending = nil
	if p == nil {
		return nil
	}
	p.timer.Stop()
	if p.stopSession != nil {
		p.stopSession()
	}
	if q != nil && q.Series != nil && p.uri == q.Uri && p.readerID == q.ReaderId {
		return p.choice
	}
	closeChoice(c, p.choice)
	return nil
}

func (s *seriesState) expire(c *control.Client, p *pendingSeries) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.pending != p {
		return
	}
	s.pending = nil
	p.timer.Stop()
	if p.stopSession != nil {
		p.stopSession()
	}
	closeChoice(c, p.choice)
}

func (s *seriesState) hold(c *control.Client, q *pb.OpenReader, choice *SeriesChoice) {
	s.mu.Lock()
	defer s.mu.Unlock()
	// Retain only the continuation and closer, not a potentially huge listing.
	p := &pendingSeries{uri: q.Uri, readerID: q.ReaderId, choice: &SeriesChoice{Open: choice.Open, Close: choice.Close}}
	s.pending = p
	p.timer = time.AfterFunc(seriesHoldTime, func() { s.expire(c, p) })
	if c != nil {
		p.stopSession = context.AfterFunc(c.Context(), func() { s.expire(c, p) })
	}
}

// beforeOpen runs after disposing an unrelated listing, before accessing the
// file. The router uses it for H2's same-URI release.
func (s *seriesState) open(ctx context.Context, c *control.Client, h ReaderHandlers, q *pb.OpenReader, beforeOpen func()) (_ *pb.PluginMsg, err error) {
	held := s.take(c, q)
	if beforeOpen != nil {
		beforeOpen()
	}
	if h.OpenSeries == nil {
		out, err := invoke(ctx, c, q, h.Open, "open_reader", func(r *pb.ReaderOpened) *pb.PluginMsg {
			return &pb.PluginMsg{Msg: &pb.PluginMsg_ReaderOpened{ReaderOpened: r}}
		})
		if err == nil {
			err = validateReaderNames(ctx, c, h, out.GetReaderOpened())
		}
		if err != nil {
			return nil, err
		}
		return out, nil
	}
	opener := h.OpenSeries
	if held != nil && held.Open != nil {
		opener = held.Open
	}
	cleanup := held
	defer func() {
		if p := recover(); p != nil {
			err = fmt.Errorf("reader OpenSeries panic: %v", p)
		}
		if err != nil {
			closeChoice(c, cleanup)
		}
	}()
	request := proto.Clone(q).(*pb.OpenReader)
	for {
		selected, selectedID := request.Series != nil, request.GetSeries()
		result, openErr := opener(ctx, c, request)
		if cleanup != nil && cleanup.Open == nil && result.Choice != cleanup && (result.Opened != nil || result.Choice != nil) {
			closeChoice(c, cleanup)
			cleanup = nil
		}
		if result.Choice != nil && (cleanup == nil || result.Choice.Close != nil) {
			cleanup = result.Choice
		}
		if openErr != nil {
			return nil, openErr
		}
		if (result.Opened == nil) == (result.Choice == nil) {
			return nil, &control.HostError{Kind: pb.Error_INVALID_ARGUMENT, Message: "open failed: return exactly one of Opened or Choice"}
		}
		if result.Opened != nil {
			// A returned handle belongs to Close now, including on validation
			// failure; do not also run the listing-only closer.
			cleanup = nil
			if err := validateReaderNames(ctx, c, h, result.Opened); err != nil {
				return nil, err
			}
			return &pb.PluginMsg{Msg: &pb.PluginMsg_ReaderOpened{ReaderOpened: result.Opened}}, nil
		}
		if selected {
			return nil, &control.HostError{Kind: pb.Error_INVALID_ARGUMENT, Message: fmt.Sprintf("open failed: asked for series '%s', the reader answered with a list", selectedID)}
		}
		listed, listErr := seriesList(result.Choice)
		if listErr != nil {
			return nil, &control.HostError{Kind: pb.Error_INVALID_ARGUMENT, Message: "open failed: the reader's SeriesChoice is not valid: " + listErr.Error()}
		}
		if len(result.Choice.Entries) == 1 {
			request = proto.Clone(q).(*pb.OpenReader)
			request.Series = proto.String(result.Choice.Entries[0].ID)
			if result.Choice.Open != nil {
				opener = result.Choice.Open
			}
			continue
		}
		s.hold(c, q, result.Choice)
		return &pb.PluginMsg{Msg: &pb.PluginMsg_SeriesList{SeriesList: listed}}, nil
	}
}

func validateReaderNames(ctx context.Context, c *control.Client, h ReaderHandlers, opened *pb.ReaderOpened) error {
	err := readerNamesError(opened)
	if err == nil {
		return nil
	}
	if closeErr := closeReaderBinding(ctx, c, readerBinding{handlers: h, local: opened.Handle}); closeErr != nil && c != nil {
		c.Diagnose(fmt.Errorf("closing an invalid reader failed: %w", closeErr))
	}
	return err
}

func readerNamesError(opened *pb.ReaderOpened) error {
	if len(opened.GetChannelNames()) == 0 {
		return nil
	}
	var channels uint32
	if len(opened.ShapeTczyx) > 1 {
		channels = opened.ShapeTczyx[1]
	}
	if uint64(len(opened.ChannelNames)) <= uint64(channels) {
		return nil
	}
	return &control.HostError{Kind: pb.Error_INVALID_ARGUMENT, Message: fmt.Sprintf("open failed: the reader's ReaderInfo is not valid: channel_names names %d channels of %d", len(opened.ChannelNames), channels)}
}
