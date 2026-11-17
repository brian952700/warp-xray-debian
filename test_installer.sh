#!/usr/bin/env bash
# Run only in the disposable Debian CI container.
set -euo pipefail

if [[ "${GITHUB_ACTIONS:-}" != true || $EUID -ne 0 ||
      -e /usr/local/bin/warp-xray || -L /usr/local/bin/warp-xray ||
      -e /usr/local/lib/warp-xray || -L /usr/local/lib/warp-xray ]]; then
    echo 'The installer test requires a clean, disposable GitHub Actions container.' >&2
    exit 1
fi

workspace="$(pwd)"
fixture_dir="$(mktemp -d)"
trap 'rm -rf -- "$fixture_dir"' EXIT

cat > "$fixture_dir/curl" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
url=''
output=''
while (($#)); do
    case "$1" in
        https://*) url="$1"; shift ;;
        -o) output="$2"; shift 2 ;;
        *) shift ;;
    esac
done
case "$url" in
    https://api.github.com/repos/brian952700/warp-xray-debian/commits/main)
        printf '{"sha":"%s"}\n' "$INSTALLER_FIXTURE_COMMIT" > "$output"
        ;;
    "https://raw.githubusercontent.com/brian952700/warp-xray-debian/$INSTALLER_FIXTURE_COMMIT/"*)
        name="${url##*/}"
        case "$name" in
            SHA256SUMS) cp -- "$INSTALLER_FIXTURE_REPO/$name" "$output" ;;
            warp-xray.py|warp-xray.sh)
                if [[ "$INSTALLER_FIXTURE_MODE" == cache ]]; then
                    echo "Unexpected payload download: $name" >&2
                    exit 1
                fi
                cp -- "$INSTALLER_FIXTURE_REPO/$name" "$output"
                if [[ "$INSTALLER_FIXTURE_MODE" == corrupt && "$name" == warp-xray.py ]]; then
                    printf '# corrupt fixture\n' >> "$output"
                fi
                ;;
            *) echo "Unexpected filename: $name" >&2; exit 1 ;;
        esac
        ;;
    *) echo "Unexpected URL: $url" >&2; exit 1 ;;
esac
SH
chmod 700 "$fixture_dir/curl"
export PATH="$fixture_dir:$PATH"
export INSTALLER_FIXTURE_REPO="$workspace"
export INSTALLER_FIXTURE_COMMIT='1111111111111111111111111111111111111111'
export INSTALLER_FIXTURE_MODE=normal

# Default setup must finish successfully without a controlling terminal.
setsid --wait bash install.sh </dev/null > "$fixture_dir/install.log"
grep -F '没有交互终端' "$fixture_dir/install.log"
/usr/local/bin/warp-xray --help
release="/usr/local/lib/warp-xray/releases/$INSTALLER_FIXTURE_COMMIT"
inode="$(stat -c %i "$release/warp-xray.py")"

# Same-version upgrades must reuse the checksum-verified release directory.
INSTALLER_FIXTURE_MODE=cache bash install.sh --download-only
test "$(stat -c %i "$release/warp-xray.py")" = "$inode"
cp -- /usr/local/bin/warp-xray "$fixture_dir/original-wrapper"

# A failed update must leave the checked-out version runnable.
export INSTALLER_FIXTURE_COMMIT='2222222222222222222222222222222222222222'
export INSTALLER_FIXTURE_MODE=corrupt
if bash install.sh --download-only > "$fixture_dir/corrupt.log" 2>&1; then
    echo 'Corrupt payload was unexpectedly installed' >&2
    exit 1
fi
grep -F 'checksum mismatch: warp-xray.py' "$fixture_dir/corrupt.log"
cmp -- "$fixture_dir/original-wrapper" /usr/local/bin/warp-xray
test ! -e "/usr/local/lib/warp-xray/releases/$INSTALLER_FIXTURE_COMMIT"
/usr/local/bin/warp-xray --help

# A daemon-reload failure preserves the legacy service and allows a retry.
legacy_unit=/etc/systemd/system/warp-xray-health.service
test ! -e "$legacy_unit"
mkdir -p -- "${legacy_unit%/*}"
printf '[Service]\nExecStart=/usr/bin/python3 /usr/local/lib/warp-xray/warp-xray.py watchdog\n' \
    > "$fixture_dir/legacy.service"
cp -- "$fixture_dir/legacy.service" "$legacy_unit"
cat > "$fixture_dir/systemctl" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
test "$*" = daemon-reload
test "${INSTALLER_FIXTURE_RELOAD:-fail}" = success
SH
chmod 700 "$fixture_dir/systemctl"
export INSTALLER_FIXTURE_COMMIT='1111111111111111111111111111111111111111'
export INSTALLER_FIXTURE_MODE=cache
if bash install.sh --download-only > "$fixture_dir/migration.log" 2>&1; then
    echo 'Health migration unexpectedly succeeded' >&2
    exit 1
fi
grep -F 'warp-xray update-health 重试' "$fixture_dir/migration.log"
cmp -- "$fixture_dir/legacy.service" "$legacy_unit"
/usr/local/bin/warp-xray --help
INSTALLER_FIXTURE_RELOAD=success /usr/local/bin/warp-xray update-health
grep -Fx 'ExecStart=/usr/local/bin/warp-xray watchdog' "$legacy_unit"
