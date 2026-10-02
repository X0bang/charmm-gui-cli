#!/usr/bin/env bash
set -euo pipefail

CGUI_SOURCE_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CGUI_PYTHON="${CHARMM_GUI_PYTHON:-python3}"
if [[ "${1:-}" == "--python" ]]; then
  if [[ $# -lt 2 ]]; then
    printf '%s\n' 'Error: --python requires a Python executable.' >&2
    exit 2
  fi
  CGUI_PYTHON="$2"
  shift 2
fi
if ! command -v "$CGUI_PYTHON" >/dev/null 2>&1; then
  printf '%s\n' 'Python 3.10+ was not found. Ubuntu 22.04+: ask your administrator to install python3 and python3-venv.' >&2
  exit 2
fi
if ! "$CGUI_PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
  printf '%s\n' 'Python 3.10+ is required. Select it with CHARMM_GUI_PYTHON or a leading --python PATH.' >&2
  exit 2
fi
exec "$CGUI_PYTHON" "$CGUI_SOURCE_DIR/scripts/install_user.py" --source "$CGUI_SOURCE_DIR" "$@"
