package plugin

import (
	"errors"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/manifest"
)

func TestHostProtocolFromSession(t *testing.T) {
	if _, _, ok := HostProtocol(nil); ok {
		t.Fatal("no session reported a version")
	}
	c, _ := listTestSession(t, control.Config{PluginID: "test.plugin", BuildIdentity: "test.plugin test build"})
	major, minor, ok := HostProtocol(c)
	if !ok || major != 1 || minor != 0 {
		t.Fatal(major, minor, ok)
	}
	if major, minor, ok := SessionProtocol(c); !ok || major != 1 || minor != 0 {
		t.Fatal(major, minor, ok)
	}
	if !Supports(c, control.FeatureSetView) || Require(c, control.FeatureFollow) != nil || Supports(c, "telepathy") {
		t.Fatal("feature table disagrees with a 1.0 session")
	}
}

func TestDefinitionCarriesEnginesToTheHandshake(t *testing.T) {
	a := buildDefinition(t, NewDefinition(Info{ID: "org.example.older", Name: "Older", Version: "1", Engines: "1.0"}))
	cfg, err := a.Configure(control.Plugin{})
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Control.Engines != "1.0" {
		t.Fatalf("Engines %q", cfg.Control.Engines)
	}
	// Above this SDK's minor the manifest is refused engines-first, at Build,
	// before any handshake; control's handshake check is the backstop.
	var incompatible *manifest.EnginesIncompatibleError
	if _, err := NewDefinition(Info{ID: "org.example.newer", Name: "Newer", Version: "1", Engines: "1.2"}).Build(); !errors.As(err, &incompatible) {
		t.Fatalf("engines 1.2 built: %v", err)
	}
	if def := buildDefinition(t, NewDefinition(Info{ID: "org.example.plain", Name: "Plain", Version: "1"})); def.m.Engines.Vizi != "1.0" {
		t.Fatalf("default engines %q", def.m.Engines.Vizi)
	}
}
