//! Channel names retain the source's wire number, including future values.
use crate::proto;

/// Shared by Geometry, ViewState and OpenExport, with no conversion needed.
pub type ChannelName = proto::ChannelInfo;

impl proto::ChannelInfo {
    /// Only a file's or person's name may be written as an artifact's name.
    pub fn declared(&self) -> bool {
        matches!(
            proto::ChannelNameSource::try_from(self.source),
            Ok(proto::ChannelNameSource::File | proto::ChannelNameSource::User)
        )
    }

    /// Known source labels, or the original decimal number for a future source.
    pub fn source_name(&self) -> String {
        match proto::ChannelNameSource::try_from(self.source) {
            Ok(proto::ChannelNameSource::Unspecified) => "unspecified".into(),
            Ok(proto::ChannelNameSource::File) => "file".into(),
            Ok(proto::ChannelNameSource::Placeholder) => "placeholder".into(),
            Ok(proto::ChannelNameSource::User) => "user".into(),
            Err(_) => self.source.to_string(),
        }
    }
}
