#!/usr/bin/env bash
# Regenerate the committed Python stubs in vizi_plugin_sdk/_proto/ from proto/.
#
#   scripts/regen-stubs.sh          # rewrite the committed stubs in place
#   scripts/regen-stubs.sh --check  # fail if they are stale (what CI does)
#
# Intra-package imports are rewritten to relative form so
# vizi_plugin_sdk._proto imports cleanly as a package.  roi.proto is generated
# alongside vizi_plugin.proto, which imports it.
#
# NOTE: vizi_plugin_sdk/ and proto/ are published into this repository from
# vizi's source tree, so --check (verifying the committed stubs) is the
# routine use.
set -euo pipefail

cd "$(dirname "$0")/.."

check=0
[ "${1:-}" = "--check" ] && check=1

out="$(mktemp -d)"
trap 'rm -rf "$out"' EXIT

scripts/protoc.sh --python_out="$out" --grpc_python_out="$out" \
  proto/vizi_plugin.proto proto/roi.proto

sed -i 's/^import vizi_plugin_pb2/from . import vizi_plugin_pb2/' \
  "$out/vizi_plugin_pb2_grpc.py"
sed -i 's/^import roi_pb2/from . import roi_pb2/' \
  "$out/vizi_plugin_pb2.py" "$out/roi_pb2_grpc.py"

stubs=(vizi_plugin_pb2.py vizi_plugin_pb2_grpc.py roi_pb2.py roi_pb2_grpc.py)
status=0
for f in "${stubs[@]}"; do
    if [ "$check" = 1 ]; then
        diff -u "vizi_plugin_sdk/_proto/$f" "$out/$f" || status=1
    else
        cp "$out/$f" "vizi_plugin_sdk/_proto/$f"
    fi
done

if [ "$check" = 1 ]; then
    [ "$status" = 0 ] && echo "stubs up to date" || echo "STALE: re-run scripts/regen-stubs.sh" >&2
    exit "$status"
fi
echo "regenerated ${#stubs[@]} stubs in vizi_plugin_sdk/_proto/"
