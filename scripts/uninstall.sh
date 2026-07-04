#!/usr/bin/env bash
set -euo pipefail

BIN_DIR="${HOME}/.local/bin"
KEEP_VENV=0
KEEP_LAUNCHER=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bin-dir)
      BIN_DIR="${2:-}"
      shift 2
      ;;
    --keep-venv)
      KEEP_VENV=1
      shift
      ;;
    --keep-launcher)
      KEEP_LAUNCHER=1
      shift
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${REPO_ROOT}/.venv"
RUNTIME_DIR="${REPO_ROOT}/.shamsu"
LAUNCHER="${BIN_DIR}/shamsu"

echo "SHAMSU uninstall"
echo "Repo: ${REPO_ROOT}"

if [[ "${KEEP_LAUNCHER}" -eq 0 && -e "${LAUNCHER}" ]]; then
  rm -f "${LAUNCHER}"
  echo "Removed launcher: ${LAUNCHER}"
elif [[ "${KEEP_LAUNCHER}" -eq 1 ]]; then
  echo "Keeping user-local launcher."
fi

if [[ "${KEEP_VENV}" -eq 0 && -d "${VENV_DIR}" ]]; then
  rm -rf "${VENV_DIR}"
  echo "Removed repo virtual environment: ${VENV_DIR}"
elif [[ "${KEEP_VENV}" -eq 1 ]]; then
  echo "Keeping repo virtual environment."
fi

if [[ -d "${RUNTIME_DIR}" ]]; then
  rm -rf "${RUNTIME_DIR}"
  echo "Removed repo runtime state: ${RUNTIME_DIR}"
fi

while IFS= read -r -d '' nested_dir; do
  rm -rf "${nested_dir}"
  echo "Removed stray nested workspace state: ${nested_dir}"
done < <(find "${REPO_ROOT}" -type d -name ".shamsu" -not -path "*/.venv/*" -not -path "*/.git/*" -print0 2>/dev/null)

echo
echo "SHAMSU uninstall complete."
echo "This removed SHAMSU-managed files from this repo and your user-local launcher directory."
echo "It did not remove Ollama or workspace .shamsu folders from your other projects."
