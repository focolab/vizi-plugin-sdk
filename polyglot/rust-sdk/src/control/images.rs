use super::*;
use serde_json::Value;

type Key = (String, String, String); // panel, widget, row
#[derive(Default)]
pub(super) struct Images {
    held: BTreeMap<Key, Vec<u8>>,
    bytes: usize,
}
impl Images {
    pub fn prune(&mut self, manifest: &crate::manifest::Manifest, patch: &pb::PanelPatch) {
        fn cells<'a>(node: &'a Value, list: &str) -> Option<&'a Vec<Value>> {
            if node["id"] == list && node["kind"] == "list" {
                return node["row"].as_array();
            }
            node["children"]
                .as_array()?
                .iter()
                .find_map(|n| cells(n, list))
        }
        for op in &patch.ops {
            let Some(pb::patch_op::Op::SetRows(rows)) = &op.op else {
                continue;
            };
            let Some(cells) = manifest
                .panel_root(&patch.panel_id)
                .and_then(|n| cells(n, &op.widget_id))
            else {
                continue;
            };
            self.held.retain(|(panel, widget, row), bytes| {
                let keep = panel != &patch.panel_id
                    || row.is_empty()
                    || !cells.iter().any(|c| c["id"] == *widget)
                    || rows.keys.contains(row);
                if !keep {
                    self.bytes -= bytes.len();
                }
                keep
            });
        }
    }
    fn hold(&mut self, key: Key, png: &[u8]) -> Result<()> {
        // The reference limits all held images of a panel to its default
        // arena budget, excluding the value this update replaces.
        let others: usize = self
            .held
            .iter()
            .filter(|(k, _)| k.0 == key.0 && **k != key)
            .map(|(_, bytes)| bytes.len())
            .sum();
        if png.len() > 16 * 1024 * 1024 - others {
            return Err(Error::from(crate::memory::Failure::too_large(
                "held panel images exceed the 16 MiB P arena budget",
            )));
        }
        let bytes = self.bytes - self.held.get(&key).map_or(0, Vec::len) + png.len();
        if bytes > 64 * 1024 * 1024 || (!self.held.contains_key(&key) && self.held.len() >= 65536) {
            return Err(Error::internal("held panel image budget exceeded"));
        }
        self.held.insert(key, png.to_vec());
        self.bytes = bytes;
        Ok(())
    }
    fn remove(&mut self, key: &Key) {
        if let Some(old) = self.held.remove(key) {
            self.bytes -= old.len();
        }
    }
}
impl Context {
    pub(crate) fn hold_or_push_image(
        &self,
        panel: &str,
        widget: &str,
        row: &str,
        png: &[u8],
    ) -> Result<()> {
        if self.core.manifest.panel_root(panel).is_none() {
            return Err(Error::protocol("undeclared panel"));
        }
        if row.chars().count() > 128 || png.is_empty() {
            return Err(Error::protocol(
                "image needs nonempty bytes and a row key of at most 128 characters",
            ));
        }
        let mut images = self.core.images.lock().unwrap();
        let key = (panel.into(), widget.into(), row.into());
        if self.send_panel_image(&key, png)? {
            images.remove(&key);
        } else {
            images.hold(key, png)?;
        }
        Ok(())
    }
    /// Publish row images in bounded batches, holding them until the panel's
    /// segment installs. Each pair is a row key and PNG bytes for `widget`.
    /// Earlier batches may already be sent if a later batch fails.
    pub fn push_image_rows(
        &self,
        panel: &str,
        widget: &str,
        rows: &[(String, Vec<u8>)],
    ) -> Result<()> {
        if self.core.manifest.panel_root(panel).is_none() {
            return Err(Error::protocol("undeclared panel"));
        }
        for batch in rows.chunks(64) {
            let mut images = self.core.images.lock().unwrap();
            let mut ops = Vec::new();
            let mut loans = Vec::new();
            for (row, png) in batch {
                let key = (panel.into(), widget.into(), row.clone());
                let result = (|| {
                    if row.chars().count() > 128 || png.is_empty() {
                        return Err(Error::protocol(
                            "image needs nonempty bytes and a row key of at most 128 characters",
                        ));
                    }
                    if let Some((op, loan)) = self.prepare_panel_image(&key, png)? {
                        ops.push(op);
                        loans.push(loan);
                    } else {
                        images.hold(key, png)?;
                    }
                    Ok(())
                })();
                if let Err(error) = result {
                    for loan in loans {
                        let _ = self.memory().cancel_unsent(loan);
                    }
                    return Err(error);
                }
            }
            let sent: Vec<_> = ops
                .iter()
                .map(|op| (panel.into(), widget.into(), op.row_key.clone()))
                .collect();
            self.send_image_batch(panel, ops, loans)?;
            for key in sent {
                images.remove(&key);
            }
        }
        Ok(())
    }
    fn prepare_panel_image(
        &self,
        key: &Key,
        png: &[u8],
    ) -> Result<Option<(pb::PatchOp, crate::memory::Loan)>> {
        let Some(loan) = self.memory().reserve_for_subject("P", &key.0, png, true)? else {
            return Ok(None);
        };
        Ok(Some((
            pb::PatchOp {
                widget_id: key.1.clone(),
                row_key: key.2.clone(),
                op: Some(pb::patch_op::Op::SetImage(pb::ImageRef {
                    segment_id: loan.segment_id,
                    offset: loan.offset,
                    len: loan.len,
                })),
            },
            loan,
        )))
    }
    // The image mutex serializes reserve/enqueue with set_rows and replay.
    fn send_image_batch(
        &self,
        panel: &str,
        ops: Vec<pb::PatchOp>,
        loans: Vec<crate::memory::Loan>,
    ) -> Result<()> {
        if ops.is_empty() {
            return Ok(());
        }
        let result = self.core.send(
            String::new(),
            Plugin::PanelPatch(pb::PanelPatch {
                panel_id: panel.into(),
                ops,
            }),
            2,
        );
        if result.is_err() {
            for loan in loans {
                let _ = self.memory().cancel_unsent(loan);
            }
        }
        result
    }
    fn send_panel_image(&self, key: &Key, png: &[u8]) -> Result<bool> {
        let Some((op, loan)) = self.prepare_panel_image(key, png)? else {
            return Ok(self.is_shutting_down());
        };
        self.send_image_batch(&key.0, vec![op], vec![loan])?;
        Ok(true)
    }
    pub(super) fn flush_panel_images(&self, panel: &str) {
        let mut images = self.core.images.lock().unwrap();
        let keys: Vec<_> = images
            .held
            .keys()
            .filter(|k| k.0 == panel)
            .cloned()
            .collect();
        for batch in keys.chunks(64) {
            let mut ops = Vec::new();
            let mut loans = Vec::new();
            for key in batch {
                match self.prepare_panel_image(key, &images.held[key]) {
                    Ok(None) => {} // Still waiting for an arena.
                    Ok(Some((op, loan))) => {
                        images.remove(key);
                        ops.push(op);
                        loans.push(loan);
                    }
                    Err(error) => {
                        images.remove(key);
                        if !self.is_shutting_down() {
                            eprintln!("vizi SDK: held panel image {key:?} dropped: {error}");
                        }
                    }
                }
            }
            if let Err(error) = self.send_image_batch(panel, ops, loans) {
                if !self.is_shutting_down() {
                    eprintln!("vizi SDK: held panel image batch for {panel} dropped: {error}");
                }
            }
        }
    }
}

#[cfg(all(test, target_os = "linux"))]
#[path = "image_tests.rs"]
mod tests;
