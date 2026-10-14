//! Separate workers let state handlers make host requests without blocking
//! reception. A mailbox holds pending state, not an unbounded event queue.
use super::*;
use crate::plugin::Plugin as Application;
use std::sync::Condvar;

struct Mailbox<T> {
    state: Mutex<(bool, Option<T>)>,
    wake: Condvar,
}
impl<T: Send + 'static> Mailbox<T> {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            state: Mutex::new((false, None)),
            wake: Condvar::new(),
        })
    }
    fn push(&self, value: T, merge: impl FnOnce(&mut T, T)) {
        let mut state = self.state.lock().unwrap();
        if state.0 {
            return;
        }
        if let Some(old) = &mut state.1 {
            merge(old, value);
        } else {
            state.1 = Some(value);
        }
        self.wake.notify_one();
    }
    fn stop(&self) {
        let mut state = self.state.lock().unwrap();
        *state = (true, None);
        self.wake.notify_one();
    }
    fn start(
        self: &Arc<Self>,
        name: &str,
        ctx: &Context,
        callback: impl Fn(&Context, T) -> Result<()> + Send + 'static,
    ) -> Result<()> {
        let mailbox = self.clone();
        let ctx = ctx.clone();
        std::thread::Builder::new()
            .name(name.into())
            .spawn(move || loop {
                let value = {
                    let mut state = mailbox.state.lock().unwrap();
                    while !state.0 && state.1.is_none() {
                        state = mailbox.wake.wait(state).unwrap();
                    }
                    if state.0 {
                        break;
                    }
                    state.1.take().unwrap()
                };
                if ctx.is_shutting_down() {
                    break;
                }
                match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    callback(&ctx, value)
                })) {
                    Ok(Ok(())) => {}
                    Ok(Err(_)) if ctx.is_shutting_down() => {}
                    Ok(Err(e)) => eprintln!("vizi SDK state callback error: {e}"),
                    Err(_) => {
                        ctx.core.fault("application state callback panicked");
                        break;
                    }
                }
            })?;
        Ok(())
    }
}

struct Regions {
    generation: u64,
    reset: bool,
    changes: BTreeMap<String, pb::RegionChange>,
}
impl From<pb::RegionsChanged> for Regions {
    fn from(value: pb::RegionsChanged) -> Self {
        let mut regions = Self {
            generation: value.open_generation,
            reset: value.reset,
            changes: BTreeMap::new(),
        };
        for change in value.changes {
            regions.insert(change);
        }
        regions
    }
}
impl Regions {
    fn insert(&mut self, change: pb::RegionChange) {
        // Pushes arrive on one ordered control stream. A later entry wins,
        // including a deletion whose zero revision cannot sequence changes.
        self.changes.insert(change.region_id.clone(), change);
    }
    fn merge(&mut self, next: Self) {
        if next.reset || self.generation != next.generation {
            *self = next;
            // A generation boundary invalidates old state even if a faulty
            // sender omitted reset. Never merge regions from two datasets.
            self.reset = true;
        } else {
            for change in next.changes.into_values() {
                self.insert(change);
            }
        }
    }
    fn into_wire(self) -> pb::RegionsChanged {
        pb::RegionsChanged {
            open_generation: self.generation,
            reset: self.reset,
            changes: self.changes.into_values().collect(),
        }
    }
}

pub(super) struct Subscriptions {
    view: Option<Arc<Mailbox<pb::ViewChanged>>>,
    regions: Option<Arc<Mailbox<Regions>>>,
}
impl Subscriptions {
    pub fn new(manifest: &crate::manifest::Manifest) -> Self {
        let has = |key: &str, value: &str| {
            manifest.value()[key]
                .as_array()
                .is_some_and(|a| a.iter().any(|v| v.as_str() == Some(value)))
        };
        let enabled = |name| has("permissions", "data_access") && has("subscriptions", name);
        Self {
            view: enabled("view").then(Mailbox::new),
            regions: enabled("regions").then(Mailbox::new),
        }
    }
    pub fn start(&self, ctx: &Context, app: &Arc<dyn Application>) -> Result<()> {
        if let Some(mailbox) = &self.view {
            let app = app.clone();
            mailbox.start("vizi-view", ctx, move |ctx, v| app.view_changed(ctx, v))?;
        }
        if let Some(mailbox) = &self.regions {
            let app = app.clone();
            mailbox.start("vizi-regions", ctx, move |ctx, v| {
                app.regions_changed(ctx, v.into_wire())
            })?;
        }
        Ok(())
    }
    pub fn view(&self, value: pb::ViewChanged) {
        if let Some(mailbox) = &self.view {
            mailbox.push(value, |old, new| *old = new);
        }
    }
    pub fn regions(&self, value: pb::RegionsChanged) {
        if let Some(mailbox) = &self.regions {
            mailbox.push(value.into(), Regions::merge);
        }
    }
    pub fn stop(&self) {
        if let Some(mailbox) = &self.view {
            mailbox.stop();
        }
        if let Some(mailbox) = &self.regions {
            mailbox.stop();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn batch(generation: u64, reset: bool, id: &str, revision: u64) -> Regions {
        pb::RegionsChanged {
            open_generation: generation,
            reset,
            changes: vec![pb::RegionChange {
                region_id: id.into(),
                revision,
                deleted: revision == 0,
                ..Default::default()
            }],
        }
        .into()
    }
    #[test]
    fn reset_deletion_recreation_and_generation_boundaries() {
        let mut pending = batch(1, true, "a", 10);
        pending.merge(batch(1, false, "b", 11));
        assert!(pending.reset);
        assert_eq!(pending.changes.len(), 2);
        pending.merge(batch(1, false, "a", 0));
        assert!(pending.changes["a"].deleted);
        pending.merge(batch(1, false, "a", 12));
        assert!(!pending.changes["a"].deleted);
        pending.merge(batch(1, true, "c", 13));
        assert_eq!(pending.changes.len(), 1);
        assert!(pending.changes.contains_key("c"));
        pending.merge(batch(2, false, "d", 1));
        assert!(pending.reset);
        assert_eq!(pending.changes.len(), 1);
        assert!(pending.changes.contains_key("d"));
    }
    #[test]
    fn subscriptions_require_both_declaration_and_data_permission() {
        for permission in [false, true] {
            for declared in [false, true] {
                let value = serde_json::json!({"manifest_version":1,"id":"x","name":"X","version":"1", "engines":{"vizi":"1.0"}, "entry":{"command":["x"]},
                    "permissions":if permission {vec!["data_access"]} else {vec![]},
                    "subscriptions":if declared {vec!["view", "regions"]} else {vec![]}});
                let manifest = crate::manifest::Manifest::parse(&value.to_string()).unwrap();
                let subscriptions = Subscriptions::new(&manifest);
                assert_eq!(subscriptions.view.is_some(), permission && declared);
                assert_eq!(subscriptions.regions.is_some(), permission && declared);
            }
        }
    }
}
