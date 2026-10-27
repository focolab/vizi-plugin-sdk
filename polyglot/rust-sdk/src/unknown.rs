//! Field records a message type was not generated with.
//!
//! The region messages (`proto::pois_math::v1`) keep every field record whose
//! number they do not know, in arrival order, and write the records back after
//! their known fields when encoded. A region read from a later 1.x host keeps
//! the fields that host added through an edit and a re-contribution.

use prost::bytes::{Buf, BufMut};
use prost::encoding::{decode_key, decode_varint, encode_key, encode_varint, WireType};
use prost::DecodeError;

/// Nesting limit for unknown groups, matching prost's recursion limit.
const GROUP_DEPTH: u32 = 100;

/// The encoded field records a message did not recognize.
///
/// Each record is kept as tag, wire type and value. Re-encoding writes them
/// after the known fields, in the order they arrived. Varint values (and
/// keys) are rewritten in their shortest form; every other byte is kept as
/// received.
#[derive(Clone, Default, PartialEq, Eq, Hash)]
pub struct UnknownFields {
    raw: Vec<u8>,
}

impl UnknownFields {
    /// The encoded records, ready to append to a message's known fields.
    pub fn as_bytes(&self) -> &[u8] {
        &self.raw
    }

    /// Whether no unknown record was received.
    pub fn is_empty(&self) -> bool {
        self.raw.is_empty()
    }

    /// Encoded length of the records in bytes.
    pub fn len(&self) -> usize {
        self.raw.len()
    }

    /// Forgets every record, so re-encoding writes known fields only.
    pub fn clear(&mut self) {
        self.raw.clear();
    }

    /// Writes the records to `buf`.
    #[doc(hidden)]
    pub fn encode(&self, buf: &mut impl BufMut) {
        buf.put_slice(&self.raw);
    }

    /// Reads one field record whose key was already decoded, and keeps it.
    #[doc(hidden)]
    pub fn merge(
        &mut self,
        tag: u32,
        wire_type: WireType,
        buf: &mut impl Buf,
    ) -> Result<(), DecodeError> {
        if wire_type == WireType::EndGroup {
            return Err(DecodeError::new("unexpected end group tag"));
        }
        let mark = self.raw.len();
        encode_key(tag, wire_type, &mut self.raw);
        let kept = self.value(tag, wire_type, buf, 0);
        if kept.is_err() {
            self.raw.truncate(mark);
        }
        kept
    }

    fn value(
        &mut self,
        tag: u32,
        wire_type: WireType,
        buf: &mut impl Buf,
        depth: u32,
    ) -> Result<(), DecodeError> {
        match wire_type {
            WireType::Varint => encode_varint(decode_varint(buf)?, &mut self.raw),
            WireType::ThirtyTwoBit => self.copy(4, buf)?,
            WireType::SixtyFourBit => self.copy(8, buf)?,
            WireType::LengthDelimited => {
                let len = decode_varint(buf)?;
                let len = usize::try_from(len)
                    .map_err(|_| DecodeError::new("length delimiter exceeds usize"))?;
                encode_varint(len as u64, &mut self.raw);
                self.copy(len, buf)?;
            }
            WireType::StartGroup => {
                if depth >= GROUP_DEPTH {
                    return Err(DecodeError::new("recursion limit reached"));
                }
                loop {
                    let (inner, inner_type) = decode_key(buf)?;
                    encode_key(inner, inner_type, &mut self.raw);
                    if inner_type == WireType::EndGroup {
                        if inner != tag {
                            return Err(DecodeError::new("unexpected end group tag"));
                        }
                        break;
                    }
                    self.value(inner, inner_type, buf, depth + 1)?;
                }
            }
            WireType::EndGroup => return Err(DecodeError::new("unexpected end group tag")),
        }
        Ok(())
    }

    fn copy(&mut self, len: usize, buf: &mut impl Buf) -> Result<(), DecodeError> {
        if buf.remaining() < len {
            return Err(DecodeError::new("buffer underflow"));
        }
        let mut left = len;
        while left > 0 {
            let chunk = buf.chunk();
            let n = chunk.len().min(left);
            self.raw.extend_from_slice(&chunk[..n]);
            buf.advance(n);
            left -= n;
        }
        Ok(())
    }
}

impl std::fmt::Debug for UnknownFields {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "UnknownFields({} bytes)", self.raw.len())
    }
}
