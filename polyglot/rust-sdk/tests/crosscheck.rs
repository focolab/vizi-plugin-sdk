#![cfg(unix)]
//! Cross-implementation check against a corpus produced by another SDK.
//!
//! The corpus carries, for every scenario, the lifecycle calls, the record
//! bytes, and one or more implementations' verdicts. This replays the same
//! records here and compares against each of them. Neither implementation is
//! the oracle: a disagreement is a finding against the specification.
//!
//!     VIZI_V2_CORPUS=/path/to/corpus.json cargo test --test crosscheck -- --ignored
//!
//! Setting `VIZI_V2_VERDICTS` additionally writes this SDK's own verdicts, in
//! the corpus's verdict shape, for `scripts/compare_v2_verdicts.py` to fold
//! back in as another column.

use std::{
    collections::BTreeMap,
    fs,
    io::Write,
    os::fd::{AsRawFd, OwnedFd},
    sync::mpsc::{self, Receiver, RecvTimeoutError},
    thread,
    time::{Duration, Instant},
};
use vizi_doorbell::*;

// --- the smallest JSON reader that reads this corpus ------------------------

#[derive(Clone, Debug, PartialEq)]
enum Json {
    Null,
    Bool(bool),
    Num(f64),
    Str(String),
    Arr(Vec<Json>),
    Obj(Vec<(String, Json)>),
}
impl Json {
    fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Obj(fields) => fields.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }
    fn str(&self) -> &str {
        match self {
            Json::Str(s) => s,
            other => panic!("expected a string, got {other:?}"),
        }
    }
    fn u64(&self) -> u64 {
        match self {
            Json::Num(n) => *n as u64,
            other => panic!("expected a number, got {other:?}"),
        }
    }
    fn arr(&self) -> &[Json] {
        match self {
            Json::Arr(items) => items,
            other => panic!("expected an array, got {other:?}"),
        }
    }
    fn truthy(&self) -> bool {
        matches!(self, Json::Bool(true))
    }
}

struct Reader<'a> {
    bytes: &'a [u8],
    at: usize,
}
impl<'a> Reader<'a> {
    fn parse(text: &'a str) -> Json {
        let mut r = Reader {
            bytes: text.as_bytes(),
            at: 0,
        };
        let value = r.value();
        r.space();
        assert_eq!(r.at, r.bytes.len(), "trailing bytes after the JSON value");
        value
    }
    fn space(&mut self) {
        while self.at < self.bytes.len() && self.bytes[self.at].is_ascii_whitespace() {
            self.at += 1;
        }
    }
    fn eat(&mut self, literal: &str) -> bool {
        if self.bytes[self.at..].starts_with(literal.as_bytes()) {
            self.at += literal.len();
            return true;
        }
        false
    }
    fn expect(&mut self, byte: u8) {
        self.space();
        assert_eq!(
            self.bytes.get(self.at),
            Some(&byte),
            "at offset {}",
            self.at
        );
        self.at += 1;
    }
    fn value(&mut self) -> Json {
        self.space();
        match self.bytes[self.at] {
            b'{' => {
                self.at += 1;
                let mut fields = Vec::new();
                self.space();
                if self.eat("}") {
                    return Json::Obj(fields);
                }
                loop {
                    self.space();
                    let key = self.string();
                    self.expect(b':');
                    fields.push((key, self.value()));
                    self.space();
                    if !self.eat(",") {
                        break;
                    }
                }
                self.expect(b'}');
                Json::Obj(fields)
            }
            b'[' => {
                self.at += 1;
                let mut items = Vec::new();
                self.space();
                if self.eat("]") {
                    return Json::Arr(items);
                }
                loop {
                    items.push(self.value());
                    self.space();
                    if !self.eat(",") {
                        break;
                    }
                }
                self.expect(b']');
                Json::Arr(items)
            }
            b'"' => Json::Str(self.string()),
            b't' => {
                assert!(self.eat("true"));
                Json::Bool(true)
            }
            b'f' => {
                assert!(self.eat("false"));
                Json::Bool(false)
            }
            b'n' => {
                assert!(self.eat("null"));
                Json::Null
            }
            _ => {
                let start = self.at;
                while self.at < self.bytes.len()
                    && matches!(
                        self.bytes[self.at],
                        b'0'..=b'9' | b'-' | b'+' | b'.' | b'e' | b'E'
                    )
                {
                    self.at += 1;
                }
                Json::Num(
                    std::str::from_utf8(&self.bytes[start..self.at])
                        .unwrap()
                        .parse()
                        .expect("a number"),
                )
            }
        }
    }
    fn string(&mut self) -> String {
        self.expect(b'"');
        let mut out = String::new();
        loop {
            match self.bytes[self.at] {
                b'"' => {
                    self.at += 1;
                    return out;
                }
                b'\\' => {
                    self.at += 1;
                    let escape = self.bytes[self.at];
                    self.at += 1;
                    match escape {
                        b'n' => out.push('\n'),
                        b't' => out.push('\t'),
                        b'r' => out.push('\r'),
                        b'b' => out.push('\u{8}'),
                        b'f' => out.push('\u{c}'),
                        b'u' => {
                            let hex =
                                std::str::from_utf8(&self.bytes[self.at..self.at + 4]).unwrap();
                            self.at += 4;
                            let point = u32::from_str_radix(hex, 16).expect("a code point");
                            out.push(char::from_u32(point).expect("a scalar value"));
                        }
                        other => out.push(other as char),
                    }
                }
                _ => {
                    let start = self.at;
                    while !matches!(self.bytes[self.at], b'"' | b'\\') {
                        self.at += 1;
                    }
                    out.push_str(std::str::from_utf8(&self.bytes[start..self.at]).unwrap());
                }
            }
        }
    }
}

fn unhex(text: &str) -> Vec<u8> {
    assert!(text.len().is_multiple_of(2), "odd-length hex");
    (0..text.len() / 2)
        .map(|i| u8::from_str_radix(&text[2 * i..2 * i + 2], 16).expect("hex"))
        .collect()
}

// --- the comparable part of an outcome -------------------------------------

/// Only what the documents fix. Diagnostic spellings and cancellation reasons
/// are each implementation's own and are deliberately not compared.
#[derive(Clone, Debug, PartialEq, Eq)]
struct Verdict {
    kind: &'static str,
    disposition: String,
    /// The `where` of a `ContractViolation`; absent when the violation rides
    /// the grant acknowledgement instead.
    site: String,
    grant_failure: bool,
    implicitly_closed: u32,
    interaction: u32,
    class: String,
}
impl Verdict {
    fn blank(kind: &'static str) -> Self {
        Self {
            kind,
            disposition: String::new(),
            site: String::new(),
            grant_failure: false,
            implicitly_closed: 0,
            interaction: 0,
            class: String::new(),
        }
    }
    /// The corpus's own shape, for a verdicts file another implementation reads.
    fn to_json(&self) -> String {
        match self.kind {
            "frame" => format!(
                "{{\"type\": \"frame\", \"disposition\": \"{}\", \"where\": {}, \
                 \"grant_failure\": {}, \"implicitly_closed\": {}}}",
                self.disposition,
                if self.site.is_empty() {
                    "null".to_owned()
                } else {
                    format!("\"{}\"", self.site)
                },
                self.grant_failure,
                self.implicitly_closed
            ),
            "cancellation" => format!(
                "{{\"type\": \"cancellation\", \"interaction\": {}}}",
                self.interaction
            ),
            _ => format!("{{\"type\": \"fault\", \"class\": \"{}\"}}", self.class),
        }
    }
}

/// Folds the one label the specification leaves to each implementation: it
/// names the disposition "unknown opcode" in prose only, and the spelling is
/// not wire visible, so comparing spellings reports a divergence that is not one.
fn canonical(mut v: Verdict) -> Verdict {
    if v.disposition == "unknown_op" {
        v.disposition = "unknown_opcode".to_owned();
    }
    v
}

fn from_json(value: &Json) -> Verdict {
    let kind = value.get("type").expect("a verdict type").str();
    let mut v = Verdict::blank(match kind {
        "frame" => "frame",
        "cancellation" => "cancellation",
        "fault" => "fault",
        other => panic!("unknown verdict type {other:?}"),
    });
    if let Some(d) = value.get("disposition") {
        v.disposition = d.str().to_owned();
    }
    if let Some(Json::Str(w)) = value.get("where") {
        v.site = w.clone();
    }
    v.grant_failure = value.get("grant_failure").is_some_and(Json::truthy);
    if let Some(n) = value.get("implicitly_closed") {
        v.implicitly_closed = n.u64() as u32;
    }
    if let Some(n) = value.get("interaction") {
        v.interaction = n.u64() as u32;
    }
    if let Some(c) = value.get("class") {
        v.class = c.str().to_owned();
    }
    canonical(v)
}

fn violation_site(v: Violation) -> &'static str {
    match v {
        Violation::ZeroId => "zero_id",
        Violation::SeqOnOpen => "seq_on_open",
        Violation::PositionTail => "position_tail",
        Violation::NonfinitePosition => "nonfinite_position",
        Violation::MissWithPicked => "miss_with_picked",
        Violation::InvalidTarget => "invalid_target",
        Violation::InteractionMismatch => "interaction_mismatch",
        Violation::UnknownTool => "unknown_tool",
        Violation::EpochMismatch => "epoch_mismatch",
        // A site this test does not name compares unequal to every fixture.
        _ => "unnamed_site",
    }
}

/// The failure classes the protocol names in §12; the enum spelling is ours.
fn failure_class(c: FailureClass) -> &'static str {
    match c {
        FailureClass::BadCrc => "bad_crc",
        FailureClass::BadLength => "bad_length",
        FailureClass::BadVer => "bad_ver",
        FailureClass::BadFlags => "bad_flags",
        FailureClass::BadReserved => "bad_reserved",
        FailureClass::BadPadding => "bad_padding",
        FailureClass::Ctrunc => "ctrunc",
        FailureClass::WrongFdCount => "wrong_fd_count",
        FailureClass::ReservedOp => "reserved_op",
        FailureClass::ShortWrite => "short_write",
        FailureClass::AmbiguousSend => "ambiguous_send",
        FailureClass::RecvError => "recv_error",
        _ => "unnamed_failure_class",
    }
}

fn normalise(outcome: &Outcome) -> Option<Verdict> {
    match outcome {
        Outcome::Frame(f) => {
            let mut v = Verdict::blank("frame");
            v.disposition = match &f.disposition {
                Disposition::Decoded(record) => {
                    if let DecodedRecord::Input {
                        implicitly_closed: Some(prior),
                        ..
                    } = record
                    {
                        v.implicitly_closed = *prior;
                    }
                    "decoded"
                }
                Disposition::UnknownOpcode(_) => "unknown_opcode",
                Disposition::Ignored(_) => "ignored",
                Disposition::FramingRejected(_) => "framing_rejection",
                Disposition::Rejected(Rejection::Contract(violation)) => {
                    v.site = violation_site(*violation).to_owned();
                    "semantic_rejection"
                }
                Disposition::Rejected(Rejection::Grant { .. }) => {
                    v.grant_failure = true;
                    "semantic_rejection"
                }
                Disposition::Rejected(_) => "semantic_rejection",
                _ => "unnamed_disposition",
            }
            .to_owned();
            Some(v)
        }
        Outcome::Cancellation(c) => {
            let mut v = Verdict::blank("cancellation");
            v.interaction = c.interaction.interaction;
            Some(v)
        }
        Outcome::Terminal(Terminal::Fault(fault)) => {
            let mut v = Verdict::blank("fault");
            v.class = failure_class(fault.failure_class).to_owned();
            Some(v)
        }
        // An ordinary teardown is scenario shutdown, not a verdict.
        Outcome::Terminal(Terminal::Teardown(_)) => None,
    }
}

// --- an independent host peer ----------------------------------------------

fn send(fd: &OwnedFd, bytes: &[u8], rights: &[i32]) {
    let mut iov = libc::iovec {
        iov_base: bytes.as_ptr() as *mut _,
        iov_len: bytes.len(),
    };
    let mut control = [0usize; 16];
    // SAFETY: all pointers below refer to appropriately aligned live buffers.
    unsafe {
        let mut msg: libc::msghdr = std::mem::zeroed();
        msg.msg_iov = &mut iov;
        msg.msg_iovlen = 1;
        if !rights.is_empty() {
            msg.msg_control = control.as_mut_ptr().cast();
            msg.msg_controllen = libc::CMSG_SPACE(std::mem::size_of_val(rights) as u32) as _;
            let h = libc::CMSG_FIRSTHDR(&msg);
            (*h).cmsg_level = libc::SOL_SOCKET;
            (*h).cmsg_type = libc::SCM_RIGHTS;
            (*h).cmsg_len = libc::CMSG_LEN(std::mem::size_of_val(rights) as u32) as _;
            std::ptr::copy_nonoverlapping(
                rights.as_ptr().cast::<u8>(),
                libc::CMSG_DATA(h),
                std::mem::size_of_val(rights),
            );
        }
        #[cfg(target_os = "linux")]
        let flags = libc::MSG_DONTWAIT | libc::MSG_NOSIGNAL;
        #[cfg(target_os = "macos")]
        let flags = libc::MSG_DONTWAIT;
        assert_eq!(
            libc::sendmsg(fd.as_raw_fd(), &msg, flags),
            bytes.len() as isize,
            "{}",
            std::io::Error::last_os_error()
        );
    }
}

fn drain(events: &Receiver<Verdict>, quiet: Duration, deadline: Instant) -> Vec<Verdict> {
    let mut got = Vec::new();
    while Instant::now() < deadline {
        match events.recv_timeout(quiet) {
            Ok(v) => got.push(v),
            // Quiet for a whole window, or the listener has finished: either
            // way there is nothing more this scenario will produce.
            Err(RecvTimeoutError::Timeout) | Err(RecvTimeoutError::Disconnected) => break,
        }
    }
    got
}

fn replay(scenario: &Json) -> Vec<Verdict> {
    let name = scenario.get("name").expect("a scenario name").str();
    let (host, plugin) = socket_pair().expect("socketpair");
    let (bell, listener) = Doorbell::new(plugin, Config::default()).expect("adopt");
    let (tx, events) = mpsc::channel();
    let listening = thread::spawn(move || {
        let _ = listener.run(|outcome| {
            if let Some(v) = normalise(outcome) {
                let _ = tx.send(v);
            }
        });
    });

    for call in scenario.get("lifecycle").map_or(&[][..], Json::arr) {
        let call = call.arr();
        match call[0].str() {
            "tool_activated" => bell
                .tool_activated(call[1].u64() as u32)
                .unwrap_or_else(|e| panic!("{name}: tool_activated: {e}")),
            "epoch_acknowledged" => bell
                .epoch_acknowledged(call[1].u64() as u8, call[2].u64() as u32)
                .unwrap_or_else(|e| panic!("{name}: epoch_acknowledged: {e}")),
            other => panic!("{name}: unknown lifecycle call {other:?}"),
        }
    }

    let with_descriptor = scenario.get("fds_per_record").map_or(0, Json::u64);
    assert!(
        with_descriptor <= 1,
        "{name}: only one attached fd is modeled"
    );
    let mut spare = Vec::new();
    for record in scenario.get("records").expect("records").arr() {
        let bytes = unhex(record.str());
        if with_descriptor == 1 {
            let pair = socket_pair().expect("a descriptor to grant");
            send(&host, &bytes, &[pair.1.as_raw_fd()]);
            spare.push(pair);
        } else {
            send(&host, &bytes, &[]);
        }
        thread::sleep(Duration::from_millis(3));
    }

    let got = drain(
        &events,
        Duration::from_millis(250),
        Instant::now() + Duration::from_secs(3),
    );
    drop(spare);
    bell.stop();
    listening.join().expect("the listener thread");
    drop(host);
    got
}

// --- the comparison ---------------------------------------------------------

/// Keys that describe the scenario rather than grade it. Everything else in a
/// scenario object is another implementation's verdict column.
const STRUCTURE: [&str; 5] = ["name", "lifecycle", "records", "fds_per_record", "terminal"];

/// Every column in the corpus except this SDK's own: one entry per named
/// implementation, in the order the corpus lists them.
fn columns(scenario: &Json) -> Vec<(String, Vec<Verdict>)> {
    let Json::Obj(fields) = scenario else {
        panic!("a scenario is an object")
    };
    fields
        .iter()
        .filter(|(key, _)| key != "rust" && !STRUCTURE.contains(&key.as_str()))
        .map(|(key, value)| {
            (
                key.clone(),
                value.arr().iter().map(from_json).collect::<Vec<_>>(),
            )
        })
        .collect()
}

/// Each verdict kind on its own, in the order it was produced.
fn by_kind(column: &[Verdict]) -> BTreeMap<&'static str, Vec<&Verdict>> {
    let mut kinds: BTreeMap<&'static str, Vec<&Verdict>> = BTreeMap::new();
    for v in column {
        kinds.entry(v.kind).or_default().push(v);
    }
    kinds
}

/// What separates two columns under this scenario's comparison mode, which is
/// strict unless the corpus lists the scenario as unsettled.
fn compare(mode: &str, name: &str, mine: &[Verdict], theirs: &[Verdict], who: &str) -> Vec<String> {
    let mut trouble = Vec::new();
    if mode == "interleaving" {
        // The documents fix each kind's own order and not the interleaving
        // between kinds, so compare the kinds separately.
        let (left, right) = (by_kind(mine), by_kind(theirs));
        for kind in left.keys().chain(right.keys()).copied().collect::<Vec<_>>() {
            let (one, two) = (left.get(kind), right.get(kind));
            if one != two {
                trouble.push(format!(
                    "{name}: {kind}:\n  rust   {one:?}\n  {who} {two:?}"
                ));
            }
        }
        trouble.dedup();
        return trouble;
    }
    if mine.len() != theirs.len() {
        trouble.push(format!(
            "{name}: verdict count: rust={} {who}={}",
            mine.len(),
            theirs.len()
        ));
    }
    for (i, (a, b)) in mine.iter().zip(theirs).enumerate() {
        if a != b {
            trouble.push(format!(
                "{name}: verdict {i}:\n  rust   {a:?}\n  {who} {b:?}"
            ));
        }
    }
    for (i, only) in mine.iter().enumerate().skip(theirs.len()) {
        trouble.push(format!("{name}: verdict {i} only in rust: {only:?}"));
    }
    for (i, only) in theirs.iter().enumerate().skip(mine.len()) {
        trouble.push(format!("{name}: verdict {i} only in {who}: {only:?}"));
    }
    trouble
}

#[test]
#[ignore = "requires VIZI_V2_CORPUS; run explicitly with --ignored (crosscheck CI does this)"]
fn cross_check_against_the_other_sdks() {
    let path = std::env::var("VIZI_V2_CORPUS")
        .expect("set VIZI_V2_CORPUS to the corpus from scripts/gen_v2_crosscheck.py");
    let text = fs::read_to_string(path).expect("read the corpus");
    let corpus = Reader::parse(&text);
    let scenarios = corpus.get("scenarios").expect("scenarios").arr();

    // A question the documents leave open is data the corpus carries, so every
    // implementation relaxes exactly the same comparison and no more.
    let modes: BTreeMap<&str, &str> = corpus
        .get("unsettled")
        .map_or(&[][..], Json::arr)
        .iter()
        .map(|entry| {
            (
                entry.get("scenario").expect("a scenario name").str(),
                entry.get("compare").map_or("strict", Json::str),
            )
        })
        .collect();

    let mut trouble = Vec::new();
    let mut compared: BTreeMap<String, usize> = BTreeMap::new();
    let mut mine = String::from("{\n \"scenarios\": [\n");
    for (n, scenario) in scenarios.iter().enumerate() {
        let name = scenario.get("name").expect("a name").str();
        let mode = modes.get(name).copied().unwrap_or("strict");
        let got = replay(scenario);
        for (who, want) in columns(scenario) {
            let found = compare(mode, name, &got, &want, &who);
            if mode == "report" {
                // A content divergence with an open question against the
                // documents; printing it is the point, failing is not.
                for line in &found {
                    eprintln!("unsettled: {line}");
                }
            } else {
                trouble.extend(found);
            }
            *compared.entry(who).or_default() += want.len().min(got.len());
        }
        let verdicts: Vec<String> = got.iter().map(Verdict::to_json).collect();
        mine.push_str(&format!(
            "  {{\"name\": \"{name}\", \"rust\": [\n   {}\n  ]}}{}\n",
            verdicts.join(",\n   "),
            if n + 1 == scenarios.len() { "" } else { "," }
        ));
    }
    mine.push_str(" ]\n}\n");
    if let Ok(out) = std::env::var("VIZI_V2_VERDICTS") {
        let mut file = fs::File::create(&out).expect("write the verdicts");
        file.write_all(mine.as_bytes()).expect("write the verdicts");
        eprintln!("wrote {out}");
    }
    for (who, count) in &compared {
        eprintln!(
            "rust vs {who}: {count} verdicts over {} scenarios",
            scenarios.len()
        );
    }
    // Writing the first column of a --scenarios-only corpus is a legitimate run;
    // grading against nothing is not.
    assert!(
        !compared.is_empty() || std::env::var_os("VIZI_V2_VERDICTS").is_some(),
        "the corpus carried no other SDK's verdicts, and none were written"
    );
    assert!(trouble.is_empty(), "\n{}", trouble.join("\n"));
}
