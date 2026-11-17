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

task_temp=''
task_stage=''
task_wrapper=''

cleanup_installer() {
    if [[ -n "$task_wrapper" ]]; then rm -f -- "$task_wrapper"; fi
    if [[ -n "$task_stage" ]]; then rm -rf -- "$task_stage"; fi
    if [[ -n "$task_temp" ]]; then rm -rf -- "$task_temp"; fi
}

trap cleanup_installer EXIT

download() {
    curl --disable --fail --silent --show-error --location --retry 2 \
        --connect-timeout 10 --max-time 60 "$1" -o "$2"
}

verify_payload() {
    # Only check the two known payloads; do not trust arbitrary manifest paths.
    /usr/bin/python3 - "$1" "$2" <<'PY'
import hashlib
from pathlib import Path
import sys

manifest, directory = map(Path, sys.argv[1:])
try:
    entries = {}
    for line in manifest.read_text().splitlines():
        checksum, name = line.split(None, 1)
        name = name.lstrip('* ')
        if name in entries:
            raise ValueError(f'duplicate manifest entry: {name}')
        entries[name] = checksum
    for name in ('warp-xray.py', 'warp-xray.sh'):
        path = directory / name
        if path.is_symlink() or entries.get(name) != hashlib.sha256(path.read_bytes()).hexdigest():
            raise ValueError(f'checksum mismatch: {name}')
except (OSError, ValueError) as exc:
    raise SystemExit(str(exc))
PY
}

download_payload() {
    local base="$1" file
    for file in warp-xray.py warp-xray.sh; do
        message 36 "[下载] $file"
        download "$base/$file" "$task_temp/$file"
    done
    verify_payload "$task_temp/SHA256SUMS" "$task_temp"
}

publish_release() {
    local target="$1" base="$2" admin_fd
    exec {admin_fd}>>/run/warp-xray-admin.lock
    if ! flock -n "$admin_fd"; then
        message 31 '[失败] 另一个 warp-xray 操作正在修改配置，请稍后重试。' >&2
        return 1
    fi

    if [[ -L "$target" ]]; then
        message 31 '[失败] 安装目录指向符号链接，请检查版本目录。' >&2
        return 1
    fi
    if verify_payload "$task_temp/SHA256SUMS" "$target" 2>/dev/null; then
        message 32 '[成功] 已校验并复用本机版本。'
    else
        # Another operation may have removed a cached version before this lock.
        if [[ ! -f "$task_temp/warp-xray.py" ]]; then
            download_payload "$base"
        fi
        install -d -m 700 "${target%/*}"
        task_stage="$(mktemp -d "${target%/*}/.stage.XXXXXX")"
        install -m 600 "$task_temp/warp-xray.py" "$task_stage/warp-xray.py"
        install -m 700 "$task_temp/warp-xray.sh" "$task_stage/warp-xray.sh"
        verify_payload "$task_temp/SHA256SUMS" "$task_stage"
        if [[ -d "$target" ]]; then
            # When repairing a partial legacy installation, keep each live path
            # readable until its verified replacement is ready.
            mv -fT -- "$task_stage/warp-xray.py" "$target/warp-xray.py"
            mv -fT -- "$task_stage/warp-xray.sh" "$target/warp-xray.sh"
        else
            mv -T -- "$task_stage" "$target"
            task_stage=''
        fi
        message 32 '[成功] SHA256 文件校验通过。'
    fi

    task_wrapper="$(mktemp /usr/local/bin/.warp-xray.XXXXXX)"
    printf '#!/usr/bin/env bash\nexec /usr/bin/python3 "%s/warp-xray.py" "$@"\n' "$target" > "$task_wrapper"
    chmod 700 "$task_wrapper"
    mv -fT -- "$task_wrapper" /usr/local/bin/warp-xray
    task_wrapper=''
    exec {admin_fd}>&-
}

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
    if ! command -v curl >/dev/null || [[ ! -x /usr/bin/python3 ]] || ! command -v flock >/dev/null; then
        apt-get update
        apt-get -o DPkg::Lock::Timeout=300 install -y curl ca-certificates python3 util-linux
    fi
    message 32 '[成功] 基础运行环境已就绪。'
    local task_sha task_target task_base tty_fd
    task_temp="$(mktemp -d)"
    # The repository and ref are fixed, not supplied by untrusted page content.
    heading '02 · 获取版本与下载文件'
    download 'https://api.github.com/repos/brian952700/warp-xray-debian/commits/main' "$task_temp/commit.json"
    task_sha="$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sha"])' "$task_temp/commit.json")"
    [[ "$task_sha" =~ ^[a-f0-9]{40}$ ]] || { message 31 '[失败] 无法解析 GitHub 提交编号。'; exit 1; }
    message 90 "版本提交：${task_sha:0:12}"
    task_base="https://raw.githubusercontent.com/brian952700/warp-xray-debian/$task_sha"
    task_target="/usr/local/lib/warp-xray/releases/$task_sha"
    download "$task_base/SHA256SUMS" "$task_temp/SHA256SUMS"
    if ! verify_payload "$task_temp/SHA256SUMS" "$task_target" 2>/dev/null; then
        download_payload "$task_base"
    fi
    heading '03 · 校验文件并安装命令'
    publish_release "$task_target" "$task_base"
    if ! /usr/local/bin/warp-xray update-health; then
        message 31 '[失败] 管理工具已更新，健康检查迁移失败。请运行 warp-xray update-health 重试。' >&2
        exit 1
    fi
    message 32 '[成功] warp-xray 管理工具已安装。'
    printf '\n'
    message 36 '管理菜单：sudo warp-xray'
    message 90 '其他命令：setup / status / prepare / apply / restore / uninstall'
    if [[ "$action" == --download-only ]]; then
        message 90 '本次仅更新管理工具，没有修改 Xray 或 WARP 配置。'
        printf '\n'
        return
    fi
    # A readable /dev/tty node may have no controlling terminal attached.
    if ! { exec {tty_fd}</dev/tty; } 2>/dev/null; then
        message 33 '[注意] 没有交互终端。工具已下载，请稍后运行 sudo warp-xray。'
        return
    fi
    # Read interactive answers from the terminal even when this installer is piped.
    /usr/local/bin/warp-xray setup <&"$tty_fd"
    exec {tty_fd}<&-
}

main "$@"
