package vizi.plugin.javasdk;

import java.util.List;
import java.util.Objects;
import vizi.plugin.v1.ViziPlugin.ChannelInfo;
import vizi.plugin.v1.ViziPlugin.ChannelNameSource;

/**
 * A channel's name and whose it is (control_plane_protocol.md §8.11), as {@code
 * Geometry.channels}, {@code ViewState.channels} and {@code OpenExport.channels} carry it. The name
 * is never empty: a channel nothing named carries the label the person sees ({@code "Channel N"})
 * with {@code source = PLACEHOLDER}. Label a trace with any name; write one into an artifact only
 * when it is {@link #declared}.
 *
 * @param source {@code UNRECOGNIZED} for a value this SDK does not know, whose number is {@link
 *     #sourceNumber}
 */
public record ChannelName(int index, String name, ChannelNameSource source, int sourceNumber) {

  public ChannelName {
    Objects.requireNonNull(name, "name");
    Objects.requireNonNull(source, "source");
  }

  public static ChannelName of(ChannelInfo c) {
    return new ChannelName(c.getIndex(), c.getName(), c.getSource(), c.getSourceValue());
  }

  public static List<ChannelName> of(List<ChannelInfo> cs) {
    return cs.stream().map(ChannelName::of).toList();
  }

  /** A name the file (or its reader) or the person gave: {@code FILE} or {@code USER}. */
  public boolean declared() {
    return source == ChannelNameSource.CHANNEL_NAME_SOURCE_FILE || source == ChannelNameSource.CHANNEL_NAME_SOURCE_USER;
  }

  /** The host's label for a channel nothing named. */
  public boolean placeholder() {
    return source == ChannelNameSource.CHANNEL_NAME_SOURCE_PLACEHOLDER;
  }

  /**
   * {@code "file"}, {@code "placeholder"}, {@code "user"}, {@code "unspecified"}, or a source this
   * SDK does not know as its number ({@code "7"}): kept, never dropped.
   */
  public String sourceLabel() {
    return switch (source) {
      case CHANNEL_NAME_SOURCE_FILE -> "file";
      case CHANNEL_NAME_SOURCE_PLACEHOLDER -> "placeholder";
      case CHANNEL_NAME_SOURCE_USER -> "user";
      case CHANNEL_NAME_SOURCE_UNSPECIFIED -> "unspecified";
      case UNRECOGNIZED -> Integer.toString(sourceNumber);
    };
  }
}
