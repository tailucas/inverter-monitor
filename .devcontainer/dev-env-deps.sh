#!/usr/bin/env bash
set -eu
set -o pipefail

if ! task --version; then
  sh -c "$(curl -sSL https://taskfile.dev/install.sh)" -- -b ~/.local/bin
fi

if ! uv --version; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

uv python install
uv sync
