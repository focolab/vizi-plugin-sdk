#!/bin/sh
set -eu
cd "$(dirname "$0")"
# Build on a fresh checkout; subsequent launches check freshness. Keep Cargo's
# output off the control transport and explicitly fix the executable location.
cargo build --locked --quiet --target-dir target >&2
exec target/debug/stage-chain
