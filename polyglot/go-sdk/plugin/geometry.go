package plugin

import (
	"context"
	"errors"
	"fmt"
	"math"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

// WidgetSize is how large an image widget is drawn (control_plane_protocol.md
// §8.4): device-independent pixels and the screen's pixel ratio. An image of
// Pixels is shown one image pixel per screen pixel; any other is scaled to fit.
type WidgetSize struct {
	PanelID, WidgetID string
	WidthPx, HeightPx int
	DPR               float64 // 1 when the host sends none
}

// Pixels is the image size drawn 1:1: (round(WidthPx*DPR), round(HeightPx*DPR)),
// rounding halves to even as the Python SDK's round does.
func (s WidgetSize) Pixels() (width, height int) {
	return int(math.RoundToEven(float64(s.WidthPx) * s.DPR)), int(math.RoundToEven(float64(s.HeightPx) * s.DPR))
}

// SizeOf reads a WidgetGeometry as a WidgetSize.
func SizeOf(g *pb.WidgetGeometry) WidgetSize {
	dpr := g.GetDpr()
	if dpr == 0 {
		dpr = 1
	}
	return WidgetSize{PanelID: g.GetPanelId(), WidgetID: g.GetWidgetId(), WidthPx: int(g.GetWidthPx()), HeightPx: int(g.GetHeightPx()), DPR: dpr}
}

// WidgetGeometryHandler hears a panel's image widget sizes: when the panel is
// first shown and whenever a size or the pixel ratio changes. It runs on its
// own worker and may call the host; several changes in a row may reach it as
// the newest alone. A plot redrawn at size.Pixels() and pushed is shown crisp;
// one that is not is scaled to fit. Clicks stay in the pushed image's pixels.
type WidgetGeometryHandler func(context.Context, *control.Client, WidgetSize) error

// OnWidgetGeometry registers this definition's panel's geometry handler, which
// hears every image widget in it (size.WidgetID names which).
func (d *Definition) OnWidgetGeometry(panel PanelRef, handler WidgetGeometryHandler) {
	d.open()
	if panel.owner != d.owner || handler == nil {
		d.fail(errors.New("widget geometry requires this definition's panel and a handler"))
		return
	}
	if d.h.WidgetGeometry == nil {
		d.h.WidgetGeometry = map[string]WidgetGeometryHandler{}
	}
	if d.h.WidgetGeometry[panel.id] != nil {
		d.fail(fmt.Errorf("duplicate widget geometry handler for %q", panel.id))
		return
	}
	d.h.WidgetGeometry[panel.id] = handler
}

// widgetGeometry is the control hook dispatching to the panel's handler.
func widgetGeometry(handlers map[string]WidgetGeometryHandler) func(context.Context, *control.Client, *pb.WidgetGeometry) error {
	return func(ctx context.Context, c *control.Client, g *pb.WidgetGeometry) error {
		if f := handlers[g.GetPanelId()]; f != nil {
			return f(ctx, c, SizeOf(g))
		}
		return nil
	}
}
