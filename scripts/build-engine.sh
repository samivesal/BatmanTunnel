#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
command -v go >/dev/null || { echo 'Go 1.26.6+ is required to build the native engine.' >&2; exit 1; }
mkdir -p "$ROOT/bin"
cd "$ROOT/engine"
CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o "$ROOT/bin/batmantunnel-engine" .
