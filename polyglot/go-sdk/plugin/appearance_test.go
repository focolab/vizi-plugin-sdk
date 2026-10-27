package plugin

import (
	"context"
	"image/color"
	"maps"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

var darkAppearance = &pb.HostAppearance{Theme: "dark", WindowBg: 0x1e1e1eff, PanelBg: 0x2b2b2bff, Text: 0xe6e6e6ff,
	TextDisabled: 0x80808080, Accent: 0x2a82daff, Border: 0x00000000}

func TestAppearanceOf(t *testing.T) {
	a := AppearanceOf(darkAppearance)
	if a.Theme != "dark" || !a.Dark() {
		t.Fatal(a)
	}
	if a.WindowBg != (color.NRGBA{0x1e, 0x1e, 0x1e, 0xff}) || a.TextDisabled != (color.NRGBA{0x80, 0x80, 0x80, 0x80}) || a.Border != (color.NRGBA{}) {
		t.Fatal("colors are 0xRRGGBBAA:", a)
	}
	want := map[string]string{"window_bg": "#1e1e1e", "panel_bg": "#2b2b2b", "text": "#e6e6e6",
		"text_disabled": "#808080", "accent": "#2a82da", "border": "#000000"}
	if got := a.HexColors(); !maps.Equal(got, want) {
		t.Fatal(got)
	}
	if a.Hex("accent") != "#2a82da" || a.Hex("nope") != "" {
		t.Fatal(a.Hex("accent"), a.Hex("nope"))
	}
	if _, ok := a.Color("nope"); ok {
		t.Fatal("an unknown color name")
	}
	if len(AppearanceColors) != 6 {
		t.Fatal(AppearanceColors)
	}
	if AppearanceOf(&pb.HostAppearance{Theme: "light"}).Dark() {
		t.Fatal("light reads as dark")
	}
	if _, ok := CurrentAppearance(nil); ok {
		t.Fatal("an appearance without a session")
	}
}

func TestAppearanceHandler(t *testing.T) {
	d := definition()
	heard := make(chan Appearance, 1)
	d.OnAppearance(func(_ context.Context, _ *control.Client, a Appearance) error {
		heard <- a
		return nil
	})
	cfg, err := buildDefinition(t, d).Configure(control.Plugin{})
	if err != nil {
		t.Fatal(err)
	}
	if err := cfg.Control.Appearance(context.Background(), nil, darkAppearance); err != nil {
		t.Fatal(err)
	}
	if got := <-heard; got != AppearanceOf(darkAppearance) {
		t.Fatal(got)
	}

	twice := definition()
	twice.OnAppearance(func(context.Context, *control.Client, Appearance) error { return nil })
	twice.OnAppearance(func(context.Context, *control.Client, Appearance) error { return nil })
	if _, err := twice.Build(); err == nil {
		t.Fatal("accepted two appearance handlers")
	}
	none := definition()
	none.OnAppearance(nil)
	if _, err := none.Build(); err == nil {
		t.Fatal("accepted a nil handler")
	}
	h := Handlers{Appearance: func(context.Context, *control.Client, Appearance) error { return nil }}
	if _, err := h.Configure(control.Config{Appearance: func(context.Context, *control.Client, *pb.HostAppearance) error { return nil }}); err == nil {
		t.Fatal("replaced an existing appearance hook")
	}
}
