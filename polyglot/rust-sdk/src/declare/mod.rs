//! Declare the interface once, alongside its implementation.
//!
//! [`PluginBuilder`] produces an immutable [`DeclaredPlugin`]: its manifest and
//! registry are assembled together. The low-level `Registry`, `Plugin` trait
//! and `Runtime` constructors are available for custom dispatchers.
//! Panels are host-side declarations; runtime updates use `Context`.
//! Filesystem writing and the CLI parser require the default-enabled `install`
//! feature. Declaration generation and serving also work without that feature.
#[cfg(feature = "install")]
mod install;
#[cfg(feature = "install")]
mod management;
mod types;
#[cfg(feature = "install")]
pub use install::{InstallOptions, ManagementCommand};
#[cfg(feature = "install")]
pub use management::{report_exit, ManagementError};
pub use types::*;

use crate::{
    control::{self, Context, Input, Runtime},
    manifest::Manifest,
    plugin::{Chunk, Exporter, Reader, Registry},
    proto as pb, Cancellation, Doorbell, Listener,
};
use serde::Serialize;
use serde_json::{json, Map, Value};
use std::{collections::BTreeMap, sync::Arc};

type Result<T> = std::result::Result<T, String>;

/// A declaration builder; `build(self)` seals registrations. Metadata is owned
/// here, not inferred by inspecting handler bodies or a second manifest.
pub struct PluginBuilder {
    id: String,
    name: String,
    version: String,
    protocol: (u32, u32),
    command: Option<Vec<String>>,
    startup_s: Option<f64>,
    owner: Arc<()>,
    registry: Registry,
    contributions: Map<String, Value>,
    panels: Vec<PanelSpec>,
    assets: Vec<AssetSpec>,
    permissions: Vec<Permission>,
    subscriptions: Vec<Subscription>,
    icons: BTreeMap<String, Vec<u8>>,
    errors: Vec<String>,
}
impl PluginBuilder {
    pub fn new(id: impl Into<String>, name: impl Into<String>, version: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            name: name.into(),
            version: version.into(),
            protocol: (1, 0),
            command: None,
            startup_s: None,
            owner: Arc::new(()),
            registry: Registry::new(),
            contributions: Map::new(),
            panels: Vec::new(),
            assets: Vec::new(),
            permissions: Vec::new(),
            subscriptions: Vec::new(),
            icons: BTreeMap::new(),
            errors: Vec::new(),
        }
    }

    /// Defaults to protocol 1.0. This is independent of the plugin version.
    pub fn minimum_protocol(&mut self, major: u32, minor: u32) -> &mut Self {
        self.protocol = (major, minor);
        self
    }

    /// Override the default absolute path to this executable. Each element is
    /// one argument, without shell quoting. Relative paths are relative to the
    /// installed plugin directory. On Unix, launchers must preserve the host's
    /// inherited doorbell descriptor (typically by exec'ing the plugin).
    pub fn launch_command(
        &mut self,
        command: impl IntoIterator<Item = impl Into<String>>,
    ) -> &mut Self {
        self.command = Some(command.into_iter().map(Into::into).collect());
        self
    }

    /// Advisory seconds expected from process start to Ready, for a heavy first
    /// start. Must be positive and finite. The host may cap or ignore this hint;
    /// plugin behavior must not depend on it being honoured.
    pub fn startup_s(&mut self, seconds: f64) -> &mut Self {
        self.startup_s = Some(seconds);
        self
    }

    fn record(&mut self, kind: &str, spec: &impl Serialize) {
        match serde_json::to_value(spec) {
            Ok(value) => self
                .contributions
                .entry(kind)
                .or_insert_with(|| json!([]))
                .as_array_mut()
                .unwrap()
                .push(value),
            Err(e) => self.errors.push(e.to_string()),
        }
    }

    /// Register the declaration and handler together; returns the reference for
    /// interactive widgets. Duplicate IDs are rejected at `build`.
    pub fn command(
        &mut self,
        spec: CommandSpec,
        handler: impl Fn(&Context, &pb::InvokeCommand) -> control::Result<pb::CommandDone>
            + Send
            + Sync
            + 'static,
    ) -> CommandRef {
        self.record("commands", &spec);
        self.registry = std::mem::take(&mut self.registry).command(&spec.id, handler);
        CommandRef {
            id: spec.id,
            owner: self.owner.clone(),
        }
    }

    /// Declare a plugin-shaped region type and register its computation.
    /// Also declare Permission::ContributeRois before build.
    pub fn region_type(
        &mut self,
        id: impl Into<String>,
        name: Option<String>,
        handler: impl Fn(&Context, &pb::ShapeEdit) -> control::Result<crate::shape::Shape>
            + Send
            + Sync
            + 'static,
    ) -> &mut Self {
        let id = id.into();
        self.region_type_declaration(id.clone(), name);
        self.registry = std::mem::take(&mut self.registry).shaper(id, handler);
        self
    }
    /// Declare an asked region type without registering a computation.
    /// Its commits receive the SDK's missing-shaper REJECTED response.
    /// Requires Permission::ContributeRois, like `region_type`.
    pub fn region_type_declaration(
        &mut self,
        id: impl Into<String>,
        name: Option<String>,
    ) -> &mut Self {
        let mut entry = json!({"id": id.into()});
        if let Some(name) = name {
            entry["name"] = json!(name);
        }
        self.contributions
            .entry("region_types")
            .or_insert_with(|| json!([]))
            .as_array_mut()
            .unwrap()
            .push(entry);
        self
    }
    pub fn on_shape_edit_ended(
        &mut self,
        hook: impl Fn(&Context, pb::ShapeEditEnded) -> control::Result<()> + Send + Sync + 'static,
    ) -> &mut Self {
        self.registry = std::mem::take(&mut self.registry).on_shape_edit_ended(hook);
        self
    }
    pub fn stage(&mut self, spec: StageSpec, stage: Arc<dyn crate::stage::Stage>) -> &mut Self {
        self.record("stages", &spec);
        self.registry = std::mem::take(&mut self.registry).stage(spec.id, stage);
        self
    }

    pub fn reader(
        &mut self,
        spec: impl Into<ReaderDeclaration>,
        factory: impl Fn(&Context, &pb::OpenReader) -> control::Result<Arc<dyn Reader>>
            + Send
            + Sync
            + 'static,
    ) -> &mut Self {
        let spec = spec.into();
        self.record("readers", &spec);
        self.registry = std::mem::take(&mut self.registry).reader(spec.reader.id, factory);
        self
    }

    pub fn exporter(
        &mut self,
        spec: ExporterSpec,
        factory: impl Fn(&Context, &pb::OpenExport) -> control::Result<Arc<dyn Exporter>>
            + Send
            + Sync
            + 'static,
    ) -> &mut Self {
        self.record("exporters", &spec);
        self.registry = std::mem::take(&mut self.registry).exporter(spec.id, factory);
        self
    }

    pub fn panel(&mut self, spec: PanelSpec) -> &mut Self {
        self.panels.push(spec);
        self
    }

    pub fn overlay(&mut self, spec: OverlaySpec) -> &mut Self {
        self.record("overlays", &spec);
        self
    }

    /// Chunked overlays always register their source with the same ID used in
    /// the manifest, and cannot declare a fixed arena size.
    pub fn chunked_overlay(
        &mut self,
        spec: ChunkedOverlaySpec,
        source: impl Fn(&Context, &pb::ChunkWant) -> control::Result<Option<Chunk>>
            + Send
            + Sync
            + 'static,
    ) -> &mut Self {
        let mut value = json!(spec);
        value["chunked"] = json!(true);
        self.record("overlays", &value);
        self.registry = std::mem::take(&mut self.registry).chunk_source(spec.id, source);
        self
    }

    /// Tool metadata uses the plugin-wide input/lifecycle callbacks.
    /// `Input::binding` and `Input::action()` identify the tool and action.
    pub fn tool(&mut self, spec: ToolSpec) -> &mut Self {
        self.record("tools", &spec);
        self
    }
    pub fn asset(&mut self, spec: AssetSpec) -> &mut Self {
        self.assets.push(spec);
        self
    }
    /// Permissions are explicitly requested, never inferred from code.
    pub fn permission(&mut self, permission: Permission) -> &mut Self {
        self.permissions.push(permission);
        self
    }

    /// Receive dataset and pane epochs even without a tool contribution.
    /// The runtime acknowledges arrangements before exposing them to callers.
    pub fn subscription(&mut self, subscription: Subscription) -> &mut Self {
        self.subscriptions.push(subscription);
        self
    }

    /// Supply a pane icon, normally with `include_bytes!`. It is validated at
    /// build time and written alongside the generated manifest. There is no
    /// dependency on the process's working directory or the source tree.
    pub fn icon(&mut self, path: impl Into<String>, bytes: impl AsRef<[u8]>) -> &mut Self {
        let path = path.into().replace('\\', "/");
        if path.contains([':', '\0']) || path.split('/').any(|s| matches!(s, "" | "." | "..")) {
            self.errors.push(format!(
                "icon '{path}' must be a relative path with plain components"
            ));
        } else if self
            .icons
            .insert(path.clone(), bytes.as_ref().to_vec())
            .is_some()
        {
            self.errors.push(format!("duplicate icon '{path}'"));
        }
        self
    }

    pub fn on_input(
        &mut self,
        hook: impl Fn(&Context, Input) -> control::Result<()> + Send + Sync + 'static,
    ) -> &mut Self {
        self.registry = std::mem::take(&mut self.registry).on_input(hook);
        self
    }
    pub fn on_cancel(
        &mut self,
        hook: impl Fn(&Context, Cancellation) -> control::Result<()> + Send + Sync + 'static,
    ) -> &mut Self {
        self.registry = std::mem::take(&mut self.registry).on_cancel(hook);
        self
    }
    pub fn on_activated(
        &mut self,
        hook: impl Fn(&Context, pb::ToolBinding) -> control::Result<()> + Send + Sync + 'static,
    ) -> &mut Self {
        self.registry = std::mem::take(&mut self.registry).on_activated(hook);
        self
    }
    pub fn on_deactivated(
        &mut self,
        hook: impl Fn(&Context, pb::ToolBinding) -> control::Result<()> + Send + Sync + 'static,
    ) -> &mut Self {
        self.registry = std::mem::take(&mut self.registry).on_deactivated(hook);
        self
    }
    /// Register a view-state handler; declare Subscription::View and DataAccess.
    pub fn on_view_changed(
        &mut self,
        hook: impl Fn(&Context, pb::ViewChanged) -> control::Result<()> + Send + Sync + 'static,
    ) -> &mut Self {
        self.registry = std::mem::take(&mut self.registry).on_view_changed(hook);
        self
    }
    /// Register a region-state handler; declare Subscription::Regions and DataAccess.
    pub fn on_regions_changed(
        &mut self,
        hook: impl Fn(&Context, pb::RegionsChanged) -> control::Result<()> + Send + Sync + 'static,
    ) -> &mut Self {
        self.registry = std::mem::take(&mut self.registry).on_regions_changed(hook);
        self
    }
    /// Drive a person-started Follow; declare follow_targets on the panel.
    pub fn follow(
        &mut self,
        panel: impl Into<String>,
        handler: impl Fn(&Context, &control::Follow) -> control::Result<()> + Send + Sync + 'static,
    ) -> &mut Self {
        self.registry = std::mem::take(&mut self.registry).follow(panel, handler);
        self
    }
    pub fn on_panel_opened(
        &mut self,
        hook: impl Fn(&Context, pb::PanelOpened) -> control::Result<()> + Send + Sync + 'static,
    ) -> &mut Self {
        self.registry = std::mem::take(&mut self.registry).on_panel_opened(hook);
        self
    }
    pub fn on_shutdown(
        &mut self,
        hook: impl Fn(&Context, pb::Shutdown) -> control::Result<()> + Send + Sync + 'static,
    ) -> &mut Self {
        self.registry = std::mem::take(&mut self.registry).on_shutdown(hook);
        self
    }

    /// Seal registration and validate the generated schema-1 manifest, including
    /// widget references and icon contents. Handlers/factories are not invoked.
    pub fn build(mut self) -> Result<DeclaredPlugin> {
        if !self.errors.is_empty() {
            return Err(self.errors.join("; "));
        }
        for panel in std::mem::take(&mut self.panels) {
            panel.root.check_owner(&self.owner)?;
            self.record("panels", &panel);
        }
        if !self.errors.is_empty() {
            return Err(self.errors.join("; "));
        }
        let command = match self.command {
            Some(c) => c,
            None => {
                let path = std::env::current_exe()
                    .map_err(|e| format!("locating running executable: {}\nTo fix: Restore or rebuild this executable and rerun the command.", crate::manifest::io_cause(&e)))?;
                vec![path
                    .canonicalize()
                    .map_err(|e| format!("resolving running executable {}: {}\nTo fix: Restore or rebuild this executable and rerun the command.", path.display(), crate::manifest::io_cause(&e)))?
                    .to_str()
                    .ok_or_else(|| {
                        format!(
                            "executable path {} cannot be represented as a JSON string",
                            path.display()
                        )
                    })?
                    .to_owned()]
            }
        };
        if command.first().is_none_or(|s| s.trim().is_empty())
            || command.iter().any(|s| s.contains('\0'))
        {
            return Err(
                "launch command needs a non-blank executable and arguments without NUL.\nTo fix: Set PluginBuilder::launch_command to the intended executable (or remove the override), rebuild, and retry.".into(),
            );
        }
        let mut entry = json!({"command": command});
        if let Some(seconds) = self.startup_s {
            // JSON serialization would turn NaN/infinity into null, silently
            // dropping the request. Reject them before validating the manifest.
            entry["startup_s"] = serde_json::Number::from_f64(seconds)
                .ok_or("entry.startup_s must be a positive number of seconds")?
                .into();
        }
        let value = json!({
            "manifest_version": 1, "id": self.id, "name": self.name, "version": self.version,
            "engines": {"vizi": format!("{}.{}", self.protocol.0, self.protocol.1)},
            "entry": entry, "contributions": self.contributions,
            "permissions": self.permissions, "assets": self.assets,
            "subscriptions": self.subscriptions,
        });
        let manifest = Manifest::parse_with_icons(&value.to_string(), &|path| {
            self.icons.get(path).cloned().ok_or_else(|| {
                format!("pane icon '{path}' was not supplied with PluginBuilder::icon\nTo fix: Supply {path} with PluginBuilder::icon, rebuild, and retry.")
            })
        })?;
        Ok(DeclaredPlugin {
            manifest,
            registry: self.registry,
            #[cfg(feature = "install")]
            icons: self.icons,
        })
    }
}

/// An immutable interface and its implementation. Serving uses this generated
/// manifest, not a separately authored `vizi-plugin.json` in the working directory.
pub struct DeclaredPlugin {
    manifest: Manifest,
    registry: Registry,
    #[cfg(feature = "install")]
    icons: BTreeMap<String, Vec<u8>>,
}
impl DeclaredPlugin {
    pub fn manifest(&self) -> &Manifest {
        &self.manifest
    }

    /// Build a runtime with a transport supplied by an embedder or test host.
    pub fn into_runtime(self, doorbell: Doorbell, listener: Listener) -> control::Result<Runtime> {
        Runtime::new(self.manifest, Arc::new(self.registry), doorbell, listener)
    }

    /// Adopt Vizi's launch environment with the generated interface.
    ///
    /// # Safety
    /// On Unix, the inherited doorbell descriptor must be exclusively owned by
    /// this call; see `Runtime::from_env`. Call only once per launch.
    pub unsafe fn into_runtime_from_env(self) -> control::Result<Runtime> {
        unsafe { Runtime::from_env(self.manifest, Arc::new(self.registry)) }
    }
}
