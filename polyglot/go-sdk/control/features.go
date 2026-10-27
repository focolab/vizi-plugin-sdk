package control

import (
	"fmt"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/manifest"
)

// Feature names, one per protocol addition a plugin may check for with
// Client.Supports. Every SDK uses the same names, and FeatureMinor says which
// protocol minor introduced each.
const (
	// FeatureViewSubscription is the "view" subscription and its ViewChanged push.
	FeatureViewSubscription = "view_subscription"
	// FeatureRegionsSubscription is the "regions" subscription and its RegionsChanged push.
	FeatureRegionsSubscription = "regions_subscription"
	// FeatureRawRead is ReadDataVolume.raw: the file's own voxels, not a filter's output.
	FeatureRawRead = "raw_read"
	// FeatureProcessingStages is PlanStage and StageArm: the plugin as a stage in the host's chain.
	FeatureProcessingStages = "processing_stages"
	// FeatureRegionShapers is a region type the plugin shapes: ShapeDrag, ShapeCommit, ShapeEditEnded.
	FeatureRegionShapers = "region_shapers"
	// FeatureSetView is SetView: the plugin moves the view when the person asked it to.
	FeatureSetView = "set_view"
	// FeatureFollow is OfferFollow, FollowStarted, FollowState and FollowEnded.
	FeatureFollow = "follow"
)

// features maps each feature name to the protocol minor (of major
// ProtocolMajor) that introduced it. Minors only add, so a session at minor m
// has every feature whose minor is at most m.
var features = map[string]uint32{
	FeatureViewSubscription:    0,
	FeatureRegionsSubscription: 0,
	FeatureRawRead:             0,
	FeatureProcessingStages:    0,
	FeatureRegionShapers:       0,
	FeatureSetView:             0,
	FeatureFollow:              0,
}

// FeatureMinor returns the protocol minor that introduced feature, and false
// for a name this SDK does not know.
func FeatureMinor(feature string) (minor uint32, ok bool) {
	minor, ok = features[feature]
	return minor, ok
}

// ReadyMinor is the minor this SDK announces in Ready to a host whose Hello
// says hostMinor: the lower of the host's and this SDK's own. The session runs
// at that minor, never above what the host speaks.
func ReadyMinor(hostMinor uint32) uint32 {
	return min(hostMinor, uint32(ProtocolMinor))
}

// checkEngines refuses a Ready below the plugin's own engines.vizi: a plugin
// whose manifest asks for more than its code speaks fails at the handshake,
// naming both.
func checkEngines(engines string, readyMinor uint32) error {
	if engines == "" {
		return nil
	}
	_, minor, err := manifest.EnginesMinimum(engines)
	if err != nil {
		return err
	}
	if minor > readyMinor {
		return fmt.Errorf("this plugin's manifest requires protocol %d.%d but its code speaks %d.%d", ProtocolMajor, minor, ProtocolMajor, readyMinor)
	}
	return nil
}

// FeatureUnavailableError is Client.Require's refusal: the session's
// negotiated minor is below the one the feature needs, the session has no
// handshake yet, or the name is not one this SDK knows.
type FeatureUnavailableError struct {
	Feature      string
	Needs        uint32 // the minor that introduced it; zero when !Known
	SessionMinor uint32
	Known        bool
	Handshaken   bool
}

func (e *FeatureUnavailableError) Error() string {
	switch {
	case !e.Known:
		return fmt.Sprintf("feature %q is not one this SDK knows", e.Feature)
	case !e.Handshaken:
		return fmt.Sprintf("feature %q needs protocol %d.%d; the session has no handshake yet", e.Feature, ProtocolMajor, e.Needs)
	default:
		return fmt.Sprintf("feature %q needs protocol %d.%d; this session speaks %d.%d", e.Feature, ProtocolMajor, e.Needs, ProtocolMajor, e.SessionMinor)
	}
}

// SessionProtocol returns the session's negotiated protocol: the version this
// plugin's Ready announced, the host's Hello minor clamped to this SDK's. ok
// is false before the handshake. Gate a feature on it, through Supports,
// rather than on HostProtocol.
func (c *Client) SessionProtocol() (major, minor uint32, ok bool) {
	if c == nil || c.peerHello == nil {
		return 0, 0, false
	}
	return ProtocolMajor, c.readyMinor, true
}

// Supports reports whether this session may use feature: the name is one this
// SDK knows and the negotiated minor includes it. False before the handshake.
// It is safe to call concurrently.
func (c *Client) Supports(feature string) bool {
	return c.Require(feature) == nil
}

// Require is Supports as a call-site guard: nil when the session may use
// feature, else a *FeatureUnavailableError naming the feature, the minor it
// needs and the session's minor. Call it before using a newer feature and fall
// back on the error.
func (c *Client) Require(feature string) error {
	needs, known := FeatureMinor(feature)
	_, minor, ok := c.SessionProtocol()
	if known && ok && needs <= minor {
		return nil
	}
	return &FeatureUnavailableError{Feature: feature, Needs: needs, SessionMinor: minor, Known: known, Handshaken: ok}
}
