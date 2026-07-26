#!/usr/bin/env bash
set -eu
set -o pipefail

if ! uv --version; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

uv python install
uv sync
