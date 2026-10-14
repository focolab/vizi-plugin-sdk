package control

import (
	"fmt"
	"strings"
	"unicode"
	"unicode/utf8"
)

const helloIdentityPrefix = "Hello from "
const readyIdentityPrefix = "Ready; I am "
const maxIdentityBytes = 2048

// Identity validation checks form only. In particular, whitespace, version
// syntax and agreement with a manifest are not additional receiver rules.
func checkIdentity(id, prefix string) error {
	if len(id) > maxIdentityBytes {
		return fmt.Errorf("identity exceeds %d UTF-8 bytes", maxIdentityBytes)
	}
	if !utf8.ValidString(id) {
		return fmt.Errorf("identity is not valid UTF-8")
	}
	if !strings.HasPrefix(id, prefix) || len(id) == len(prefix) {
		return fmt.Errorf("identity requires %q followed by a non-empty version string", prefix)
	}
	for _, r := range id {
		if unicode.IsControl(r) {
			return fmt.Errorf("identity contains a Unicode Cc control character")
		}
	}
	return nil
}

// Bound untrusted diagnostics by bytes before quoting them; quoting prevents
// control characters in a refused identity from creating extra log lines.
func identityPreview(id string) string {
	if len(id) <= 256 {
		return fmt.Sprintf("%q", id)
	}
	return fmt.Sprintf("%q... (truncated, %d bytes)", id[:256], len(id))
}

func readyIdentity(build string) (string, error) {
	id := readyIdentityPrefix + build
	if err := checkIdentity(id, readyIdentityPrefix); err != nil {
		return "", fmt.Errorf("invalid Config.BuildIdentity %s: %w", identityPreview(build), err)
	}
	return id, nil
}

// HostIdentity returns the complete validated Hello.id, including its prefix.
// It is an immutable observation available to Attach and Connected callbacks.
func (c *Client) HostIdentity() string { return c.peerIdentity }

// PluginIdentity returns this session's complete Ready.id.
func (c *Client) PluginIdentity() string { return c.pluginIdentity }
