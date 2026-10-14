package control

import (
	"runtime/debug"
	"strings"
	"testing"
)

func testBuildInfo(modified string) *debug.BuildInfo {
	return &debug.BuildInfo{
		GoVersion: "go1.25.0", Path: "example.org/tool/cmd/plugin",
		Main:     debug.Module{Path: "example.org/tool", Version: "(devel)"},
		Deps:     []*debug.Module{{Path: sdkModulePath, Version: "v1.2.3", Sum: "h1:module-sum"}},
		Settings: []debug.BuildSetting{{Key: "vcs", Value: "git"}, {Key: "vcs.revision", Value: "1598cf91abcdef01234567890123456789012345678"}, {Key: "vcs.modified", Value: modified}, {Key: "GOOS", Value: "linux"}, {Key: "GOARCH", Value: "amd64"}},
	}
}

func TestBuildIdentityQualifiesRevision(t *testing.T) {
	for _, modified := range []string{"false", "true", "", "unexpected"} {
		info := testBuildInfo(modified)
		got, err := composeBuildIdentity("example.plugin", "", info)
		if err != nil {
			t.Fatal(err)
		}
		wantModified := modified
		if modified == "" || modified == "unexpected" {
			wantModified = "unknown"
		}
		for _, want := range []string{"Ready; I am example.plugin;", "main-vcs=", "1598cf91abcdef01234567890123456789012345678", "modified=" + wantModified, "example.org/tool", "vizi-go-sdk 1.0a85; sdk-module=", "v1.2.3", "go1.25.0", "linux", "amd64"} {
			if !strings.Contains(got, want) {
				t.Fatalf("missing provenance %q: %s", want, got)
			}
		}
		if strings.Contains(got, "not an exact source snapshot") != (modified != "false") {
			t.Fatal("revision presented with wrong confidence", got)
		}
		again, _ := composeBuildIdentity("example.plugin", "", info)
		if again != got {
			t.Fatal("identity is not stable for the same build")
		}
	}
}

func TestBuildIdentityMissingMetadataDoesNotGuess(t *testing.T) {
	for _, info := range []*debug.BuildInfo{nil, {Main: debug.Module{Path: "example.org/tool", Version: "v2.0.0"}}} {
		got, err := composeBuildIdentity("plugin", "", info)
		if err != nil || !strings.Contains(got, "unavailable") || !strings.Contains(got, "vizi-go-sdk 1.0a85; sdk-module=version unknown") || strings.Contains(got, "modified=false") {
			t.Fatal("invented build provenance", got, err)
		}
	}
}

func TestBuildIdentityReplacementProvenanceCannotBeOverridden(t *testing.T) {
	for _, version := range []string{"", "v2.0.1"} {
		info := testBuildInfo("false")
		info.Deps[0].Replace = &debug.Module{Path: "example.org/fork", Version: version}
		got, err := composeBuildIdentity("plugin", "author's release label", info)
		if err != nil || !strings.HasPrefix(got, "Ready; I am author's release label;") {
			t.Fatal(got, err)
		}
		for _, want := range []string{"vizi-go-sdk", "v1.2.3", "=>", "example.org/fork", "modified=false"} {
			if !strings.Contains(got, want) {
				t.Fatal("override concealed provenance", got)
			}
		}
		if version == "" {
			if !strings.Contains(got, "revision unknown; source snapshot required") || !strings.Contains(got, "unversioned-dependencies=1") {
				t.Fatal("local replacement presented as original release", got)
			}
		} else if !strings.Contains(got, "v2.0.1") || strings.Contains(got, "unversioned-dependencies") {
			t.Fatal("versioned replacement misidentified", got)
		}
	}
	workspace := testBuildInfo("false")
	workspace.Deps = append(workspace.Deps, &debug.Module{Path: "example.org/workspace", Version: "(devel)"})
	workspaceID, err := composeBuildIdentity("plugin", "", workspace)
	if err != nil || !strings.Contains(workspaceID, "unversioned-dependencies=1 (source snapshots required)") {
		t.Fatal("workspace dependency hidden by clean main tree", workspaceID, err)
	}
	info := testBuildInfo("true")
	info.Main.Path = sdkModulePath
	info.Deps = nil
	got, err := composeBuildIdentity("example", "", info)
	if err != nil || !strings.Contains(got, "(main module)") || !strings.Contains(got, "modified=true") {
		t.Fatal("SDK built as main module misidentified", got, err)
	}
}

func TestBuildIdentityDoesNotTruncateProvenance(t *testing.T) {
	if _, err := composeBuildIdentity("plugin", strings.Repeat("x", 1900), testBuildInfo("true")); err == nil {
		t.Fatal("oversized identity silently lost build provenance")
	}
	info := testBuildInfo("true")
	info.Deps[0].Replace = &debug.Module{Path: "../sdk\nforged"}
	got, err := composeBuildIdentity("plugin", "", info)
	if err != nil || strings.ContainsRune(got, '\n') || !strings.Contains(got, "source snapshot required") {
		t.Fatal("embedded path was not safely quoted", got, err)
	}
}

func TestDefaultIdentityAndBuildInfoReachClient(t *testing.T) {
	// openPeer supplies a test label; metadata still comes from this binary.
	p := openPeer(t, Config{}, nil)
	info, ok := debug.ReadBuildInfo()
	if ok && p.client.BuildInfo() != info.String() {
		t.Fatal("full embedded build metadata lost")
	}
	if !strings.Contains(p.client.PluginIdentity(), "vizi-go-sdk") {
		t.Fatal("SDK implementation omitted")
	}
}
