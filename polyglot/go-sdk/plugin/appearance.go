package plugin

import (
	"context"
	"errors"
	"fmt"
	"image/color"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

// AppearanceColors names an Appearance's colors in the order the message
// numbers them; Hex and Color take these names.
var AppearanceColors = []string{"window_bg", "panel_bg", "text", "text_disabled", "accent", "border"}

// Appearance is the host's theme and the colors it draws panels with
// (control_plane_protocol.md §8.4): what a plugin that renders its own
// pictures, a plot pushed to an image widget, needs to match them. Each color
// is non-premultiplied (r, g, b, a), 0-255, and is a color.Color, so it draws
// directly with image/draw.
type Appearance struct {
	Theme        string      // "dark" or "light"
	WindowBg     color.NRGBA // behind a panel
	PanelBg      color.NRGBA // inside a field, a list or a table
	Text         color.NRGBA
	TextDisabled color.NRGBA
	Accent       color.NRGBA // a selection's highlight
	Border       color.NRGBA // a frame or a grid line
}

// Dark reports whether the theme is "dark".
func (a Appearance) Dark() bool { return a.Theme == "dark" }

// Color is one color by its AppearanceColors name, false for any other name.
func (a Appearance) Color(name string) (color.NRGBA, bool) {
	switch name {
	case "window_bg":
		return a.WindowBg, true
	case "panel_bg":
		return a.PanelBg, true
	case "text":
		return a.Text, true
	case "text_disabled":
		return a.TextDisabled, true
	case "accent":
		return a.Accent, true
	case "border":
		return a.Border, true
	}
	return color.NRGBA{}, false
}

// Hex is one color by its AppearanceColors name as "#rrggbb", alpha dropped,
// for example Hex("text"); "" for any other name.
func (a Appearance) Hex(name string) string {
	c, ok := a.Color(name)
	if !ok {
		return ""
	}
	return HexColor(c)
}

// HexColors is every color as "#rrggbb", by name.
func (a Appearance) HexColors() map[string]string {
	out := make(map[string]string, len(AppearanceColors))
	for _, name := range AppearanceColors {
		out[name] = a.Hex(name)
	}
	return out
}

// HexColor writes a color as "#rrggbb", alpha dropped.
func HexColor(c color.NRGBA) string { return fmt.Sprintf("#%02x%02x%02x", c.R, c.G, c.B) }

func rgba(v uint32) color.NRGBA {
	return color.NRGBA{R: uint8(v >> 24), G: uint8(v >> 16), B: uint8(v >> 8), A: uint8(v)}
}

// AppearanceOf reads a HostAppearance, whose colors are 0xRRGGBBAA.
func AppearanceOf(m *pb.HostAppearance) Appearance {
	return Appearance{
		Theme:        m.GetTheme(),
		WindowBg:     rgba(m.GetWindowBg()),
		PanelBg:      rgba(m.GetPanelBg()),
		Text:         rgba(m.GetText()),
		TextDisabled: rgba(m.GetTextDisabled()),
		Accent:       rgba(m.GetAccent()),
		Border:       rgba(m.GetBorder()),
	}
}

// CurrentAppearance is the host's theme and colors as last heard, false
// before the first push (or from a host that sends none). A PanelOpened
// handler can read it without registering OnAppearance: the host sends it
// once after Ready, before any panel opens.
func CurrentAppearance(c *control.Client) (Appearance, bool) {
	if c == nil {
		return Appearance{}, false
	}
	m, ok := c.Appearance()
	if !ok {
		return Appearance{}, false
	}
	return AppearanceOf(m), true
}

// AppearanceHandler hears the host's theme and colors: once after the
// session starts, before any panel opens, and again whenever the person
// switches theme. It runs on its own worker and may call the host; several
// changes in a row may reach it as the newest alone.
type AppearanceHandler func(context.Context, *control.Client, Appearance) error

// OnAppearance registers the definition's one appearance handler. No
// subscription is needed: every host that knows the push sends it.
func (d *Definition) OnAppearance(handler AppearanceHandler) {
	d.open()
	if handler == nil {
		d.fail(errors.New("appearance requires a handler"))
		return
	}
	if d.h.Appearance != nil {
		d.fail(errors.New("duplicate appearance handler"))
		return
	}
	d.h.Appearance = handler
}

// appearanceHook is the control hook calling the typed handler.
func appearanceHook(f AppearanceHandler) func(context.Context, *control.Client, *pb.HostAppearance) error {
	return func(ctx context.Context, c *control.Client, m *pb.HostAppearance) error {
		return f(ctx, c, AppearanceOf(m))
	}
}
