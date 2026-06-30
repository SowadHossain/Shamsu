#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON:-python3}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${REPO_ROOT}/.venv"
VENV_PYTHON_UNIX="${VENV_DIR}/bin/python"
VENV_PYTHON_WIN="${VENV_DIR}/Scripts/python.exe"

find_venv_python() {
  if [[ -x "${VENV_PYTHON_UNIX}" ]]; then
    echo "${VENV_PYTHON_UNIX}"
  elif [[ -x "${VENV_PYTHON_WIN}" ]]; then
    echo "${VENV_PYTHON_WIN}"
  fi
}

echo "SHAMSU installer"
echo "Repo: ${REPO_ROOT}"
echo "Creating local virtual environment: ${VENV_DIR}"

cd "${REPO_ROOT}"

if [[ -z "$(find_venv_python)" ]]; then
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

VENV_PYTHON="$(find_venv_python)"
if [[ -z "${VENV_PYTHON}" ]]; then
  echo "Could not find venv Python after creating ${VENV_DIR}" >&2
  exit 1
fi

"${VENV_PYTHON}" -m pip install --upgrade pip
"${VENV_PYTHON}" -m pip install -e ".[dev]"

echo
echo "Install complete."
echo "Run from any workspace with:"
echo "  ${REPO_ROOT}/scripts/run-shamsu.sh"
