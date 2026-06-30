#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON:-python3}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${REPO_ROOT}/.venv"
VENV_PYTHON="${VENV_DIR}/bin/python"

echo "SHAMSU installer"
echo "Repo: ${REPO_ROOT}"
echo "Creating local virtual environment: ${VENV_DIR}"

cd "${REPO_ROOT}"

if [[ ! -x "${VENV_PYTHON}" ]]; then
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

"${VENV_PYTHON}" -m pip install --upgrade pip
"${VENV_PYTHON}" -m pip install -e ".[dev]"

echo
echo "Install complete."
echo "Run from any workspace with:"
echo "  ${REPO_ROOT}/scripts/run-shamsu.sh"
