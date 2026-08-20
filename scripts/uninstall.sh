#!/usr/bin/env bash
# NOT `set -e`. An uninstaller that stops at the first problem leaves whatever
# it had not reached yet installed, which is the worst of both outcomes: the
# user is told it failed and cannot tell what remains. Every step runs, every
# failure is reported, and the exit code tells the truth at the end.
#
# `-u` and `pipefail` stay: an unset variable here would mean an `rm -rf` with
# an empty path, which is the one failure mode an uninstaller must never have.
set -uo pipefail

BIN_DIR="${HOME}/.local/bin"
KEEP_VENV=0
KEEP_LAUNCHER=0
FAILURES=()

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

# Remove one path, describing it, and record rather than raise a failure.
# Refuses an empty target outright - see the note on `set -u` above.
remove_safely() {
  local target="$1" describe="$2"
  if [[ -z "${target}" ]]; then
    FAILURES+=("${describe}: no path was resolved")
    echo "Warning: refusing to remove ${describe} - no path was resolved." >&2
    return
  fi
  [[ -e "${target}" ]] || return 0
  if rm -rf "${target}"; then
    echo "Removed ${describe}: ${target}"
  else
    FAILURES+=("${describe}: ${target}")
    echo "Warning: could not remove ${describe}: ${target}" >&2
  fi
}

echo "SHAMSU uninstall"
echo "Repo: ${REPO_ROOT}"

if [[ "${KEEP_LAUNCHER}" -eq 0 ]]; then
  remove_safely "${LAUNCHER}" "launcher"
else
  echo "Keeping user-local launcher."
fi

if [[ "${KEEP_VENV}" -eq 0 ]]; then
  remove_safely "${VENV_DIR}" "repo virtual environment"
else
  echo "Keeping repo virtual environment."
fi

remove_safely "${RUNTIME_DIR}" "repo runtime state"

# Workspace state left inside the repo by test runs and nested projects.
# `.venv` and `.git` are excluded so this can never reach into a dependency's
# own files or into git's object store.
while IFS= read -r -d '' nested_dir; do
  remove_safely "${nested_dir}" "stray nested workspace state"
done < <(find "${REPO_ROOT}" -type d -name ".shamsu" -not -path "*/.venv/*" -not -path "*/.git/*" -print0 2>/dev/null)

echo
if [[ "${#FAILURES[@]}" -gt 0 ]]; then
  echo "SHAMSU uninstall finished with ${#FAILURES[@]} problem(s):" >&2
  for failure in "${FAILURES[@]}"; do
    echo "  - ${failure}" >&2
  done
  echo "Everything else was removed. Re-run this script after dealing with the above." >&2
  exit 1
fi

echo "SHAMSU uninstall complete."
echo "This removed SHAMSU-managed files from this repo and your user-local launcher directory."
echo "It did not remove Ollama or workspace .shamsu folders from your other projects."
exit 0
