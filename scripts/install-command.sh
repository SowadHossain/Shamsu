#!/usr/bin/env bash
set -euo pipefail

BIN_DIR="${HOME}/.local/bin"
FORCE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bin-dir)
      BIN_DIR="${2:-}"
      shift 2
      ;;
    --force|-f)
      FORCE=1
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
RUN_SCRIPT="${REPO_ROOT}/scripts/run-shamsu.sh"

if [[ ! -f "${RUN_SCRIPT}" ]]; then
  echo "Could not find SHAMSU run script: ${RUN_SCRIPT}" >&2
  exit 1
fi

mkdir -p "${BIN_DIR}"
LAUNCHER="${BIN_DIR}/shamsu"

if [[ -e "${LAUNCHER}" && "${FORCE}" -ne 1 ]]; then
  echo "Launcher already exists: ${LAUNCHER}. Re-run with --force to overwrite." >&2
  exit 1
fi

cat > "${LAUNCHER}" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec "${RUN_SCRIPT}" "\$@"
EOF
chmod +x "${LAUNCHER}"

echo "Installed SHAMSU launcher:"
echo "  ${LAUNCHER}"
echo
echo "SHAMSU did not edit your shell profile, PATH, global Python, or system registry."

case ":${PATH}:" in
  *":${BIN_DIR}:"*)
    echo
    echo "Run SHAMSU from any project with:"
    echo "  shamsu"
    ;;
  *)
    echo
    echo "This bin directory is not on PATH for the current shell."
    echo "Run directly with:"
    echo "  ${LAUNCHER}"
    echo
    echo "Or add this directory to PATH yourself if you want plain 'shamsu':"
    echo "  ${BIN_DIR}"
    ;;
esac
