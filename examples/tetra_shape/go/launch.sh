#!/bin/sh
cd "$(dirname "$0")" || exit 1
if [ ! -x build/tetra-shape ]; then
  echo 'tetra-shape: run go build -o build/tetra-shape . first (see README.md)' >&2
  exit 1
fi
exec ./build/tetra-shape "$@"
