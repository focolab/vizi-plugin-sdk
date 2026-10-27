#!/bin/sh
set -eu
cd "$(dirname "$0")"
go build -o stage-chain .
exec ./stage-chain "$@"
