#!/usr/bin/env bash
# Download a coherent, commit-pinned set of files before invoking Python.
set -euo pipefail

main() {
    if [[ ${EUID} -ne 0 ]]; then
        echo '请通过 sudo bash install.sh 或 root 运行。' >&2
        exit 1
    fi
    . /etc/os-release
    if [[ "${ID:-}" != debian || ! "${VERSION_ID:-}" =~ ^[0-9]+(\.[0-9]+)?$ || ${VERSION_ID%%.*} -lt 12 ]]; then
        echo '仅适用于 Debian 12+。未来版本需要 Cloudflare 官方源支持。' >&2
        exit 1
    fi
    local action="${1:-setup}"
    if [[ "$action" != setup && "$action" != --download-only ]]; then
        echo '用法：bash install.sh [setup|--download-only]' >&2
        exit 1
    fi
    if ! command -v curl >/dev/null || ! command -v python3 >/dev/null || ! command -v sha256sum >/dev/null; then
        apt-get update
        apt-get install -y curl ca-certificates python3 coreutils
    fi
    local task_sha task_target task_base
    task_temp="$(mktemp -d)"
    trap 'rm -rf -- "${task_temp:?}"' EXIT
    # The repository and ref are fixed, not supplied by untrusted page content.
    curl --fail --silent --show-error --location --retry 2 --connect-timeout 10 --max-time 60 \
        'https://api.github.com/repos/brian952700/warp-xray-debian/commits/main' \
        -o "$task_temp/commit.json"
    task_sha="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sha"])' "$task_temp/commit.json")"
    [[ "$task_sha" =~ ^[a-f0-9]{40}$ ]] || { echo '无法解析 GitHub 提交编号。' >&2; exit 1; }
    task_base="https://raw.githubusercontent.com/brian952700/warp-xray-debian/$task_sha"
    local task_file
    for task_file in warp-xray.py warp-xray.sh SHA256SUMS; do
        curl --fail --silent --show-error --location --retry 2 --connect-timeout 10 --max-time 60 \
            "$task_base/$task_file" -o "$task_temp/$task_file"
    done
    # Only check the two known payloads; do not trust arbitrary paths in a manifest.
    python3 - "$task_temp/SHA256SUMS" "$task_temp" <<'PY'
import hashlib, pathlib, sys
manifest, directory = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
entries = {}
for line in manifest.read_text().splitlines():
    checksum, name = line.split(None, 1)
    name = name.lstrip('* ')
    if name in entries:
        raise SystemExit('duplicate manifest entry')
    entries[name] = checksum
for name in ('warp-xray.py', 'warp-xray.sh'):
    if entries.get(name) != hashlib.sha256((directory / name).read_bytes()).hexdigest():
        raise SystemExit('checksum mismatch: ' + name)
PY
    task_target="/usr/local/lib/warp-xray/releases/$task_sha"
    install -d -m 700 "$task_target"
    install -m 600 "$task_temp/warp-xray.py" "$task_target/warp-xray.py"
    install -m 700 "$task_temp/warp-xray.sh" "$task_target/warp-xray.sh"
    # Write to a temporary file beside the wrapper, then atomically replace it.
    local task_wrapper
    task_wrapper="$(mktemp /usr/local/bin/.warp-xray.XXXXXX)"
    printf '#!/usr/bin/env bash\nexec /usr/bin/python3 "%s/warp-xray.py" "$@"\n' "$task_target" > "$task_wrapper"
    chmod 700 "$task_wrapper"
    mv -f -- "$task_wrapper" /usr/local/bin/warp-xray
    echo "已安装 warp-xray，提交：$task_sha"
    echo '命令：warp-xray setup / status / prepare / apply / restore / uninstall'
    if [[ "$action" == --download-only ]]; then
        return
    fi
    if [[ ! -r /dev/tty ]]; then
        echo '没有交互终端；已下载工具。请稍后运行 sudo warp-xray setup。'
        return
    fi
    # Read interactive answers from the terminal even when this installer is piped.
    /usr/local/bin/warp-xray setup </dev/tty
}

main "$@"
