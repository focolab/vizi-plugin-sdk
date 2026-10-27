package vizi.plugin.javasdk.control;

/**
 * {@code Host.require}'s refusal: the session's negotiated protocol minor is below the one the
 * feature needs, the session has no handshake yet, or the name is not one this SDK knows. The
 * message names the feature, the minor it needs and the session's.
 */
public final class FeatureUnavailableException extends IllegalStateException {
  private static final long serialVersionUID = 1L;

  private final String feature;

  public FeatureUnavailableException(String feature, String message) {
    super(message);
    this.feature = feature;
  }

  /** The feature that was asked for. */
  public String feature() {
    return feature;
  }
}
