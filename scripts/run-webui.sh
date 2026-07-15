#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
WEB_ROOT="${REPO_ROOT}/webui"
PORT="${1:-5174}"

if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  PYTHON="${REPO_ROOT}/.venv/bin/python"
elif [[ -x "${REPO_ROOT}/.venv/Scripts/python.exe" ]]; then
  PYTHON="${REPO_ROOT}/.venv/Scripts/python.exe"
else
  PYTHON="python3"
fi

echo "Starting SHAMSU Web UI at http://localhost:${PORT}"
echo "Press Ctrl+C to stop."
cd "${WEB_ROOT}"
"${PYTHON}" -m http.server "${PORT}" --bind 127.0.0.1
