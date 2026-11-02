#!/usr/bin/env bash
# Keep this launcher and warp-xray.py in the same directory.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
    echo '请先安装 Python 3：sudo apt-get update && sudo apt-get install -y python3' >&2
    exit 1
fi
exec python3 "$SCRIPT_DIR/warp-xray.py" "$@"
