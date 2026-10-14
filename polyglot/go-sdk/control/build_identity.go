package control

import (
	"fmt"
	"runtime/debug"
	"strconv"
	"strings"
)

const sdkModulePath = "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk"

// SDKVersion identifies this SDK's source release, aligned with the Python SDK.
// Embedded module/VCS provenance separately identifies the actual build.
const SDKVersion = "1.0a85"

// buildIdentity reads only metadata embedded in this executable, never the
// current checkout, working directory or a mutable manifest beside the binary.
func buildIdentity(cfg Config) (identity, details string, err error) {
	info, ok := debug.ReadBuildInfo()
	if !ok {
		info = nil
	} else {
		details = info.String()
	}
	identity, err = composeBuildIdentity(cfg.PluginID, cfg.BuildIdentity, info)
	return
}

func moduleIdentity(m debug.Module) string {
	version := m.Version
	if version == "" {
		version = "unknown"
	}
	out := strconv.Quote(m.Path) + "@" + strconv.Quote(version)
	if m.Replace != nil {
		if m.Replace.Version == "" {
			// The requested version does not describe the replacement's bytes.
			return out + " => local " + strconv.Quote(m.Replace.Path) + " (revision unknown; source snapshot required)"
		}
		return out + " => " + strconv.Quote(m.Replace.Path) + "@" + strconv.Quote(m.Replace.Version)
	}
	if m.Version == "" || m.Version == "(devel)" {
		out += " (unversioned source)"
	}
	return out
}

func composeBuildIdentity(pluginID, label string, info *debug.BuildInfo) (string, error) {
	if label == "" {
		label = pluginID
	}
	// Validate the author-controlled portion before adding quoted metadata.
	if _, err := readyIdentity(label); err != nil {
		return "", err
	}
	parts := []string{label}
	if info == nil {
		return readyIdentity(label + "; build metadata unavailable; vizi-go-sdk " + SDKVersion + "; sdk-module=version unknown")
	}
	settings := make(map[string]string, len(info.Settings))
	for _, setting := range info.Settings {
		settings[setting.Key] = setting.Value
	}
	if revision := settings["vcs.revision"]; revision != "" {
		modified := settings["vcs.modified"]
		if modified != "true" && modified != "false" {
			modified = "unknown"
		}
		vcs := "main-vcs=" + strconv.Quote(settings["vcs"]+":"+revision) + " modified=" + modified
		if modified != "false" {
			vcs += " (not an exact source snapshot)"
		}
		parts = append(parts, vcs)
	} else {
		parts = append(parts, "main-vcs=unavailable")
	}
	parts = append(parts, "main="+moduleIdentity(info.Main), "package="+strconv.Quote(info.Path))
	sdk := "version unknown"
	if info.Main.Path == sdkModulePath {
		sdk = moduleIdentity(info.Main) + " (main module)"
	}
	localDeps := 0
	for _, dep := range info.Deps {
		if dep == nil {
			continue
		}
		if dep.Path == sdkModulePath {
			sdk = moduleIdentity(*dep)
		}
		if (dep.Replace != nil && dep.Replace.Version == "") || (dep.Replace == nil && (dep.Version == "" || dep.Version == "(devel)")) {
			localDeps++
		}
	}
	parts = append(parts, "vizi-go-sdk "+SDKVersion, "sdk-module="+sdk)
	if localDeps > 0 {
		parts = append(parts, fmt.Sprintf("unversioned-dependencies=%d (source snapshots required)", localDeps))
	}
	parts = append(parts, "toolchain="+strconv.Quote(info.GoVersion))
	for _, key := range []string{"GOOS", "GOARCH", "CGO_ENABLED"} {
		if value, ok := settings[key]; ok {
			parts = append(parts, key+"="+strconv.Quote(value))
		}
	}
	// Refuse overflow instead of truncating a revision's dirty qualifier or
	// replacement provenance. Full metadata stays available through BuildInfo.
	return readyIdentity(strings.Join(parts, "; "))
}

// BuildInfo returns Go's embedded module versions, sums, replacements and build
// settings, as shown by `go version -m <binary>`. Empty means unavailable.
// It describes the build, not the runtime OS, libraries, dataset or environment.
func (c *Client) BuildInfo() string { return c.buildInfo }
