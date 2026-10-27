//! The Windows session token on the control stream.
//!
//! A stream proves it is the host before it can become the session. A `1.0`
//! host sends the token in `Hello.session_token` and may also send it in call
//! metadata ([`SESSION_TOKEN_METADATA_KEY`]). When the metadata carries it, it
//! is checked before the stream is accepted; without the key, the stream is
//! accepted when `Hello.session_token` matches, which every `1.0` host
//! sends. A stream with a missing or wrong token is refused
//! `UNAUTHENTICATED` and never ends the session: the plugin keeps accepting
//! streams until one authenticates.

/// The gRPC call-metadata key carrying the Windows session token.
pub const SESSION_TOKEN_METADATA_KEY: &str = "vizi-session-token";

/// Equality whose running time does not depend on where the inputs differ.
pub(crate) fn tokens_equal(a: &[u8], b: &[u8]) -> bool {
    let mut diff = a.len() ^ b.len();
    for i in 0..a.len().max(b.len()) {
        let x = a.get(i).copied().unwrap_or(0);
        let y = b.get(i).copied().unwrap_or(0);
        diff |= usize::from(x ^ y);
    }
    std::hint::black_box(diff) == 0
}

/// What the call metadata says about a stream.
pub(crate) enum MetadataAuth {
    /// No token is required, or the metadata carries the right one.
    Authenticated,
    /// The metadata carries no token; `Hello.session_token` decides.
    Absent,
}

pub(crate) fn unauthenticated() -> tonic::Status {
    tonic::Status::unauthenticated("session token missing or mismatched")
}

/// Checks the metadata before the stream is accepted.
pub(crate) fn check_metadata(
    expected: Option<&str>,
    metadata: &tonic::metadata::MetadataMap,
) -> Result<MetadataAuth, Box<tonic::Status>> {
    let Some(expected) = expected else {
        return Ok(MetadataAuth::Authenticated);
    };
    let mut values = metadata.get_all(SESSION_TOKEN_METADATA_KEY).iter();
    let Some(first) = values.next() else {
        return Ok(MetadataAuth::Absent);
    };
    if values.next().is_some() || !tokens_equal(first.as_bytes(), expected.as_bytes()) {
        return Err(Box::new(unauthenticated()));
    }
    Ok(MetadataAuth::Authenticated)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn equality_covers_length_and_content() {
        assert!(tokens_equal(b"abc", b"abc"));
        assert!(!tokens_equal(b"abc", b"abd"));
        assert!(!tokens_equal(b"abc", b"ab"));
        assert!(!tokens_equal(b"", b"a"));
        assert!(tokens_equal(b"", b""));
    }
}
