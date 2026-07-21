#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
VENV_PYTHON_UNIX="${REPO_ROOT}/.venv/bin/python"
VENV_PYTHON_WIN="${REPO_ROOT}/.venv/Scripts/python.exe"
WORKSPACE="${SHAMSU_WORKSPACE:-$(pwd)}"
export PYTHONUTF8="${PYTHONUTF8:-1}"

if [[ -x "${VENV_PYTHON_UNIX}" ]]; then
  VENV_PYTHON="${VENV_PYTHON_UNIX}"
elif [[ -x "${VENV_PYTHON_WIN}" ]]; then
  VENV_PYTHON="${VENV_PYTHON_WIN}"
else
  echo "Local .venv not found. Run scripts/install.sh from the SHAMSU repo first." >&2
  exit 1
fi

"${VENV_PYTHON}" -m shamsu.runtime.doctor --workspace "${WORKSPACE}" "$@"
