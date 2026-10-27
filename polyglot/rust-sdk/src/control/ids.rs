//! Ids are never reused. The receiver records the id of every host message
//! that deserves a reply (a request, a GrantStateQuery, and an unknown kind
//! carrying an id) and, in a separate table, of every reply it receives: a
//! reply carries the id of this end's own request, not one the host chose.
//! A second message under a recorded id is a byte-identical re-send, dropped
//! without a second dispatch, or a protocol error that faults the session.
//! The tables hold the last [`ID_WINDOW`] ids received; an older id is not
//! checked.

use std::collections::{HashMap, VecDeque};

use super::codec::{self, Digest};
use super::{reply_kind, runtime::is_request};
use crate::proto::host_msg::Msg as Host;
use crate::proto::HostMsg;

/// How many received ids a session remembers: the host's own bound.
pub(crate) const ID_WINDOW: usize = 65_536;

#[derive(Default)]
pub(crate) struct Ledger {
    requests: HashMap<String, Digest>,
    replies: HashMap<String, Digest>,
    window: VecDeque<(bool, String)>,
}

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum Seen {
    New,
    Resent,
    Reused,
}

impl Ledger {
    /// Classify `message`; `wire` is the digest of its bytes as received.
    pub(crate) fn observe(&mut self, message: &HostMsg, wire: Option<Digest>) -> Seen {
        if message.id.is_empty() {
            return Seen::New;
        }
        let replies = match &message.msg {
            None | Some(Host::GrantStateQuery(_)) => false,
            Some(m) if is_request(m) => false,
            Some(m) if reply_kind(m).is_some() || matches!(m, Host::HostError(_)) => true,
            Some(_) => return Seen::New,
        };
        let digest = wire.unwrap_or_else(|| codec::digest_of(message));
        let table = if replies {
            &mut self.replies
        } else {
            &mut self.requests
        };
        match table.get(&message.id) {
            Some(seen) if *seen == digest => return Seen::Resent,
            Some(_) => return Seen::Reused,
            None => {}
        }
        table.insert(message.id.clone(), digest);
        self.window.push_back((replies, message.id.clone()));
        if self.window.len() > ID_WINDOW {
            if let Some((old_replies, old)) = self.window.pop_front() {
                if old_replies {
                    self.replies.remove(&old);
                } else {
                    self.requests.remove(&old);
                }
            }
        }
        Seen::New
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::proto::GrantStateQuery;

    fn query(id: String, segment_id: u64) -> HostMsg {
        HostMsg {
            id,
            msg: Some(Host::GrantStateQuery(GrantStateQuery { segment_id })),
        }
    }

    #[test]
    fn window_slides_over_the_last_65536_ids() {
        let mut ledger = Ledger::default();
        for i in 0..=ID_WINDOW {
            assert_eq!(ledger.observe(&query(format!("r{i}"), 1), None), Seen::New);
        }
        assert_eq!(ledger.requests.len(), ID_WINDOW);
        // An id older than the window is not checked; one inside it is.
        assert_eq!(ledger.observe(&query("r0".into(), 2), None), Seen::New);
        assert_eq!(ledger.observe(&query("r2".into(), 1), None), Seen::Resent);
        assert_eq!(ledger.observe(&query("r3".into(), 2), None), Seen::Reused);
    }
}
