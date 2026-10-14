#!/usr/bin/env bash
# Generate from the published IDL, never from another SDK's generated bindings.
set -euo pipefail
sdk_dir="$(cd "$(dirname "$0")" && pwd)"
repo_dir="$(cd "$sdk_dir/../.." && pwd)"
tools_dir="$sdk_dir/.tools/bin"
output_dir="$sdk_dir/protocol"
if [[ "${1:-}" == "--check" ]]; then
  output_dir="$(mktemp -d)"
  trap 'rm -rf "$output_dir"' EXIT
elif [[ $# -ne 0 ]]; then
  echo "usage: bash regenerate-proto.sh [--check]" >&2
  exit 2
fi
mkdir -p "$tools_dir" "$output_dir"
GOBIN="$tools_dir" go install google.golang.org/protobuf/cmd/protoc-gen-go@v1.36.12
GOBIN="$tools_dir" go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@v1.6.2
cd "$repo_dir"
package=github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol
PATH="$tools_dir:$PATH" scripts/protoc.sh \
  --go_out="$output_dir" --go_opt=paths=source_relative \
  --go_opt="Mroi.proto=$package" --go_opt="Mvizi_plugin.proto=$package" \
  --go-grpc_out="$output_dir" --go-grpc_opt=paths=source_relative \
  --go-grpc_opt="Mroi.proto=$package" --go-grpc_opt="Mvizi_plugin.proto=$package" \
  proto/roi.proto proto/vizi_plugin.proto

if [[ "${1:-}" == "--check" ]]; then
  for generated in "$output_dir"/*.pb.go; do
    diff -u "$sdk_dir/protocol/$(basename "$generated")" "$generated"
  done
fi
