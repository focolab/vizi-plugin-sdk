use super::*;
use std::{sync::mpsc, time::Duration};

/// Reader-owned opaque id and optional listing metadata. An empty shape or
/// DType::DtypeUnspecified means the listing does not know that property yet.
pub use pb::SeriesEntry;

#[derive(Clone, Debug)]
pub struct SeriesChoice(pub Vec<SeriesEntry>);

#[derive(Clone, Debug)]
pub enum ReaderOpen {
    Opened(pb::ReaderOpened),
    Series(SeriesChoice),
}

impl SeriesChoice {
    fn validate(&self) -> Result<()> {
        let bad = |message| {
            Error::new(
                pb::error::Kind::InvalidArgument,
                format!("open failed: the reader's SeriesChoice is not valid: {message}"),
            )
        };
        if self.0.is_empty() {
            return Err(bad(
                "a SeriesChoice lists no series; a file with no image is refused".into(),
            ));
        }
        let mut ids = std::collections::HashSet::new();
        for entry in &self.0 {
            if !ids.insert(&entry.id) {
                return Err(bad(format!("series id '{}' is listed twice", entry.id)));
            }
            if !entry.shape_tczyx.is_empty()
                && (entry.shape_tczyx.len() != 5 || entry.shape_tczyx.contains(&0))
            {
                return Err(bad(format!(
                    "series '{}': shape_tczyx must be five positive sizes",
                    entry.id
                )));
            }
            if !matches!(
                pb::DType::try_from(entry.dtype),
                Ok(pb::DType::DtypeUnspecified
                    | pb::DType::DtypeU8
                    | pb::DType::DtypeU16
                    | pb::DType::DtypeF32)
            ) {
                return Err(bad(format!(
                    "series '{}': dtype {} is not one of ['f32', 'u16', 'u8']",
                    entry.id, entry.dtype
                )));
            }
        }
        Ok(())
    }

    /// Validate and retain the ordered prefix that fits in a control message.
    pub fn into_list(self) -> Result<pb::SeriesList> {
        self.validate()?;
        let mut list = pb::SeriesList::default();
        let mut remaining = crate::control::MAX_MESSAGE_BYTES - 64;
        for entry in self.0 {
            let size = entry.encoded_len();
            let cost = size + 1 + prost::length_delimiter_len(size);
            if cost > remaining {
                list.truncated = true;
                break;
            }
            remaining -= cost;
            list.entries.push(entry);
        }
        Ok(list)
    }
}

pub(super) fn open_series(reader: &Arc<dyn Reader>, open: &pb::OpenReader) -> Result<ReaderOpen> {
    let mut request = open.clone();
    loop {
        match reader.open(&request)? {
            ReaderOpen::Series(choice) => {
                if let Some(series) = &request.series {
                    return Err(Error::new(pb::error::Kind::InvalidArgument, format!(
                        "open failed: asked for series '{series}', the reader answered with a list")));
                }
                choice.validate()?;
                if choice.0.len() == 1 {
                    request.series = Some(choice.0[0].id.clone());
                } else {
                    return Ok(ReaderOpen::Series(choice));
                }
            }
            opened => return Ok(opened),
        }
    }
}

pub(super) struct PendingReader {
    reader: Option<Arc<dyn Reader>>,
    uri: String,
    reader_id: String,
    token: Arc<()>,
    // Disconnecting cancels the idle worker immediately, including on Drop.
    _cancel: mpsc::Sender<()>,
}
impl Drop for PendingReader {
    fn drop(&mut self) {
        if let Some(reader) = self.reader.take() {
            close_reader(&reader);
        }
    }
}
impl Registry {
    pub(super) fn take_pending(&self, open: &pb::OpenReader) -> Option<Arc<dyn Reader>> {
        let _expiry = self.pending_expiry.lock().unwrap();
        let pending = self.reader_state.lock().unwrap().pending.take();
        pending.and_then(|mut held| {
            if open.series.is_some() && held.uri == open.uri && held.reader_id == open.reader_id {
                held.reader.take()
            } else {
                None // Drop closes outside the state lock, before same-URI release.
            }
        })
    }

    pub(super) fn hold_pending(
        &self,
        reader: Arc<dyn Reader>,
        open: &pb::OpenReader,
        idle: Duration,
    ) -> Result<()> {
        let (cancel, wait) = mpsc::channel();
        let token = Arc::new(());
        let pending = PendingReader {
            reader: Some(reader),
            uri: open.uri.clone(),
            reader_id: open.reader_id.clone(),
            token: token.clone(),
            _cancel: cancel,
        };
        let mut state = self.reader_state.lock().unwrap();
        if state.shutting_down {
            drop(state);
            drop(pending);
            return Err(Error::protocol("reader listing completed during shutdown"));
        }
        let previous = state.pending.replace(pending);
        drop(state);
        drop(previous);
        let weak = Arc::downgrade(&self.reader_state);
        let expiry = self.pending_expiry.clone();
        let spawned = std::thread::Builder::new()
            .name("vizi-series-choice".into())
            .spawn(move || {
                if wait.recv_timeout(idle) == Err(mpsc::RecvTimeoutError::Timeout) {
                    let _expiry = expiry.lock().unwrap();
                    if let Some(state) = weak.upgrade() {
                        let expired = {
                            let mut state = state.lock().unwrap();
                            if state
                                .pending
                                .as_ref()
                                .is_some_and(|p| Arc::ptr_eq(&p.token, &token))
                            {
                                state.pending.take()
                            } else {
                                None
                            }
                        };
                        drop(expired);
                    }
                }
            });
        if let Err(error) = spawned {
            // Retention is optional; failure to start the timer must not leak a file.
            eprintln!("vizi SDK: cannot retain series listing: {error}");
            let pending = self.reader_state.lock().unwrap().pending.take();
            drop(pending);
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct ClosingReader(mpsc::Sender<()>, bool);
    impl Reader for ClosingReader {
        fn read_volume(&self, _: &pb::ReadVolume) -> Result<Volume> {
            unreachable!()
        }
        fn close(&self) -> Result<()> {
            self.0.send(()).unwrap();
            if self.1 {
                panic!("test close panic");
            }
            Ok(())
        }
    }
    fn request(series: Option<&str>) -> pb::OpenReader {
        pb::OpenReader {
            uri: "file".into(),
            reader_id: "reader".into(),
            series: series.map(str::to_owned),
            ..Default::default()
        }
    }

    #[test]
    fn held_listing_expires_without_another_request_and_cleanup_panic_is_contained() {
        let app = Registry::new();
        let (closed, wait) = mpsc::channel();
        app.hold_pending(
            Arc::new(ClosingReader(closed, true)),
            &request(None),
            Duration::ZERO,
        )
        .unwrap();
        wait.recv_timeout(Duration::from_secs(5)).unwrap();
        // take_pending waits until expiration cleanup has finished.
        assert!(app.take_pending(&request(Some(""))).is_none());
        assert!(app.reader_state.lock().unwrap().pending.is_none());
    }

    #[test]
    fn matching_take_cancels_expiry_and_dropping_registry_closes_abandoned_listing() {
        let app = Registry::new();
        let (closed, wait) = mpsc::channel();
        let reader: Arc<dyn Reader> = Arc::new(ClosingReader(closed, false));
        app.hold_pending(reader.clone(), &request(None), Duration::from_secs(600))
            .unwrap();
        let taken = app.take_pending(&request(Some(""))).unwrap();
        assert!(Arc::ptr_eq(&reader, &taken));
        assert!(matches!(wait.try_recv(), Err(mpsc::TryRecvError::Empty)));
        app.hold_pending(taken, &request(None), Duration::from_secs(600))
            .unwrap();
        drop(app);
        wait.recv_timeout(Duration::from_secs(5)).unwrap();
        assert!(wait.try_recv().is_err());
    }

    #[test]
    fn another_reader_id_drops_the_listing_and_shutdown_refuses_late_listings() {
        let app = Registry::new();
        let (closed, wait) = mpsc::channel();
        let reader: Arc<dyn Reader> = Arc::new(ClosingReader(closed, false));
        app.hold_pending(reader.clone(), &request(None), Duration::from_secs(600))
            .unwrap();
        let mut other = request(Some(""));
        other.reader_id = "different".into();
        assert!(app.take_pending(&other).is_none());
        wait.recv_timeout(Duration::from_secs(5)).unwrap();
        app.reader_state.lock().unwrap().shutting_down = true;
        assert!(app
            .hold_pending(reader, &request(None), Duration::ZERO)
            .is_err());
        wait.recv_timeout(Duration::from_secs(5)).unwrap();
    }

    #[test]
    fn listing_validates_optional_metadata_even_beyond_the_trimmed_prefix() {
        for dtype in [0, 1, 2, 3] {
            SeriesChoice(vec![SeriesEntry {
                dtype,
                shape_tczyx: vec![1; 5],
                ..Default::default()
            }])
            .into_list()
            .unwrap();
        }
        for shape in [vec![1; 4], vec![1; 6], vec![1, 1, 0, 1, 1]] {
            let err = SeriesChoice(vec![SeriesEntry {
                shape_tczyx: shape,
                ..Default::default()
            }])
            .into_list()
            .unwrap_err();
            assert_eq!(err.kind, pb::error::Kind::InvalidArgument);
        }
        for dtype in [-1, 4, 999] {
            let err = SeriesChoice(vec![SeriesEntry {
                dtype,
                ..Default::default()
            }])
            .into_list()
            .unwrap_err();
            assert_eq!(err.kind, pb::error::Kind::InvalidArgument);
        }
        let entries = vec![
            SeriesEntry {
                description: "x".repeat(crate::control::MAX_MESSAGE_BYTES),
                ..Default::default()
            },
            SeriesEntry {
                id: "other".into(),
                dtype: 4,
                ..Default::default()
            },
        ];
        assert!(SeriesChoice(entries)
            .into_list()
            .unwrap_err()
            .message
            .contains("dtype"));
    }
}
