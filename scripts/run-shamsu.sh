#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
VENV_PYTHON="${REPO_ROOT}/.venv/bin/python"
WORKSPACE="${SHAMSU_WORKSPACE:-$(pwd)}"

if [[ ! -x "${VENV_PYTHON}" ]]; then
  echo "Local .venv not found. Run scripts/install.sh from the SHAMSU repo first." >&2
  exit 1
fi

"${VENV_PYTHON}" -m shamsu.cli.repl --workspace "${WORKSPACE}" "$@"
