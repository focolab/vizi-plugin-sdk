//! One end of a live doorbell, for `scripts/head_to_head.py`.
//!
//! Adopts the descriptor in `VIZI_DOORBELL_FD` and either sends the `D` hints
//! named on the command line or prints the ones it receives, one canonical line
//! each. The other end is another SDK's peer over the same socketpair, so this
//! exercises this SDK's encoder against that one's decoder — no recording, and
//! no implementation grading another.
//!
//!     VIZI_DOORBELL_FD=3 doorbell_peer send 9/1/2/3,64
//!     VIZI_DOORBELL_FD=3 doorbell_peer receive
//!
//! A hint is written `segment/pane/epoch/regions`, with `-` for an absent pane.

#[cfg(unix)]
fn run() -> Result<(), Box<dyn std::error::Error>> {
    use std::{os::fd::FromRawFd, thread, time::Duration};
    use vizi_doorbell::*;

    // A sender outlives its own records; a receiver outlives the slowest
    // peer's startup, which is the JVM's. Both are harness timing, not protocol.
    const SEND_WINDOW: Duration = Duration::from_millis(1200);
    const RECEIVE_WINDOW: Duration = Duration::from_millis(2200);

    /// The canonical line every peer prints for one received hint.
    fn show(n: &Notification) -> String {
        let (pane, epoch) = match n.pane {
            Some(p) => (p.view_id.to_string(), p.epoch.to_string()),
            None => ("-".to_owned(), "-".to_owned()),
        };
        let regions: Vec<String> = n.mask.indices().map(|r| r.to_string()).collect();
        format!(
            "hint segment={} pane={pane} epoch={epoch} regions={}",
            n.segment_id,
            regions.join(",")
        )
    }

    fn parse(spec: &str) -> Result<Notification, Box<dyn std::error::Error>> {
        let parts: Vec<&str> = spec.split('/').collect();
        if parts.len() != 4 {
            return Err(format!("want segment/pane/epoch/regions, got {spec:?}").into());
        }
        let pane = match (parts[1], parts[2]) {
            ("-", "-") => None,
            (view, epoch) => Some(PaneEpoch {
                view_id: view.parse()?,
                epoch: epoch.parse()?,
            }),
        };
        let mut mask = RegionMask::default();
        for region in parts[3].split(',').filter(|r| !r.is_empty()) {
            mask.set(region.parse()?)?;
        }
        Ok(Notification {
            segment_id: parts[0].parse()?,
            mask,
            pane,
        })
    }

    let args: Vec<String> = std::env::args().skip(1).collect();
    let mode = args
        .first()
        .ok_or("usage: doorbell_peer send|receive [hint ...]")?;
    let fd: i32 = std::env::var("VIZI_DOORBELL_FD")?.parse()?;
    // SAFETY: the parent passed exactly this descriptor and does not use it.
    let socket = unsafe { std::os::fd::OwnedFd::from_raw_fd(fd) };
    let (bell, listener) = Doorbell::new(socket, Config::default())?;
    let listening = thread::spawn(move || {
        let _ = listener.run(|outcome| {
            if let Outcome::Frame(f) = outcome {
                match &f.disposition {
                    Disposition::Decoded(DecodedRecord::Notification(n)) => println!("{}", show(n)),
                    other => println!("other {other:?}"),
                }
            }
        });
    });
    bell.handshake_completed(1, 0)?;
    if mode == "send" {
        for spec in &args[1..] {
            match bell.notify(&parse(spec)?) {
                Ok(SendOutcome::Sent) => {}
                other => println!("refused {other:?}"),
            }
        }
        thread::sleep(SEND_WINDOW);
    } else {
        thread::sleep(RECEIVE_WINDOW);
    }
    bell.stop();
    listening.join().map_err(|_| "listener panicked")?;
    Ok(())
}

#[cfg(not(unix))]
fn run() -> Result<(), Box<dyn std::error::Error>> {
    Err("the head-to-head peer adopts an inherited descriptor, which is unix only".into())
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    run()
}
