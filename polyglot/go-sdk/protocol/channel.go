package protocol

// ChannelName is a channel's source index, display name and name source. It is
// the same type carried by Geometry, ViewState and OpenExport. Unknown Source
// values retain their numeric value, and Source.String() prints that number.
type ChannelName = ChannelInfo

// Declared reports whether the file or the user named this channel. Other
// sources may label a trace, but must not be written as an artifact's name.
func (c *ChannelInfo) Declared() bool {
	return c.GetSource() == ChannelNameSource_CHANNEL_NAME_SOURCE_FILE ||
		c.GetSource() == ChannelNameSource_CHANNEL_NAME_SOURCE_USER
}
