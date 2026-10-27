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

/// How large an `image` widget is drawn (`control_plane_protocol.md` §8.4):
/// device-independent pixels and the screen's pixel ratio. An image of
/// [`WidgetSize::pixels`] is shown one image pixel per screen pixel; any other
/// is scaled to fit. Clicks are reported in the pushed image's own pixels
/// either way.
#[derive(Clone, Debug, PartialEq)]
pub struct WidgetSize {
    pub panel_id: String,
    pub widget_id: String,
    pub width_px: u32,
    pub height_px: u32,
    /// Never zero: a host that sends none is read as 1.
    pub dpr: f64,
}
impl WidgetSize {
    /// The image size drawn 1:1, `(width, height)` in pixels: each side times
    /// `dpr`, rounded half to even as the reference SDK's `round` does.
    pub fn pixels(&self) -> (u32, u32) {
        let side = |px: u32| (f64::from(px) * self.dpr).round_ties_even() as u32;
        (side(self.width_px), side(self.height_px))
    }
}
impl From<pb::WidgetGeometry> for WidgetSize {
    fn from(g: pb::WidgetGeometry) -> Self {
        Self {
            panel_id: g.panel_id,
            widget_id: g.widget_id,
            width_px: g.width_px,
            height_px: g.height_px,
            dpr: if g.dpr.is_finite() && g.dpr > 0.0 {
                g.dpr
            } else {
                1.0
            },
        }
    }
}
/// One color of an [`Appearance`] as `(r, g, b, a)`, each 0-255.
pub type Rgba = (u8, u8, u8, u8);

/// An [`Appearance`]'s colors, in the order the message numbers them.
pub const APPEARANCE_COLORS: [&str; 6] = [
    "window_bg",
    "panel_bg",
    "text",
    "text_disabled",
    "accent",
    "border",
];

/// The host's theme and the colors it draws panels with
/// (`control_plane_protocol.md` §8.4, "Appearance"): what a plugin that
/// renders its own pictures -- a plot pushed to an `image` -- needs to match
/// them. Each color is `(r, g, b, a)`; [`Appearance::hex`] gives one as
/// `"#rrggbb"` and [`Appearance::hex_colors`] all of them.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Appearance {
    /// `"dark"` or `"light"`.
    pub theme: String,
    /// Behind a panel.
    pub window_bg: Rgba,
    /// Inside a field, a list or a table.
    pub panel_bg: Rgba,
    pub text: Rgba,
    pub text_disabled: Rgba,
    /// A selection's highlight.
    pub accent: Rgba,
    /// A frame or a grid line.
    pub border: Rgba,
}
impl Appearance {
    pub fn dark(&self) -> bool {
        self.theme == "dark"
    }
    /// One color by its name in [`APPEARANCE_COLORS`], `None` for any other.
    pub fn color(&self, name: &str) -> Option<Rgba> {
        Some(match name {
            "window_bg" => self.window_bg,
            "panel_bg" => self.panel_bg,
            "text" => self.text,
            "text_disabled" => self.text_disabled,
            "accent" => self.accent,
            "border" => self.border,
            _ => return None,
        })
    }
    /// One color as `"#rrggbb"` (alpha dropped), e.g. `hex("text")`; `None`
    /// for a name not in [`APPEARANCE_COLORS`].
    pub fn hex(&self, name: &str) -> Option<String> {
        self.color(name)
            .map(|(r, g, b, _)| format!("#{r:02x}{g:02x}{b:02x}"))
    }
    /// Every color as `"#rrggbb"`, by name.
    pub fn hex_colors(&self) -> BTreeMap<&'static str, String> {
        APPEARANCE_COLORS
            .iter()
            .filter_map(|&name| Some((name, self.hex(name)?)))
            .collect()
    }
}
fn rgba(value: u32) -> Rgba {
    let [r, g, b, a] = value.to_be_bytes();
    (r, g, b, a)
}
impl From<&pb::HostAppearance> for Appearance {
    fn from(m: &pb::HostAppearance) -> Self {
        Self {
            theme: m.theme.clone(),
            window_bg: rgba(m.window_bg),
            panel_bg: rgba(m.panel_bg),
            text: rgba(m.text),
            text_disabled: rgba(m.text_disabled),
            accent: rgba(m.accent),
            border: rgba(m.border),
        }
    }
}

/// The newest geometry per (panel, widget) not yet handed to the plugin.
type Geometries = BTreeMap<(String, String), pb::WidgetGeometry>;

pub(super) struct Subscriptions {
    view: Option<Arc<Mailbox<pb::ViewChanged>>>,
    regions: Option<Arc<Mailbox<Regions>>>,
    geometry: Option<Arc<Mailbox<Geometries>>>,
    /// The newest appearance heard, `None` before the first (§8.4).
    appearance: Mutex<Option<pb::HostAppearance>>,
    /// Wakes the appearance handler, which reads the newest when it runs.
    appearance_changed: Arc<Mailbox<()>>,
}
impl Subscriptions {
    pub fn new(manifest: &crate::manifest::Manifest) -> Self {
        let enabled = |name| manifest.arranges_subscription(name);
        Self {
            view: enabled("view").then(Mailbox::new),
            regions: enabled("regions").then(Mailbox::new),
            // The host sends geometry for any panel's image widgets, unasked.
            geometry: (!manifest.contribution_ids("panels").is_empty()).then(Mailbox::new),
            appearance: Mutex::new(None),
            appearance_changed: Mailbox::new(),
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
        if let Some(mailbox) = &self.geometry {
            let app = app.clone();
            mailbox.start("vizi-geometry", ctx, move |ctx, sizes| {
                // Every widget's newest size is delivered; the first failure
                // is reported after the rest have run.
                let mut first = Ok(());
                for geometry in sizes.into_values() {
                    let result = app.widget_geometry(ctx, geometry.into());
                    if first.is_ok() {
                        first = result;
                    }
                }
                first
            })?;
        }
        let app = app.clone();
        self.appearance_changed
            .start("vizi-appearance", ctx, move |ctx, ()| {
                match ctx.appearance() {
                    Some(appearance) => app.appearance(ctx, appearance),
                    None => Ok(()),
                }
            })?;
        Ok(())
    }
    /// Keep the newest appearance and wake its handler, which is handed the
    /// newest when it runs: several changes in a row may reach it as one.
    pub fn appearance(&self, value: pb::HostAppearance) {
        *self.appearance.lock().unwrap() = Some(value);
        self.appearance_changed.push((), |_, _| {});
    }
    pub fn newest_appearance(&self) -> Option<Appearance> {
        self.appearance
            .lock()
            .unwrap()
            .as_ref()
            .map(Appearance::from)
    }
    /// Keep the newest per (panel, widget): an older size never reaches the
    /// handler after a newer one has arrived.
    pub fn geometry(&self, value: pb::WidgetGeometry) {
        if let Some(mailbox) = &self.geometry {
            let key = (value.panel_id.clone(), value.widget_id.clone());
            mailbox.push([(key, value)].into(), |old, new| old.extend(new));
        }
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
        if let Some(mailbox) = &self.geometry {
            mailbox.stop();
        }
        self.appearance_changed.stop();
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
    fn appearance_colours_are_rgba_bytes_and_hex() {
        let appearance = Appearance::from(&pb::HostAppearance {
            theme: "dark".into(),
            window_bg: 0x1e1e1eff,
            panel_bg: 0x2a2b2cff,
            text: 0xf0f1f2ff,
            text_disabled: 0x80808080,
            accent: 0x3d7ae5ff,
            border: 0x00000000,
        });
        assert!(appearance.dark());
        assert_eq!(appearance.window_bg, (0x1e, 0x1e, 0x1e, 0xff));
        assert_eq!(appearance.text_disabled, (0x80, 0x80, 0x80, 0x80));
        assert_eq!(appearance.color("accent"), Some((0x3d, 0x7a, 0xe5, 0xff)));
        assert_eq!(appearance.hex("panel_bg").as_deref(), Some("#2a2b2c"));
        assert_eq!(appearance.hex("nothing"), None);
        let all = appearance.hex_colors();
        assert_eq!(all.len(), APPEARANCE_COLORS.len());
        assert_eq!(all["border"], "#000000");
        assert_eq!(all["text"], "#f0f1f2");
        assert!(!Appearance::from(&pb::HostAppearance::default()).dark());
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
                let value = serde_json::json!({"manifest_version":1,"id":"org.example.x","name":"X","version":"1", "engines":{"vizi":"1.0"}, "entry":{"command":["x"]},
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
