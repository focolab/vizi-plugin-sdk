#!/usr/bin/env bash
# protoc against proto/, from an isolated pinned toolchain.
#
# Deliberately NOT run out of .venv.  grpcio-tools 1.60.1 (the generator that
# produced the committed stubs) caps protobuf at <5 and ships no Python 3.12+
# wheels; installing it into the baseline env would drag protobuf back to 4.x
# and pin the interpreter.  `uv run --no-project` gives it its own throwaway
# env instead, so the baseline stays on the current runtime.
#
# The generator version is pinned because protoc output differs across
# versions: newer generators emit protobuf-6-only runtime guards, which
# reintroduces the TensorFlow-2.15 incompatibility the 4.25 gencode avoids.
# CI (.github/workflows/ci.yml) pins the same 1.60.1 for its freshness diff.
#
# Args pass straight through to protoc, with -I proto already set:
#
#   scripts/protoc.sh --python_out=OUT proto/vizi_plugin.proto proto/roi.proto
#   scripts/protoc.sh --cpp_out=OUT     proto/vizi_plugin.proto proto/roi.proto
#   scripts/protoc.sh --java_out=OUT    proto/vizi_plugin.proto proto/roi.proto
#
# grpc_tools bundles a full protoc, so every built-in language backend
# (cpp/java/csharp/js/objc/php/ruby/kotlin/pyi) works for MESSAGE codegen.
# Only the *Python* gRPC service plugin is bundled (--grpc_python_out); a
# non-Python plugin needs that language's own grpc plugin binary on PATH, or
# use buf/protoc from your own toolchain — proto/ is the portable contract.
set -euo pipefail

cd "$(dirname "$0")/.."

# grpcio-tools 1.60.1 imports pkg_resources (removed in setuptools 81); the
# pin above keeps it working, this just silences the one deprecation notice
# it emits on every invocation.  Other warnings still surface.
export PYTHONWARNINGS='ignore:pkg_resources is deprecated as an API:UserWarning'

exec uv run --no-project --quiet --python 3.11 \
  --with grpcio-tools==1.60.1 \
  --with 'setuptools<81' \
  -- python -m grpc_tools.protoc -I proto "$@"
