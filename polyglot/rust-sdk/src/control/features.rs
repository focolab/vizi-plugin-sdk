//! Version gating: the session's negotiated protocol and the features it has.
//!
//! A plugin's `Ready` carries the lower of the host's `Hello` minor and this
//! SDK's own, and the session runs at that minor. Minors only add, so a
//! session at minor `m` has every feature introduced at or below `m`;
//! [`Context::supports`](super::Context::supports) answers from [`FEATURES`].
//! Every SDK uses the same feature names.

/// The protocol this SDK speaks.
pub const PROTOCOL_MAJOR: u32 = 1;
pub const PROTOCOL_MINOR: u32 = 0;

/// The "view" subscription and its `ViewChanged` push.
pub const VIEW_SUBSCRIPTION: &str = "view_subscription";
/// The "regions" subscription and its `RegionsChanged` push.
pub const REGIONS_SUBSCRIPTION: &str = "regions_subscription";
/// `ReadDataVolume.raw`: the file's own voxels, not a filter's output.
pub const RAW_READ: &str = "raw_read";
/// `PlanStage` and `StageArm`: the plugin as a stage in the host's chain.
pub const PROCESSING_STAGES: &str = "processing_stages";
/// A region type the plugin shapes: `ShapeDrag`, `ShapeCommit`, `ShapeEditEnded`.
pub const REGION_SHAPERS: &str = "region_shapers";
/// `SetView`: the plugin moves the view when the person asked it to.
pub const SET_VIEW: &str = "set_view";
/// `OfferFollow`, `FollowStarted`, `FollowState` and `FollowEnded`.
pub const FOLLOW: &str = "follow";

/// Each feature and the protocol minor (of [`PROTOCOL_MAJOR`]) that introduced it.
pub const FEATURES: &[(&str, u32)] = &[
    (VIEW_SUBSCRIPTION, 0),
    (REGIONS_SUBSCRIPTION, 0),
    (RAW_READ, 0),
    (PROCESSING_STAGES, 0),
    (REGION_SHAPERS, 0),
    (SET_VIEW, 0),
    (FOLLOW, 0),
];

/// The protocol minor that introduced `feature`, or `None` for a name this
/// SDK does not know.
pub fn feature_minor(feature: &str) -> Option<u32> {
    FEATURES
        .iter()
        .find(|(name, _)| *name == feature)
        .map(|(_, minor)| *minor)
}

/// The minor this SDK announces in `Ready` to a host whose `Hello` says
/// `host_minor`: the lower of the two.
// At SDK minor 0 the result is always 0; the `min` is what a later minor needs.
#[allow(clippy::unnecessary_min_or_max)]
pub fn ready_minor(host_minor: u32) -> u32 {
    host_minor.min(PROTOCOL_MINOR)
}

/// The handshake's refusal of a `Ready` below the plugin's own `engines.vizi`.
pub(crate) fn engines_refusal(engines_minor: u32, ready_minor: u32) -> Option<String> {
    (engines_minor > ready_minor).then(|| {
        format!(
            "this plugin's manifest requires protocol {PROTOCOL_MAJOR}.{engines_minor} but its code speaks {PROTOCOL_MAJOR}.{ready_minor}"
        )
    })
}

/// Why [`Context::require`](super::Context::require) refused `feature` for a
/// session at `session` (`None` before the handshake).
pub(crate) fn unavailable(feature: &str, session: Option<(u32, u32)>) -> Option<String> {
    refusal(feature, feature_minor(feature), session)
}
fn refusal(feature: &str, needs: Option<u32>, session: Option<(u32, u32)>) -> Option<String> {
    let Some(needs) = needs else {
        return Some(format!("feature \"{feature}\" is not one this SDK knows"));
    };
    match session {
        None => Some(format!(
            "feature \"{feature}\" needs protocol {PROTOCOL_MAJOR}.{needs}; the session has no handshake yet"
        )),
        Some((_, minor)) if needs > minor => Some(format!(
            "feature \"{feature}\" needs protocol {PROTOCOL_MAJOR}.{needs}; this session speaks {PROTOCOL_MAJOR}.{minor}"
        )),
        Some(_) => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ready_is_the_lower_minor() {
        for host in [0, 1, 99] {
            assert_eq!(ready_minor(host), 0);
        }
    }

    #[test]
    fn a_newer_feature_names_both_minors() {
        assert_eq!(unavailable(SET_VIEW, Some((1, 0))), None);
        assert_eq!(
            refusal("test_future", Some(1), Some((1, 0))).as_deref(),
            Some("feature \"test_future\" needs protocol 1.1; this session speaks 1.0")
        );
        assert_eq!(
            unavailable(FOLLOW, None).as_deref(),
            Some("feature \"follow\" needs protocol 1.0; the session has no handshake yet")
        );
        assert_eq!(
            unavailable("telepathy", Some((1, 0))).as_deref(),
            Some("feature \"telepathy\" is not one this SDK knows")
        );
        assert_eq!(
            engines_refusal(2, 0).as_deref(),
            Some("this plugin's manifest requires protocol 1.2 but its code speaks 1.0")
        );
        assert_eq!(engines_refusal(0, 0), None);
    }
}
