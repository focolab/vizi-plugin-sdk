//! Rust SDK for Vizi protocol 1.x, built from the published specification.
//!
//! Declare contributions with [`declare::PluginBuilder`] to generate the
//! manifest alongside their handlers. For lower-level control, use
//! [`plugin::Registry`] or implement [`plugin::Plugin`], then serve with
//! [`control::Runtime`]. The runtime owns
//! the control plane, doorbell, arrangements, shared memory and shutdown.
//! [`memory::Memory`] and the doorbell API are also available to embedders.
#![deny(unsafe_op_in_unsafe_fn)]

#[cfg(not(any(target_os = "linux", target_os = "macos", windows)))]
compile_error!("vizi-doorbell supports Linux, macOS and Windows");

mod channels;
pub use channels::ChannelName;
mod folders;
mod input;
#[cfg(unix)]
mod socket;
#[cfg(windows)]
#[path = "socket_windows.rs"]
mod socket;
#[cfg(any(windows, test))]
mod tcp;
mod transport;
mod unknown;
mod wire;

pub use folders::{settings_dir, SettingsDirUnavailableError};
pub use transport::*;
pub use wire::*;

/// Generated directly from the published IDL; no implementation sources used.
// Keep Prost's generated oneof layout and Tonic's by-value Status signatures.
#[allow(
    clippy::doc_lazy_continuation,
    clippy::large_enum_variant,
    clippy::result_large_err
)]
pub mod proto {
    pub mod pois_math {
        pub mod v1 {
            tonic::include_proto!("pois_math.v1");
            include!(concat!(env!("OUT_DIR"), "/pois_math.v1.messages.rs"));
        }
    }
    pub mod vizi {
        pub mod plugin {
            pub mod v1 {
                tonic::include_proto!("vizi.plugin.v1");
            }
        }
    }
    pub use crate::unknown::UnknownFields;
    pub use vizi::plugin::v1::*;
}

pub mod control;
pub use control::{shape, work};
pub mod declare;
pub mod manifest;
pub mod memory;
pub mod plugin;
pub mod stage;
