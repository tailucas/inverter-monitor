#!/usr/bin/env bash
# Generate the runtime `.env` for this project from 1Password.
#
#   ./dot_env_setup.sh [project-name]
#
# Reads the `ENV.<project>` item from 1Password, taking the fields of the
# section(s) named after the project. Requires one of:
#
#   OP_CONNECT_HOST + OP_CONNECT_TOKEN + OP_VAULT   (1Password Connect)
#   OP_SERVICE_ACCOUNT_TOKEN                        (service account)
#     (+ OP_VAULT to pin the vault when the token can see more than one)
#
# `cred_tool` is provided by tailucas-pylib, so `uv` must be able to resolve it.
# The result is written 0600 because it may contain credentials.
#
# This is a DEVELOPER path only. CI never generates `.env`: the host owns it, and
# the deploy job only checks that it exists.
set -eu
set -o pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DOT_ENV_FILE_PATH="${DOT_ENV_FILE_PATH:-.env}"

if [ "$#" -ge 1 ]; then
  PROJECT_NAME="$1"
else
  # fall back to PROJECT_NAME in base.env so this is callable with no arguments
  PROJECT_NAME="$(sed -n 's/^PROJECT_NAME="\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' "${HERE}/base.env")"
fi
test -n "${PROJECT_NAME}" || {
  echo "No project name: pass one as an argument or set PROJECT_NAME in base.env." >&2
  exit 1
}

echo "Generating ${DOT_ENV_FILE_PATH} for '${PROJECT_NAME}' from 1Password."
umask 077
uv run --frozen --no-sync cred_tool "ENV.${PROJECT_NAME}" "${PROJECT_NAME}" build \
  | jq -r '. | to_entries[] | [.key,.value] | @tsv' \
  | tr '\t' '=' \
  | sed 's/=\(.*\)/="\1"/' > "${DOT_ENV_FILE_PATH}"
chmod 600 "${DOT_ENV_FILE_PATH}"
test "$(wc -l < "${DOT_ENV_FILE_PATH}")" -ge 2
echo "Wrote $(wc -l < "${DOT_ENV_FILE_PATH}") settings to ${DOT_ENV_FILE_PATH}."
