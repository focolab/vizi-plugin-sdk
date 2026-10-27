package plugin

import "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"

// HostProtocol returns the protocol major and minor the host's Hello announced
// for the session c, which every handler receives. ok is false before the
// handshake. A plugin may use a message added in a later minor only when the
// host's minor says it exists; a plugin that uses only protocol 1.0 messages
// need not check.
func HostProtocol(c *control.Client) (major, minor uint32, ok bool) {
	return c.HostProtocol()
}

// SessionProtocol returns the session's negotiated protocol, the version this
// plugin's Ready announced: the host's Hello minor clamped to this SDK's. ok is
// false before the handshake.
func SessionProtocol(c *control.Client) (major, minor uint32, ok bool) {
	return c.SessionProtocol()
}

// Supports reports whether the session c may use feature (a control.Feature*
// name): the negotiated minor includes the one that introduced it.
func Supports(c *control.Client, feature string) bool {
	return c.Supports(feature)
}

// Require returns nil when the session c may use feature, else a
// *control.FeatureUnavailableError naming the feature, the minor it needs and
// the session's minor.
func Require(c *control.Client, feature string) error {
	return c.Require(feature)
}
