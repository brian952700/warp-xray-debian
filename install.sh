#!/usr/bin/env bash
# Download a coherent, commit-pinned set of files before invoking Python.
set -Eeuo pipefail

color() {
    local code="$1" text="$2"
    if [[ -t 1 && -z "${NO_COLOR+x}" && "${TERM:-}" != dumb ]]; then
        printf '\033[%sm%s\033[0m' "$code" "$text"
    else
        printf '%s' "$text"
    fi
}

heading() {
    printf '\n  '
    color '1;36' "$1"
    printf '\n\n'
}

message() {
    printf '  '
    color "$1" "$2"
    printf '\n'
}

installer_failed() {
    local code="$1"
    printf '\n'
    message 31 '[失败] 当前安装步骤未完成，请查看上方错误。'
    exit "$code"
}

trap 'installer_failed "$?"' ERR

main() {
    heading 'WARP + Xray · 安装管理工具'
    message 90 'Debian 12 / 13+  |  官方 WARP  |  Xray Google 分流'
    if [[ ${EUID} -ne 0 ]]; then
        message 31 '[失败] 请通过 sudo bash install.sh 或 root 运行。'
        exit 1
    fi
    . /etc/os-release
    if [[ "${ID:-}" != debian || ! "${VERSION_ID:-}" =~ ^[0-9]+(\.[0-9]+)?$ || ${VERSION_ID%%.*} -lt 12 ]]; then
        message 31 '[失败] 仅适用于 Debian 12+，未来版本需官方软件源支持。'
        exit 1
    fi
    local action="${1:-setup}"
    if [[ "$action" != setup && "$action" != --download-only ]]; then
        echo '用法：bash install.sh [setup|--download-only]' >&2
        exit 1
    fi
    heading '01 · 检查基础环境'
    if ! command -v curl >/dev/null || ! command -v python3 >/dev/null || ! command -v sha256sum >/dev/null; then
        apt-get update
        apt-get install -y curl ca-certificates python3 coreutils
    fi
    message 32 '[成功] 基础运行环境已就绪。'
    local task_sha task_target task_base
    task_temp="$(mktemp -d)"
    trap 'rm -rf -- "${task_temp:?}"' EXIT
    # The repository and ref are fixed, not supplied by untrusted page content.
    heading '02 · 获取版本与下载文件'
    curl --fail --silent --show-error --location --retry 2 --connect-timeout 10 --max-time 60 \
        'https://api.github.com/repos/brian952700/warp-xray-debian/commits/main' \
        -o "$task_temp/commit.json"
    task_sha="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sha"])' "$task_temp/commit.json")"
    [[ "$task_sha" =~ ^[a-f0-9]{40}$ ]] || { message 31 '[失败] 无法解析 GitHub 提交编号。'; exit 1; }
    message 90 "版本提交：${task_sha:0:12}"
    task_base="https://raw.githubusercontent.com/brian952700/warp-xray-debian/$task_sha"
    local task_file
    for task_file in warp-xray.py warp-xray.sh SHA256SUMS; do
        message 36 "[下载] $task_file"
        curl --fail --silent --show-error --location --retry 2 --connect-timeout 10 --max-time 60 \
            "$task_base/$task_file" -o "$task_temp/$task_file"
    done
    # Only check the two known payloads; do not trust arbitrary paths in a manifest.
    heading '03 · 校验文件并安装命令'
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
    message 32 '[成功] SHA256 文件校验通过。'
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
    message 32 '[成功] warp-xray 管理工具已安装。'
    printf '\n'
    message 36 '管理菜单：sudo warp-xray'
    message 90 '其他命令：setup / status / prepare / apply / restore / uninstall'
    if [[ "$action" == --download-only ]]; then
        message 90 '本次仅更新管理工具，没有修改 Xray 或 WARP 配置。'
        printf '\n'
        return
    fi
    if [[ ! -r /dev/tty ]]; then
        message 33 '[注意] 没有交互终端。工具已下载，请稍后运行 sudo warp-xray。'
        return
    fi
    # Read interactive answers from the terminal even when this installer is piped.
    /usr/local/bin/warp-xray setup </dev/tty
}

main "$@"
