#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
VENV_PYTHON_UNIX="${REPO_ROOT}/.venv/bin/python"
VENV_PYTHON_WIN="${REPO_ROOT}/.venv/Scripts/python.exe"
WORKSPACE="${SHAMSU_WORKSPACE:-$(pwd)}"

USES_WINDOWS_PYTHON=0
if [[ -x "${VENV_PYTHON_UNIX}" ]]; then
  VENV_PYTHON="${VENV_PYTHON_UNIX}"
elif [[ -x "${VENV_PYTHON_WIN}" ]]; then
  VENV_PYTHON="${VENV_PYTHON_WIN}"
  USES_WINDOWS_PYTHON=1
else
  echo "Local .venv not found. Run scripts/install.sh from the SHAMSU repo first." >&2
  exit 1
fi

if [[ "${USES_WINDOWS_PYTHON}" -eq 1 ]] && command -v cygpath >/dev/null 2>&1; then
  WORKSPACE="$(cygpath -w "${WORKSPACE}")"
elif [[ "${USES_WINDOWS_PYTHON}" -eq 1 && "${WORKSPACE}" =~ ^/mnt/([A-Za-z])/(.*)$ ]]; then
  DRIVE="${BASH_REMATCH[1]^^}"
  REST="${BASH_REMATCH[2]//\//\\}"
  WORKSPACE="${DRIVE}:\\${REST}"
elif [[ "${USES_WINDOWS_PYTHON}" -eq 1 && "${WORKSPACE}" =~ ^/([A-Za-z])/(.*)$ ]]; then
  DRIVE="${BASH_REMATCH[1]^^}"
  REST="${BASH_REMATCH[2]//\//\\}"
  WORKSPACE="${DRIVE}:\\${REST}"
fi

"${VENV_PYTHON}" -m shamsu.cli.repl --workspace "${WORKSPACE}" "$@"
