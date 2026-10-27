package control

import (
	"errors"
	"fmt"
	"slices"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

const HeldImageBytes = 16 * 1024 * 1024

// PanelImageKey identifies a widget cell. List is the declaring list ID for a
// row image, when known; empty means a raw handle without declaration metadata.
type PanelImageKey struct{ Panel, Widget, Row, List string }
type heldPanelImage struct {
	list    string
	session *Session
	segment uint64
	data    []byte
}

// PublishPanelImage keeps an owned copy of the latest image when its panel has
// no installed P grant. Installation flushes these; PanelOpened also retries.
// Callers validate the encoding first. No shared-memory loan is held while closed.
func (s *Session) PublishPanelImage(key PanelImageKey, segment uint64, data []byte) error {
	if s == nil || s.Client == nil || s.Memory == nil || s.Memory.Registry == nil || key.Panel == "" || key.Widget == "" {
		return errors.New("image requires a session, panel and widget")
	}
	c := s.Client
	c.panelMu.Lock()
	defer c.panelMu.Unlock()
	if err := c.Context().Err(); err != nil {
		return err
	}
	err := s.publishPanelImage(key, segment, data)
	if errors.Is(err, memory.ErrNotInstalled) || errors.Is(err, memory.ErrRetired) {
		if c.heldImages == nil {
			c.heldImages = make(map[PanelImageKey]heldPanelImage)
		}
		list := key.List
		key.List = ""
		if list == "" {
			list = c.cfg.Panels[key.Panel].Cells[key.Widget]
			if list == "" {
				list = c.heldImages[key].list
			}
		}
		total := len(data)
		for other, held := range c.heldImages {
			if other.Panel == key.Panel && other != key {
				total += len(held.data)
			}
		}
		if total > HeldImageBytes {
			return fmt.Errorf("panel %q held images exceed %d bytes", key.Panel, HeldImageBytes)
		}
		c.heldImages[key] = heldPanelImage{list, s, 0, slices.Clone(data)}
		return nil
	}
	if err == nil {
		key.List = ""
		delete(c.heldImages, key)
	}
	return err
}

func (s *Session) publishPanelImage(key PanelImageKey, segment uint64, data []byte) error {
	if segment == 0 {
		seg, err := s.Memory.Registry.FindContribution(memory.PurposePanel, key.Panel)
		if err != nil {
			return err
		}
		segment = seg.ID()
	}
	return s.publishLoan(segment, memory.PurposePanel, data, func(d *pb.SegmentRef) (*pb.PluginMsg, error) {
		return &pb.PluginMsg{Msg: &pb.PluginMsg_PanelPatch{PanelPatch: &pb.PanelPatch{PanelId: key.Panel, Ops: []*pb.PatchOp{{WidgetId: key.Widget, RowKey: key.Row, Op: &pb.PatchOp_SetImage{SetImage: &pb.ImageRef{SegmentId: d.SegmentId, Offset: d.Offset, Len: d.Len}}}}}}}, nil
	}, false, s.Client.send)
}

func (c *Client) flushPanelImages(panel string) error {
	c.panelMu.Lock()
	defer c.panelMu.Unlock()
	var errs []error
	for key, held := range c.heldImages {
		if key.Panel != panel {
			continue
		}
		err := held.session.publishPanelImage(key, held.segment, held.data)
		if errors.Is(err, memory.ErrNotInstalled) || errors.Is(err, memory.ErrRetired) {
			continue
		}
		delete(c.heldImages, key)
		if err != nil {
			errs = append(errs, err)
		}
	}
	return errors.Join(errs...)
}

// Called only after successful admission, while panelMu serializes image pushes
// and opens with row removals. A refused patch cannot discard held state.
func (c *Client) prunePanelImages(patch *pb.PanelPatch) {
	for _, op := range patch.Ops {
		if op.GetSetImage() != nil {
			delete(c.heldImages, PanelImageKey{Panel: patch.PanelId, Widget: op.WidgetId, Row: op.RowKey})
		}
		if rows := op.GetSetRows(); rows != nil {
			for key, held := range c.heldImages {
				if key.Panel == patch.PanelId && key.Row != "" && held.list == op.WidgetId && !slices.Contains(rows.Keys, key.Row) {
					delete(c.heldImages, key)
				}
			}
		}
	}
}
