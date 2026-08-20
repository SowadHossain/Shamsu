#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON:-python3}"
YES=0
SKIP_OLLAMA_INSTALL=0
SKIP_MODELS=0
PREFETCH_MODELS=0
SKIP_COMMAND_INSTALL=0
SKIP_CODEBASE_MEMORY_INSTALL=0
SKIP_GRAPHITI_INSTALL=0
BIN_DIR="${HOME}/.local/bin"
MODELS_PATH=""
export PYTHONUTF8="${PYTHONUTF8:-1}"

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
    --prefetch-models)
      PREFETCH_MODELS=1
      shift
      ;;
    --skip-command-install)
      SKIP_COMMAND_INSTALL=1
      shift
      ;;
    --skip-codebase-memory-install)
      SKIP_CODEBASE_MEMORY_INSTALL=1
      shift
      ;;
    --skip-graphiti-install)
      SKIP_GRAPHITI_INSTALL=1
      shift
      ;;
    --bin-dir)
      BIN_DIR="${2:-}"
      shift 2
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
RUN_SCRIPT="${REPO_ROOT}/scripts/run-shamsu.sh"

find_venv_python() {
  if [[ -x "${VENV_PYTHON_UNIX}" ]]; then
    echo "${VENV_PYTHON_UNIX}"
  elif [[ -x "${VENV_PYTHON_WIN}" ]]; then
    echo "${VENV_PYTHON_WIN}"
  fi
}

# Can this environment actually run SHAMSU? Echoes the reason it cannot, or "".
#
# An interpreter EXISTING is not the same as the environment working. Live
# 2026-08-20 `pyvenv.cfg` was gone - removed along with a whole alphabetical
# block of site-packages, the signature of an antivirus quarantine - while the
# interpreter was still on disk. Everything downstream then failed with
# `failed to locate pyvenv.cfg`, and the installer reused the corpse because it
# only ever checked for the file.
venv_problem() {
  local exe
  exe="$(find_venv_python)"
  if [[ -z "${exe}" ]]; then
    echo "no interpreter in ${VENV_DIR}"
    return
  fi
  if [[ ! -f "${VENV_DIR}/pyvenv.cfg" ]]; then
    echo "pyvenv.cfg is missing, so this is no longer a virtual environment"
    return
  fi
  if ! "${exe}" -c "import sys" >/dev/null 2>&1; then
    echo "the interpreter will not start"
    return
  fi
  echo ""
}

# Say what is missing BEFORE spending ten minutes installing, and say what each
# missing thing costs. Only Python is fatal: a machine without node can still
# edit Python, and reporting a failed install over a JavaScript syntax checker
# would be false.
check_prerequisites() {
  echo
  echo "Checking prerequisites..."
  local version major minor
  if ! version="$("${PYTHON_BIN}" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"; then
    echo "Could not run '${PYTHON_BIN}'. Install Python 3.11 or newer, or set PYTHON=<path>." >&2
    exit 1
  fi
  major="${version%%.*}"
  minor="${version##*.}"
  if [[ "${major}" -lt 3 ]] || { [[ "${major}" -eq 3 ]] && [[ "${minor}" -lt 11 ]]; }; then
    echo "SHAMSU needs Python 3.11 or newer; '${PYTHON_BIN}' is ${version}." >&2
    exit 1
  fi
  echo "  python ${version} - ok"

  local tool why url
  while IFS='|' read -r tool why url; do
    if command -v "${tool}" >/dev/null 2>&1; then
      echo "  ${tool} - ok"
    else
      echo "  ${tool} is not installed - ${why}." >&2
      echo "    Install it from ${url}" >&2
    fi
  done <<'TOOLS'
node|JavaScript syntax checking falls back to a bracket scan|https://nodejs.org/en/download
git|diff review and history tools are unavailable|https://git-scm.com/downloads
TOOLS
  echo
}

echo "SHAMSU installer"
echo "Repo: ${REPO_ROOT}"

check_prerequisites

cd "${REPO_ROOT}"

VENV_PROBLEM="$(venv_problem)"
if [[ -n "${VENV_PROBLEM}" && -d "${VENV_DIR}" ]]; then
  echo "Warning: the existing .venv is unusable: ${VENV_PROBLEM}." >&2
  echo "Rebuilding it from scratch."
  # A half-deleted environment cannot be repaired in place: pip would reinstall
  # the packages it can still see records for and skip the ones whose metadata
  # went with them.
  rm -rf "${VENV_DIR}"
fi

if [[ -z "$(find_venv_python)" ]]; then
  echo "Creating local virtual environment: ${VENV_DIR}"
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
else
  echo "Using existing virtual environment: ${VENV_DIR}"
fi

VENV_PYTHON="$(find_venv_python)"
if [[ -z "${VENV_PYTHON}" ]]; then
  echo "Could not find venv Python after creating ${VENV_DIR}" >&2
  exit 1
fi

# A venv can lose pip on its own, and `ensurepip` is the supported repair. Costs
# nothing when pip is already there; without it the install dies at its first
# real step with `No module named pip`, which says nothing about what to do.
if ! "${VENV_PYTHON}" -m pip --version >/dev/null 2>&1; then
  echo "pip is missing from the environment; restoring it with ensurepip."
  "${VENV_PYTHON}" -m ensurepip --upgrade >/dev/null 2>&1 || true
fi
if ! "${VENV_PYTHON}" -m pip --version >/dev/null 2>&1; then
  echo "pip is missing from ${VENV_DIR} and ensurepip could not restore it." >&2
  exit 1
fi

"${VENV_PYTHON}" -m pip install --upgrade pip
"${VENV_PYTHON}" -m pip install -e ".[dev]"

# Prove it. An installer that reports success without ever importing what it
# installed is how a half-quarantined environment kept passing for working.
if ! "${VENV_PYTHON}" -c "import shamsu" >/dev/null 2>&1; then
  echo "SHAMSU installed but cannot be imported from ${VENV_DIR}." >&2
  echo "Check whether antivirus is quarantining files under it." >&2
  exit 1
fi
echo "SHAMSU imports cleanly from the virtual environment."

PLAYWRIGHT_MARKER="${VENV_DIR}/.shamsu-playwright-chromium-ok"
if [[ -f "${PLAYWRIGHT_MARKER}" ]]; then
  echo "Playwright Chromium already installed (skipping browser download check)."
else
  if "${VENV_PYTHON}" -m playwright install chromium; then
    touch "${PLAYWRIGHT_MARKER}"
  else
    echo "Warning: Playwright Chromium install failed or was skipped." >&2
    echo "Warning: Browser-based debugging (/browse commands) may not work until this succeeds. Rerun this script to retry." >&2
  fi
fi

# Where Ollama was found, or "" when it is missing.
#
# This used to be `grep -q '"ollama_path": "";'` - with a stray semicolon that
# no JSON can ever contain, so the pattern NEVER matched. Every test built on it
# read the wrong way round: the "install Ollama now?" prompt below was
# unreachable, the "Ollama is still missing" warning never printed, and
# --prefetch-models ran `models repair` against an Ollama that was not there.
# install.ps1 always parsed the JSON properly; this is the same check.
#
# Silent on failure by design: `status --json` needs the package to have
# imported cleanly, and a partly-installed venv must produce "I could not tell"
# rather than a traceback that ends the install.
ollama_path() {
  "${VENV_PYTHON}" -m shamsu.runtime.ollama status --json 2>/dev/null \
    | "${VENV_PYTHON}" -c 'import json, sys
try:
    print(json.load(sys.stdin).get("ollama_path") or "")
except Exception:
    print("")' 2>/dev/null
}

have_ollama() {
  [[ -n "$(ollama_path)" ]]
}

if [[ -n "${MODELS_PATH}" ]]; then
  export OLLAMA_MODELS="${MODELS_PATH}"
  echo "Using Ollama model directory for this install run: ${MODELS_PATH}"
fi

if have_ollama; then
  echo "Ollama found: $(ollama_path)"
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
      if ! curl -fsSL https://ollama.com/install.sh | sh; then
        echo "Warning: Ollama install script failed. Install manually from https://ollama.com/download, then run 'models repair'." >&2
      fi
    elif command -v brew >/dev/null 2>&1; then
      if ! brew install ollama; then
        echo "Warning: 'brew install ollama' failed. Install manually from https://ollama.com/download, then run 'models repair'." >&2
      fi
    else
      echo "Install Ollama from https://ollama.com/download, then rerun this script." >&2
    fi
  fi
fi

if [[ "${PREFETCH_MODELS}" -eq 1 && "${SKIP_MODELS}" -eq 0 ]] && have_ollama; then
  echo "Checking and pulling all required local models now. This can take a long time."
  "${VENV_PYTHON}" -m shamsu.runtime.ollama repair || \
    echo "Warning: model prefetch failed. Run 'shamsu' and it will pull what it needs." >&2
elif ! have_ollama; then
  echo "Ollama is still missing. SHAMSU installed, but local inference needs 'models repair' after Ollama is installed." >&2
else
  echo "Skipping model downloads here. The first time you run 'shamsu' in a workspace it will"
  echo "ask which model tier to use (light/default/heavy) and download that tier's models then."
  echo "Pass --prefetch-models to this script to download the default tier's models now instead."
fi

# `set -e` is on, so a non-zero exit here would end the install AFTER the
# package is in place but BEFORE the launcher is written - leaving a half
# install that reports failure. The config is a convenience; the install is not.
"${VENV_PYTHON}" -m shamsu.runtime.ollama write-config || \
  echo "Warning: could not write the Ollama config. 'shamsu doctor' can retry." >&2

if [[ "${SKIP_CODEBASE_MEMORY_INSTALL}" -eq 0 ]] && ! "${VENV_PYTHON}" -m shamsu.abstract.cli status --workspace "${REPO_ROOT}" --json | grep -q '"available": true'; then
  INSTALL_CBM="${YES}"
  if [[ "${INSTALL_CBM}" -eq 0 ]]; then
    read -r -p "Install required local Codebase-Memory MCP tool? [y/N] " ANSWER
    if [[ "${ANSWER,,}" == "y" || "${ANSWER,,}" == "yes" ]]; then
      INSTALL_CBM=1
    fi
  fi
  if [[ "${INSTALL_CBM}" -eq 1 ]]; then
    if ! "${VENV_PYTHON}" -m shamsu.abstract.cli setup --workspace "${REPO_ROOT}"; then
      echo "Warning: Codebase-Memory MCP setup failed. Run '/abstract setup' or 'shamsu doctor' later to retry." >&2
    fi
  else
    echo "Warning: Skipping Codebase-Memory MCP install. SHAMSU codebase mode will not run normal code-agent workflows until '/abstract setup' completes." >&2
  fi
fi

if [[ "${SKIP_GRAPHITI_INSTALL}" -eq 0 ]] && ! "${VENV_PYTHON}" -m shamsu.memory.cli status --workspace "${REPO_ROOT}" --json | grep -q '"available": true'; then
  INSTALL_GRAPHITI="${YES}"
  if [[ "${INSTALL_GRAPHITI}" -eq 0 ]]; then
    read -r -p "Install required local Graphiti memory tool? [y/N] " ANSWER
    if [[ "${ANSWER,,}" == "y" || "${ANSWER,,}" == "yes" ]]; then
      INSTALL_GRAPHITI=1
    fi
  fi
  if [[ "${INSTALL_GRAPHITI}" -eq 1 ]]; then
    if ! "${VENV_PYTHON}" -m shamsu.memory.cli setup --workspace "${REPO_ROOT}"; then
      echo "Warning: Graphiti setup failed. Run '/memory setup' or 'shamsu doctor' later to retry." >&2
    fi
  else
    echo "Warning: Skipping Graphiti install. SHAMSU normal agent mode will not run until '/memory setup' completes." >&2
  fi
fi

if [[ "${SKIP_COMMAND_INSTALL}" -eq 0 ]]; then
  mkdir -p "${BIN_DIR}"
  LAUNCHER="${BIN_DIR}/shamsu"
  LAUNCHER_ON_PATH=0
  cat > "${LAUNCHER}" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec "${RUN_SCRIPT}" "\$@"
EOF
  chmod +x "${LAUNCHER}"
  echo "Installed SHAMSU launcher:"
  echo "  ${LAUNCHER}"
  case ":${PATH}:" in
    *":${BIN_DIR}:"*) LAUNCHER_ON_PATH=1 ;;
    *)
      echo
      echo "Launcher directory is not on PATH for the current shell."
      echo "Run directly with:"
      echo "  ${LAUNCHER}"
      echo
      echo "Or add this directory to PATH yourself if you want plain 'shamsu':"
      echo "  ${BIN_DIR}"
      ;;
  esac
  if [[ "${LAUNCHER_ON_PATH}" -eq 1 ]] && command -v shamsu >/dev/null 2>&1; then
    RESOLVED_SHAMSU="$(command -v shamsu)"
    if [[ "${RESOLVED_SHAMSU}" != "${LAUNCHER}" ]]; then
      echo
      echo "Warning: plain 'shamsu' currently resolves to a different command:"
      echo "  ${RESOLVED_SHAMSU}"
      echo "Run this launcher directly, or move ${BIN_DIR} earlier in PATH:"
      echo "  ${LAUNCHER}"
    fi
  fi
fi

echo
echo "Install complete."
echo "SHAMSU did not edit your shell profile, PATH, global Python, or system registry."
echo "Run from any workspace with:"
if [[ "${SKIP_COMMAND_INSTALL}" -eq 0 ]]; then
  if [[ "${LAUNCHER_ON_PATH:-0}" -eq 1 ]]; then
    echo "  shamsu"
  else
    echo "  ${LAUNCHER}"
    echo "Add ${BIN_DIR} to PATH if you want plain 'shamsu' in new terminals."
  fi
else
  echo "  ${REPO_ROOT}/scripts/run-shamsu.sh"
fi


