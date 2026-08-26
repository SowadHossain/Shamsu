#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
VENV_PYTHON_UNIX="${REPO_ROOT}/.venv/bin/python"
VENV_PYTHON_WIN="${REPO_ROOT}/.venv/Scripts/python.exe"
WORKSPACE="${SHAMSU_WORKSPACE:-$(pwd)}"
export PYTHONUTF8="${PYTHONUTF8:-1}"

USES_WINDOWS_PYTHON=0
if [[ -x "${VENV_PYTHON_UNIX}" ]]; then
  VENV_PYTHON="${VENV_PYTHON_UNIX}"
elif [[ -x "${VENV_PYTHON_WIN}" ]]; then
  VENV_PYTHON="${VENV_PYTHON_WIN}"
  USES_WINDOWS_PYTHON=1
else
  VENV_PYTHON=""
fi

# `python` EXISTING is not the same as the environment working. Live 2026-08-20
# `.venv/pyvenv.cfg` was gone - removed with a whole alphabetical block of
# site-packages, the signature of an antivirus quarantine - while the
# interpreter was still on disk. The old check passed, the launcher ran it
# anyway, and the user got `failed to locate pyvenv.cfg` with no idea what to
# do. The one question worth asking is whether the environment can import
# SHAMSU, so ask that.
VENV_BROKEN=""
if [[ -z "${VENV_PYTHON}" ]]; then
  VENV_BROKEN="there is no .venv in ${REPO_ROOT}"
elif [[ ! -f "${REPO_ROOT}/.venv/pyvenv.cfg" ]]; then
  VENV_BROKEN="the .venv is missing pyvenv.cfg, so it is no longer a usable environment"
elif ! "${VENV_PYTHON}" -c "import shamsu" >/dev/null 2>&1; then
  VENV_BROKEN="the .venv cannot import SHAMSU"
fi

if [[ -n "${VENV_BROKEN}" ]]; then
  {
    echo "SHAMSU cannot start: ${VENV_BROKEN}."
    echo
    echo "Repair it by running the installer again from the repo:"
    echo "  ${REPO_ROOT}/scripts/install.sh"
    echo
    echo "It will rebuild the environment in place. If this keeps happening, check"
    echo "whether antivirus is quarantining files under ${REPO_ROOT}/.venv."
  } >&2
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

"${VENV_PYTHON}" -m shamsu.runtime.ollama status
"${VENV_PYTHON}" -m shamsu.cli.app --workspace "${WORKSPACE}" "$@"
