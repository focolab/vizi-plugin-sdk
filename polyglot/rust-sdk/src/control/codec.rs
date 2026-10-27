//! The gRPC codec for the Session stream: prost encoding and decoding, plus a
//! digest of each received message's wire bytes.
//!
//! Prost discards fields it does not know, so the decoded message cannot say
//! whether two messages under one id were byte-identical. The decoder records
//! a digest of the bytes it decoded in a thread-local, and the dispatcher
//! takes it with [`take_digest`] immediately after the stream yields the
//! message: decoding happens inside the dispatcher's own poll of the stream,
//! on its thread, with no await point in between.

use std::cell::Cell;
use std::hash::{Hash, Hasher};
use std::marker::PhantomData;

use prost::bytes::Buf;
use prost::Message;
use tonic::codec::{BufferSettings, Codec, DecodeBuf, Decoder, ProstCodec};
use tonic::Status;

/// A 128-bit digest of a message's wire bytes and their length.
pub(crate) type Digest = (u64, u64, usize);

thread_local! {
    static LAST: Cell<Option<Digest>> = const { Cell::new(None) };
}

fn digest(bytes: &[u8]) -> Digest {
    let half = |salt: u8| {
        let mut h = std::collections::hash_map::DefaultHasher::new();
        salt.hash(&mut h);
        bytes.hash(&mut h);
        h.finish()
    };
    (half(0), half(1), bytes.len())
}

/// The digest of the message this thread decoded last, taken once.
pub(crate) fn take_digest() -> Option<Digest> {
    LAST.with(Cell::take)
}

/// The digest of a message's prost encoding: the fallback when no wire
/// digest was recorded. It covers the known fields only.
pub(crate) fn digest_of(m: &impl Message) -> Digest {
    digest(&m.encode_to_vec())
}

#[derive(Debug, Clone)]
pub struct DigestCodec<T, U> {
    prost: ProstCodec<T, U>,
}

impl<T, U> Default for DigestCodec<T, U> {
    fn default() -> Self {
        Self {
            prost: ProstCodec::default(),
        }
    }
}

impl<T, U> Codec for DigestCodec<T, U>
where
    T: Message + Send + 'static,
    U: Message + Default + Send + 'static,
{
    type Encode = T;
    type Decode = U;
    type Encoder = <ProstCodec<T, U> as Codec>::Encoder;
    type Decoder = DigestDecoder<U>;

    fn encoder(&mut self) -> Self::Encoder {
        self.prost.encoder()
    }

    fn decoder(&mut self) -> Self::Decoder {
        DigestDecoder {
            buffer_settings: self.prost.decoder().buffer_settings(),
            _pd: PhantomData,
        }
    }
}

#[derive(Debug)]
pub struct DigestDecoder<U> {
    buffer_settings: BufferSettings,
    _pd: PhantomData<U>,
}

impl<U: Message + Default> Decoder for DigestDecoder<U> {
    type Item = U;
    type Error = Status;

    fn decode(&mut self, buf: &mut DecodeBuf<'_>) -> Result<Option<U>, Status> {
        let bytes = buf.copy_to_bytes(buf.remaining());
        let item = U::decode(bytes.as_ref()).map_err(|e| Status::internal(e.to_string()))?;
        LAST.with(|last| last.set(Some(digest(&bytes))));
        Ok(Some(item))
    }

    fn buffer_settings(&self) -> BufferSettings {
        self.buffer_settings
    }
}
