#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON:-python3}"
YES=0
SKIP_OLLAMA_INSTALL=0
SKIP_MODELS=0
MODELS_PATH=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes|-y)
      YES=1
      shift
      ;;
    --skip-ollama-install)
      SKIP_OLLAMA_INSTALL=1
      shift
      ;;
    --skip-models)
      SKIP_MODELS=1
      shift
      ;;
    --models-path)
      MODELS_PATH="${2:-}"
      shift 2
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

if [[ -n "${MODELS_PATH}" ]]; then
  export OLLAMA_MODELS="${MODELS_PATH}"
  echo "Using Ollama model directory for this install run: ${MODELS_PATH}"
fi

if ! "${VENV_PYTHON}" -m shamsu.runtime.ollama status --json | grep -q '"ollama_path": "";'; then
  :
elif [[ "${SKIP_OLLAMA_INSTALL}" -eq 0 ]]; then
  INSTALL_OLLAMA="${YES}"
  if [[ "${INSTALL_OLLAMA}" -eq 0 ]]; then
    read -r -p "Ollama is required for local inference. Install Ollama now? [y/N] " ANSWER
    if [[ "${ANSWER,,}" == "y" || "${ANSWER,,}" == "yes" ]]; then
      INSTALL_OLLAMA=1
    fi
  fi
  if [[ "${INSTALL_OLLAMA}" -eq 1 ]]; then
    echo "Installing Ollama through the official platform flow."
    echo "SHAMSU will not edit PATH or shell startup files."
    if [[ "$(uname -s)" == "Linux" ]]; then
      curl -fsSL https://ollama.com/install.sh | sh
    elif command -v brew >/dev/null 2>&1; then
      brew install ollama
    else
      echo "Install Ollama from https://ollama.com/download, then rerun this script." >&2
    fi
  fi
fi

if [[ "${SKIP_MODELS}" -eq 0 ]] && ! "${VENV_PYTHON}" -m shamsu.runtime.ollama status --json | grep -q '"ollama_path": "";'; then
  "${VENV_PYTHON}" -m shamsu.runtime.ollama repair
elif "${VENV_PYTHON}" -m shamsu.runtime.ollama status --json | grep -q '"ollama_path": "";'; then
  echo "Ollama is still missing. SHAMSU installed, but local inference needs 'models repair' after Ollama is installed." >&2
fi

"${VENV_PYTHON}" -m shamsu.runtime.ollama write-config

echo
echo "Install complete."
echo "SHAMSU did not edit your shell profile, PATH, global Python, or system registry."
echo "Run from any workspace with:"
echo "  ${REPO_ROOT}/scripts/run-shamsu.sh"
