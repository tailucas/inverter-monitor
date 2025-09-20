#!/usr/bin/env bash
set -eu
set -o pipefail

PROJECT_NAME="${1}"
DOT_ENV_FILE_PATH="${DOT_ENV_FILE_PATH:-.env}"

if [ -z "${GITHUB_ENV:-}" ]; then
  echo "Generating "${DOT_ENV_FILE_PATH}" from pylib cred_tool."
  uv run cred_tool "ENV.${PROJECT_NAME}" "${PROJECT_NAME}" build | jq -r '. | to_entries[] | [.key,.value] | @tsv' | tr '\t' '=' | sed 's/=\(.*\)/="\1"/' > "${DOT_ENV_FILE_PATH}"
else
  echo "Generating "${DOT_ENV_FILE_PATH}" from Taskfile application definition due to CI ${GITHUB_ENV:-}..."
  uv run python -c "import yaml; data=yaml.safe_load(open('app.yml'))['vars']; [print(f'{k}={v}') for k,v in data.items()]" > "${DOT_ENV_FILE_PATH}"
fi
test $(wc -l < "${DOT_ENV_FILE_PATH}") -ge 2
