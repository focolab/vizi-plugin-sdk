"""Version gating: the session's negotiated protocol and the features it has.

``control_plane_protocol.md`` §3.1: **``engines.vizi`` is the floor, ``Ready``
the negotiated ceiling.**  The plugin's ``Ready`` carries the lower of the
host's ``Hello`` minor and this SDK's own, and the session runs at that minor.
A minor only adds, so a session at minor ``m`` has every feature introduced at
or below ``m``; :meth:`HostClient.supports <.server.HostClient.supports>`
answers from :data:`FEATURES`, and
:meth:`HostClient.require <.server.HostClient.require>` raises
:class:`FeatureUnavailable` at the call site that asked for more.

Every SDK uses the same feature names and the same sentences, so a plugin's
log reads the same whatever language it was written in.  Stdlib only.
"""

from __future__ import annotations

from .doorbell import PROTOCOL_MAJOR, PROTOCOL_MINOR

__all__ = [
    "FEATURES",
    "FOLLOW",
    "FeatureUnavailable",
    "PROCESSING_STAGES",
    "RAW_READ",
    "REGIONS_SUBSCRIPTION",
    "REGION_SHAPERS",
    "SET_VIEW",
    "VIEW_SUBSCRIPTION",
    "engines_refusal",
    "feature_minor",
    "ready_minor",
    "unavailable",
]

#: The "view" subscription and its ``ViewChanged`` push.
VIEW_SUBSCRIPTION = "view_subscription"
#: The "regions" subscription and its ``RegionsChanged`` push.
REGIONS_SUBSCRIPTION = "regions_subscription"
#: ``ReadDataVolume.raw``: the file's own voxels, not a filter's output.
RAW_READ = "raw_read"
#: ``PlanStage`` and ``StageArm``: the plugin as a stage in the host's chain.
PROCESSING_STAGES = "processing_stages"
#: A region type the plugin shapes: ``ShapeDrag``, ``ShapeCommit``, ``ShapeEditEnded``.
REGION_SHAPERS = "region_shapers"
#: ``SetView``: the plugin moves the view when the person asked it to.
SET_VIEW = "set_view"
#: ``OfferFollow``, ``FollowStarted``, ``FollowState`` and ``FollowEnded``.
FOLLOW = "follow"

#: Each feature and the protocol minor (of :data:`PROTOCOL_MAJOR`) that
#: introduced it.  A feature a later minor adds is a new row here, and
#: nothing else: the gate is the table.
FEATURES: "dict[str, int]" = {
    VIEW_SUBSCRIPTION: 0,
    REGIONS_SUBSCRIPTION: 0,
    RAW_READ: 0,
    PROCESSING_STAGES: 0,
    REGION_SHAPERS: 0,
    SET_VIEW: 0,
    FOLLOW: 0,
}


def feature_minor(feature: str) -> "int | None":
    """The protocol minor that introduced ``feature``, or ``None`` for a name
    this SDK does not know."""
    return FEATURES.get(feature)


def ready_minor(host_minor: int) -> int:
    """The minor this SDK announces in ``Ready`` to a host whose ``Hello``
    says ``host_minor``: the lower of the two, because a ``Ready`` above the
    host's minor names messages the host has never heard of."""
    return min(int(host_minor), PROTOCOL_MINOR)


def engines_refusal(engines_minor: int, ready: int) -> "str | None":
    """The handshake's refusal of a ``Ready`` below the plugin's own
    ``engines.vizi``, or ``None``.  The manifest says the plugin needs at
    least ``engines_minor``; a session below it would run code its author
    never meant to run against that host."""
    if engines_minor > ready:
        return (
            f"this plugin's manifest requires protocol {PROTOCOL_MAJOR}.{engines_minor} "
            f"but its code speaks {PROTOCOL_MAJOR}.{ready}"
        )
    return None


def _message(feature: str, needs: "int | None", session_minor: "int | None") -> str:
    if needs is None:
        return f'feature "{feature}" is not one this SDK knows'
    if session_minor is None:
        return (
            f'feature "{feature}" needs protocol {PROTOCOL_MAJOR}.{needs}; '
            "the session has no handshake yet"
        )
    return (
        f'feature "{feature}" needs protocol {PROTOCOL_MAJOR}.{needs}; '
        f"this session speaks {PROTOCOL_MAJOR}.{session_minor}"
    )


class FeatureUnavailable(RuntimeError):
    """A plugin asked for a feature its session does not have.

    ``needs`` is the minor that introduced it (``None``: not a name this SDK
    knows); ``session_minor`` is the session's negotiated minor (``None``:
    no handshake yet).  The message names both, so the call site that asked
    is the one the log points at."""

    def __init__(self, feature: str, needs: "int | None", session_minor: "int | None") -> None:
        self.feature = feature
        self.needs = needs
        self.session_minor = session_minor
        super().__init__(_message(feature, needs, session_minor))


def unavailable(feature: str, session: "tuple[int, int] | None") -> "FeatureUnavailable | None":
    """Why a session at ``session`` (``(major, minor)``, ``None`` before the
    handshake) may not use ``feature``, or ``None`` when it may."""
    needs = feature_minor(feature)
    session_minor = None if session is None else int(session[1])
    if needs is not None and session_minor is not None and needs <= session_minor:
        return None
    return FeatureUnavailable(feature, needs, session_minor)
