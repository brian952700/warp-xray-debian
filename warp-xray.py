#!/usr/bin/env python3
"""WARP official client + Xray routing. Debian 12+. Standard library only."""
import argparse
import copy
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata

BASE = Path('/var/lib/warp-xray')
ARCHIVES = Path('/var/backups/warp-xray')
INSTALL_ROOT = Path('/usr/local/lib/warp-xray')
WRAPPER = Path('/usr/local/bin/warp-xray')
HEALTH_UNITS = [Path('/etc/systemd/system/warp-xray-health.timer'),
                Path('/etc/systemd/system/warp-xray-health.service')]
WARP_REPO = Path('/etc/apt/sources.list.d/cloudflare-client.list')
WARP_KEY = Path('/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg')
WARP_DIRS = [Path('/var/lib/cloudflare-warp'), Path('/var/log/cloudflare-warp'),
             Path('/etc/cloudflare-warp')]
TAG = 'warp-google-managed'
BLOCK = 'warp-google-udp-managed'
DOMAINS = ['google.com', 'googleapis.com', 'gstatic.com', 'googleusercontent.com',
           'googlevideo.com', 'youtube.com', 'youtu.be', 'ytimg.com',
           'ggpht.com', 'googleadservices.com', 'googlesyndication.com',
           'doubleclick.net', 'google.co.jp', 'google.co.uk', 'google.com.hk']


class TerminalUI:
    """Small, dependency-free terminal UI. Never clear previous diagnostics."""
    no_color = False
    palette = {'title': '1;36', 'info': '36', 'ok': '32', 'warn': '33',
               'error': '31', 'muted': '90', 'prompt': '1;36'}
    labels = {'info': '提示', 'ok': '成功', 'warn': '注意', 'error': '失败'}

    def paint(self, text, kind):
        enabled = (not self.no_color and 'NO_COLOR' not in os.environ and
                   os.environ.get('TERM') != 'dumb' and sys.stdout.isatty())
        return f'\033[{self.palette[kind]}m{text}\033[0m' if enabled else str(text)

    def width(self):
        return max(12, min(72, shutil.get_terminal_size(fallback=(80, 24)).columns - 4))

    def wrap(self, text):
        # Chinese terminal glyphs occupy two columns. ANSI is added after wrapping.
        lines, line, size = [], '', 0
        for char in str(text):
            if char == '\n':
                lines.append(line)
                line, size = '', 0
                continue
            width = 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in 'WF' else 1)
            if size + width > self.width() and line:
                lines.append(line)
                line, size = '', 0
            line += char
            size += width
        lines.append(line)
        return lines

    def text(self, text='', kind=None):
        for line in self.wrap(text):
            print('  ' + (self.paint(line, kind) if kind else line), flush=True)

    def section(self, title, subtitle=None):
        print('', flush=True)
        self.text('─' * self.width(), 'muted')
        self.text(title, 'title')
        if subtitle:
            self.text(subtitle, 'muted')
        self.text('─' * self.width(), 'muted')
        print('', flush=True)

    def message(self, kind, text):
        self.text(f'[{self.labels[kind]}] {text}', kind)

    def detail(self, label, value):
        self.text(label + '：', 'muted')
        print('  ' + str(value), flush=True)

    def command(self, command):
        print('', flush=True)
        print(self.paint('  $ ' + command, 'info'), flush=True)

    def ask(self, label, default=None, hint=None):
        print('', flush=True)
        self.text(label, 'prompt')
        if hint:
            self.text(hint, 'muted')
        suffix = f' [默认：{default}]' if default is not None else ''
        answer = input(self.paint('  >' + suffix + ' ', 'prompt')).strip()
        return answer if answer else (default if default is not None else '')

    def confirm(self, label, hint=None):
        while True:
            value = self.ask(label + ' [y/N]', hint=hint).lower()
            if value in ('y', 'yes', '是'):
                return True
            if value in ('', 'n', 'no', '否'):
                return False
            self.message('warn', '请输入 y 或 n；直接回车表示否。')


ui = TerminalUI()


def show_rules(rules):
    ui.text('现有路由（仅显示出站与匹配字段）', 'muted')
    for i, rule in enumerate(rules):
        target = rule.get('outboundTag') or rule.get('balancerTag') or '(未标注)'
        fields = ', '.join(k for k in rule if k not in ('type', 'outboundTag', 'balancerTag'))
        ui.text(f'[{i}] → {target}  ·  {fields or "无匹配字段"}')


def ask_rule_index(rules):
    if not rules:
        ui.message('info', '没有现有路由，将添加到位置 0。')
        return 0
    show_rules(rules)
    ui.text('API / 安全拦截规则留在前面，兜底规则留在 Google 分流之后。', 'muted')
    while True:
        answer = ui.ask(f'插入位置：0 ～ {len(rules)}',
                        hint='N 表示放在原第 N 条之前；最大值表示末尾。')
        try:
            index = int(answer)
            if 0 <= index <= len(rules):
                return index
        except ValueError:
            pass
        ui.message('warn', f'请输入 0 到 {len(rules)} 之间的整数。')


def run(argv, check=True, capture=False, timeout=90):
    return subprocess.run([str(x) for x in argv], check=check, text=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None, timeout=timeout)


def apt(args):
    # Package hooks (including initramfs) can legitimately take many minutes.
    # Never kill apt/dpkg using the short timeout used for service commands.
    title = '更新软件源' if args[0] == 'update' else ('卸载 WARP 软件包' if args[0] == 'purge' else '安装 / 检查软件包')
    ui.section(title, '以下为系统包管理器输出；耗时操作会等待完成。')
    result = run(['apt-get', '-o', 'DPkg::Lock::Timeout=300'] + args, timeout=None)
    print('', flush=True)
    ui.message('ok', title + '完成。')
    return result


def root():
    if os.geteuid() != 0:
        raise ValueError('请使用 sudo/root 运行。')
    data = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines()
                if '=' in line)
    if data.get('ID', '').strip('"') != 'debian' or int(data['VERSION_ID'].strip('"').split('.')[0]) < 12:
        raise ValueError('仅面向 Debian 12+；未来版本还需官方软件源支持。')
    os.umask(0o077)
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(path):
    if not Path(path).is_file():
        raise ValueError('需要具体的配置/清单文件，不能填写目录：' + str(path))
    # Reject JSONC, duplicate keys, and malformed JSON rather than silently discard settings.
    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('重复 JSON 字段：' + key)
            result[key] = value
        return result
    return json.loads(Path(path).read_text(encoding='utf-8-sig'), object_pairs_hook=unique)


def managed_present(cfg):
    return (any(x.get('tag') in (TAG, BLOCK) for x in cfg.get('outbounds', [])) or
            any(x.get('outboundTag') in (TAG, BLOCK)
                for x in cfg.get('routing', {}).get('rules', [])))


def save(path, obj):
    Path(path).write_text(json.dumps(obj, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def patch_config(source, domains, index, sniff=False, socks_style='legacy'):
    cfg = copy.deepcopy(source)
    outs = cfg.setdefault('outbounds', [])
    if not outs:
        raise ValueError('配置没有现有默认出站，拒绝让 WARP 意外成为默认出口。')
    if any(x.get('tag') in (TAG, BLOCK) for x in outs):
        raise ValueError('已存在脚本管理的出站；请先恢复原配置，或手动调整现有分流。')
    route = cfg.setdefault('routing', {})
    rules = route.setdefault('rules', [])
    if not 0 <= index <= len(rules):
        raise ValueError('rule-index 超出范围。')
    if not domains:
        raise ValueError('域名列表为空。')
    settings = {'servers': [{'address': '127.0.0.1', 'port': 40000}]}
    if socks_style == 'current':
        settings = {'address': '127.0.0.1', 'port': 40000}
    outs.extend([{'tag': TAG, 'protocol': 'socks', 'settings': settings},
                 {'tag': BLOCK, 'protocol': 'blackhole', 'settings': {}}])
    match = ['domain:' + d for d in domains]
    # WARP local proxy UDP support is not assumed. Reject matched UDP, never send it direct.
    rules[index:index] = [
        {'type': 'field', 'network': 'udp', 'domain': match, 'outboundTag': BLOCK},
        {'type': 'field', 'network': 'tcp', 'domain': match, 'outboundTag': TAG}]
    if sniff:
        for inbound in cfg.get('inbounds', []):
            if inbound.get('protocol') not in ('vless', 'vmess', 'trojan', 'shadowsocks', 'socks', 'http'):
                continue
            # Preserve custom sniffing; only add a missing section.
            if 'sniffing' not in inbound:
                inbound['sniffing'] = {'enabled': True, 'destOverride': ['http', 'tls', 'quic'],
                                       'routeOnly': True}
    return cfg


def service_context(service, config):
    if not re.fullmatch(r'[A-Za-z0-9_.@-]+\.service', service):
        raise ValueError('service 必须是 systemd 服务名称，例如 xray.service。')
    pid = int(run(['systemctl', 'show', service, '-p', 'MainPID', '--value'], capture=True).stdout.strip())
    if not pid:
        raise ValueError('Xray 服务必须正在运行，才能验证实际配置路径。')
    args = Path(f'/proc/{pid}/cmdline').read_bytes().decode().strip('\0').split('\0')
    binary = Path(f'/proc/{pid}/exe').resolve()
    if 'xray' not in binary.name.lower():
        raise ValueError('服务主进程不是 Xray。面板请使用 export，不要直接覆盖生成的配置。')
    cwd = Path(f'/proc/{pid}/cwd').resolve()
    configs = []
    env = Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
    if any(e.startswith((b'XRAY_LOCATION_CONFDIR=', b'v2ray.location.confdir=')) and e.split(b'=', 1)[1] for e in env):
        raise ValueError('检测到配置目录环境变量，仅支持 export。')
    for i, arg in enumerate(args[1:], 1):
        if 'confdir' in arg.lower():
            raise ValueError('检测到多文件配置目录，仅支持 export。')
        if arg in ('-c', '-config', '--config'):
            if i + 1 >= len(args):
                raise ValueError('配置参数不完整。')
            configs.append(args[i + 1])
        elif any(arg.startswith(k + '=') for k in ('-c', '-config', '--config')):
            configs.append(arg.split('=', 1)[1])
    resolved = [(cwd / p).resolve() for p in configs]
    if resolved != [Path(config).resolve()]:
        raise ValueError('实际进程不是仅加载指定的单个配置文件；请使用 export，或核对 systemctl cat。')
    return str(binary)


def validate(binary, path):
    # Keep output on terminal; never publish full configs or private keys in reports.
    ui.message('info', '正在校验 Xray 配置…')
    run([binary, 'run', '-test', '-config', path], capture=True)
    ui.message('ok', 'Xray 配置校验通过。')


def probe(family=None):
    cmd = ['curl', '--silent', '--show-error', '--fail', '--noproxy', '',
           '--connect-timeout', '5', '--max-time', '12', '--proxy',
           'socks5h://127.0.0.1:40000', 'https://www.cloudflare.com/cdn-cgi/trace']
    if family:
        # Domain names below force the *destination* family; curl -4/-6 would only
        # constrain connection to the SOCKS server, which is always IPv4 here.
        cmd[-1] = f'https://{family}.cloudflare.com/cdn-cgi/trace'
    result = run(cmd, check=False, capture=True, timeout=18)
    fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    return result.returncode == 0 and fields.get('warp') in ('on', 'plus'), fields, result.stderr.strip()


def warp(args):
    # Avoid several bare "Success" lines; failures are printed with diagnostics.
    run(['warp-cli', '--accept-tos'] + args, capture=True)


def install_warp(_):
    apt(['update'])
    apt(['install', '-y', 'curl', 'ca-certificates', 'gnupg', 'python3'])
    if not shutil.which('warp-cli'):
        data = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
        codename = data.get('VERSION_CODENAME', '').strip('"')
        if not re.fullmatch('[a-z]+', codename):
            raise ValueError('无法识别发行版代号。')
        # Do not substitute bookworm/trixie repositories on future Debian releases.
        run(['curl', '-fsS', '--max-time', '20', '-o', '/dev/null',
             f'https://pkg.cloudflareclient.com/dists/{codename}/Release'])
        key = BASE / 'cloudflare-key.asc'
        run(['curl', '-fsS', '--max-time', '30', '-o', key, 'https://pkg.cloudflareclient.com/pubkey.gpg'])
        keyring = Path('/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg')
        run(['gpg', '--batch', '--yes', '--dearmor', '--output', keyring, key])
        keyring.chmod(0o644)
        repo = Path('/etc/apt/sources.list.d/cloudflare-client.list')
        if repo.exists():
            shutil.copy2(repo, BASE / ('repo-before-' + str(time.time_ns())))
        repo.write_text(f'deb [signed-by={keyring}] https://pkg.cloudflareclient.com/ {codename} main\n')
        repo.chmod(0o644)
        apt(['update'])
    # A previous interrupted installation may have created warp-cli without
    # completing package configuration. Ask APT to finish; do not upgrade it.
    apt(['install', '--no-upgrade', '-y', 'cloudflare-warp'])
    ui.section('配置 WARP 本地代理', '保留现有注册；代理地址 127.0.0.1:40000。')
    run(['systemctl', 'enable', '--now', 'warp-svc.service'])
    # Do not delete registrations, licenses or account settings.
    existing = run(['warp-cli', '--accept-tos', 'registration', 'show'], check=False, capture=True)
    if existing.returncode:
        warp(['registration', 'new'])
    warp(['mode', 'proxy'])
    warp(['proxy', 'port', '40000'])
    warp(['connect'])
    ui.message('info', '正在连接并测试真实 SOCKS5 请求…')
    for _ in range(12):
        if probe()[0]:
            ui.message('ok', 'WARP SOCKS5 实际请求成功。')
            return
        time.sleep(2)
    raise ValueError('WARP 未通过连通性检查。未修改 Xray，请查看 journalctl -u warp-svc。')


def domains_for(args):
    domains = DOMAINS if not args.domains else [x.strip().lower() for x in Path(args.domains).read_text().splitlines()
                                              if x.strip() and not x.lstrip().startswith('#')]
    for d in domains:
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', d) or '..' in d:
            raise ValueError('非法域名：' + d)
    return list(dict.fromkeys(domains))


def export(args):
    out = Path(args.output).resolve()
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any((out / p).exists() for p in ('outbounds.json', 'routing-rules.json')):
        raise ValueError('导出文件已存在，请选择新的目录。')
    cfg = patch_config({'outbounds': [{'protocol': 'freedom'}]}, domains_for(args), 0,
                       socks_style=args.socks_style)
    save(out / 'outbounds.json', cfg['outbounds'][1:])
    save(out / 'routing-rules.json', cfg['routing']['rules'])
    ui.message('ok', '配置片段已导出。')
    ui.detail('保存位置', out)
    ui.message('info', '请合并到面板或完整配置中，再由 Xray 校验；片段不是完整 config。')


def prepare(args):
    path = Path(args.config).resolve(strict=True)
    cfg = load(path)
    if managed_present(cfg):
        raise ValueError('检测到已有/残留的 WARP 分流，跳过重复配置；请用 status 检查，或 uninstall 恢复原配置。')
    count = len(cfg.get('routing', {}).get('rules', []))
    if args.rule_index is None and count:
        show_rules(cfg['routing']['rules'])
        raise ValueError('请用 --rule-index N 指定插入位置；API、安全拦截规则应保留在前，兜底规则应在后。')
    binary = service_context(args.service, path)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + str(time.time_ns())[-6:]
    plan = BASE / ('plan-' + stamp)
    plan.mkdir(mode=0o700)
    shutil.copy2(path, plan / 'original.json')
    candidate = plan / 'candidate.json'
    index = args.rule_index if args.rule_index is not None else 0
    modified = patch_config(cfg, domains_for(args), index, args.add_sniffing)
    save(candidate, modified)
    try:
        validate(binary, candidate)
    except subprocess.CalledProcessError:
        ui.message('info', '当前内核未接受旧版 SOCKS 字段，尝试新版格式…')
        modified = patch_config(cfg, domains_for(args), index, args.add_sniffing, 'current')
        save(candidate, modified)
        validate(binary, candidate)
    save(plan / 'manifest.json', {'config': str(path), 'service': args.service, 'binary': binary,
                                'original_sha256': digest(path), 'candidate_sha256': digest(candidate),
                                'domains': domains_for(args), 'rule_index': index})
    ui.message('ok', '候选配置已生成并校验，尚未修改运行配置。')
    ui.detail('备份与候选配置目录', plan)
    ui.command('warp-xray apply --plan ' + str(plan))
    return plan


def replace_bytes(path, source):
    path = Path(path)
    stat = path.stat()
    fd, tmp = tempfile.mkstemp(prefix='.warp-xray-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(Path(source).read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(tmp, stat.st_uid, stat.st_gid)
        os.chmod(tmp, stat.st_mode & 0o777)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def restart_xray(service):
    ui.message('info', '正在重启 Xray，现有连接会短暂中断…')
    run(['systemctl', 'restart', service])
    time.sleep(4)
    run(['systemctl', 'is-active', '--quiet', service])


def apply(args):
    plan = Path(args.plan).resolve(strict=True)
    m = load(plan / 'manifest.json')
    if (plan / 'applied').exists():
        raise ValueError('计划已经应用，不重复执行。')
    if digest(m['config']) != m['original_sha256']:
        raise ValueError('运行配置已经变化，请重新 prepare，避免覆盖其他修改。')
    if digest(plan / 'candidate.json') != m['candidate_sha256']:
        raise ValueError('候选配置被改动，请重新 prepare 和校验。')
    binary = service_context(m['service'], m['config'])
    validate(binary, plan / 'candidate.json')
    if not probe()[0]:
        raise ValueError('WARP 实际请求失败，拒绝应用。')
    replace_bytes(m['config'], plan / 'candidate.json')
    try:
        restart_xray(m['service'])
        service_context(m['service'], m['config'])
    except Exception:
        replace_bytes(m['config'], plan / 'original.json')
        restart_xray(m['service'])
        raise ValueError('新配置启动失败，已经恢复 Xray 原配置。')
    (plan / 'applied').write_text('applied\n')
    ui.message('ok', '新配置已应用，Xray 服务已启动。')
    ui.message('info', '请从客户端测试 Google 和其他网站；旧透明代理可用 cleanup-legacy 清理。')
    ui.command('warp-xray restore --plan ' + str(plan))


def restore(args):
    plan = Path(args.plan).resolve(strict=True)
    m = load(plan / 'manifest.json')
    if digest(m['config']) != m['candidate_sha256']:
        raise ValueError('运行配置与该计划不一致；拒绝覆盖后续修改。请人工合并备份。')
    if digest(plan / 'original.json') != m['original_sha256']:
        raise ValueError('原始备份校验失败。')
    binary = service_context(m['service'], m['config'])
    validate(binary, plan / 'original.json')
    replace_bytes(m['config'], plan / 'original.json')
    try:
        restart_xray(m['service'])
    except Exception:
        replace_bytes(m['config'], plan / 'candidate.json')
        restart_xray(m['service'])
        raise ValueError('原配置未能启动，已换回候选配置。')
    ui.message('ok', 'Xray 原配置已恢复。WARP 保留，旧透明代理不会重新启用。')


def cleanup(args, restoring=False):
    # Only accept the known legacy script, never execute an unknown file as root.
    old = Path('/usr/local/bin/warp-google')
    if not old.exists():
        raise ValueError('未发现旧脚本标记，不自动清理。')
    text = old.read_text()
    if not all(s in text for s in ('WARP_GOOGLE', 'redsocks -c /etc/redsocks.conf', '--to-ports 12345')):
        raise ValueError('旧脚本与已知版本不符，请人工检查。')
    plan = Path(args.plan).resolve(strict=True)
    m = load(plan / 'manifest.json')
    expected = m['original_sha256'] if restoring else m['candidate_sha256']
    if (not restoring and not (plan / 'applied').exists()) or digest(m['config']) != expected:
        raise ValueError('请先应用并验证 Xray 分流计划，再清理旧配置。')
    if not restoring and not probe()[0]:
        raise ValueError('WARP 不可用，暂不清理。')
    backup = plan / ('legacy-' + str(time.time_ns()))
    backup.mkdir(mode=0o700)
    files = ['/usr/local/bin/warp-google', '/usr/local/bin/warp', '/etc/redsocks.conf',
             '/etc/systemd/system/warp-google.service', '/etc/gai.conf']
    for name in files:
        if Path(name).exists():
            target = backup / name.lstrip('/')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(name, target)
    if shutil.which('iptables-save'):
        result = run(['iptables-save'], capture=True)
        (backup / 'iptables-before.txt').write_text(result.stdout)
    unit = Path('/etc/systemd/system/warp-google.service')
    if unit.exists():
        content = unit.read_text()
        if 'ExecStop=/usr/local/bin/warp-google stop' not in content:
            raise ValueError('旧服务内容不同，请人工清理；备份已保存。')
        # systemctl stop would execute the legacy script's global pkill redsocks.
        # Remove its commands in memory via runtime drop-in before stopping.
        dropdir = Path('/run/systemd/system/warp-google.service.d')
        dropdir.mkdir(parents=True, exist_ok=True)
        drop = dropdir / '99-warp-xray-cleanup.conf'
        drop.write_text('[Service]\nExecStop=\n')
        run(['systemctl', 'daemon-reload'])
        run(['systemctl', 'disable', '--now', 'warp-google.service'])
        unit.unlink()
        drop.unlink()
        run(['systemctl', 'daemon-reload'])
    if shutil.which('iptables'):
        # Delete only exact OUTPUT jumps and the dedicated legacy chain.
        while run(['iptables', '-w', '5', '-t', 'nat', '-C', 'OUTPUT', '-j', 'WARP_GOOGLE'],
                  check=False, capture=True).returncode == 0:
            run(['iptables', '-w', '5', '-t', 'nat', '-D', 'OUTPUT', '-j', 'WARP_GOOGLE'])
        exists = run(['iptables', '-w', '5', '-t', 'nat', '-S', 'WARP_GOOGLE'], check=False, capture=True)
        if exists.returncode == 0:
            run(['iptables', '-w', '5', '-t', 'nat', '-F', 'WARP_GOOGLE'])
            run(['iptables', '-w', '5', '-t', 'nat', '-X', 'WARP_GOOGLE'])
    if shutil.which('ip'):
        run(['ip', '-6', 'route', 'del', 'blackhole', '2607:f8b0::/32'], check=False, capture=True)
    gai = Path('/etc/gai.conf')
    if gai.exists():
        # Exact line written by the supplied script. Preserve all other settings.
        lines = gai.read_text().splitlines(keepends=True)
        gai.write_text(''.join(s for s in lines if s.rstrip('\r\n') != 'precedence ::ffff:0:0/96  100'))
    old.unlink()
    manager = Path('/usr/local/bin/warp')
    if manager.exists() and 'WARP 管理工具  （VPS值得买' in manager.read_text():
        manager.unlink()
    # Leave redsocks processes/package/config intact: could be used by another service.
    ui.message('ok', '旧分流规则、开机服务和已识别的管理命令已清理。')
    ui.detail('清理前备份', backup)
    ui.message('info', 'redsocks 可能由其他服务共用，仍然保留。')


def checked_uninstall_plan(plan):
    plan = Path(plan).resolve(strict=True)
    m = load(plan / 'manifest.json')
    original = plan / 'original.json'
    candidate = plan / 'candidate.json'
    if digest(original) != m['original_sha256'] or digest(candidate) != m['candidate_sha256']:
        raise ValueError('备份或候选配置哈希不符，拒绝卸载。')
    if managed_present(load(original)):
        raise ValueError('该备份仍含有本工具的分流标记，不是可恢复的原始配置。')
    current = digest(m['config'])
    if current not in (m['original_sha256'], m['candidate_sha256']):
        raise ValueError('当前 config 已有后续修改，拒绝覆盖。请人工合并原始备份后再卸载。')
    return plan, m


def select_uninstall_plan(explicit=None):
    if explicit:
        return checked_uninstall_plan(explicit)
    matches = []
    for directory in sorted(BASE.glob('plan-*'), reverse=True):
        try:
            plan, m = checked_uninstall_plan(directory)
            if (plan / 'applied').exists() or digest(m['config']) == m['candidate_sha256']:
                matches.append((plan, m))
        except (ValueError, OSError, KeyError):
            continue
    # Equivalent duplicate plans have identical restore content. Never choose
    # arbitrarily between different files, services, or original backups.
    choices = {(m['config'], m['service'], m['original_sha256']) for _, m in matches}
    if len(choices) != 1:
        raise ValueError('找不到唯一可恢复的已应用计划；请用 uninstall --plan /var/lib/warp-xray/plan-编号 指定原备份。')
    return matches[0]


def preflight_uninstall(plan, m):
    # No other tracked config may still depend on the WARP package.
    for directory in BASE.glob('plan-*'):
        manifest = directory / 'manifest.json'
        if not manifest.is_file():
            continue
        other = load(manifest)
        path = Path(other['config'])
        if path.resolve() != Path(m['config']).resolve() and path.exists() and managed_present(load(path)):
            raise ValueError('另一个配置仍在使用 WARP 分流，请先 restore：' + str(path))
    for path in [BASE, INSTALL_ROOT, *WARP_DIRS]:
        if path.exists() or path.is_symlink():
            if path.is_symlink() or path.resolve() != path.absolute():
                raise ValueError('安装/数据目录含符号链接，拒绝自动递归删除：' + str(path))
    for path in HEALTH_UNITS:
        if path.exists():
            content = path.read_text()
            owned = ('Description=Periodic WARP proxy health check' in content and
                     'OnUnitActiveSec=1min' in content) if path.suffix == '.timer' else (
                         'ExecStart=/usr/bin/python3 /usr/local/lib/warp-xray/warp-xray.py watchdog' in content)
            if not owned:
                raise ValueError('健康检查单元内容不符，拒绝删除：' + str(path))
    if WRAPPER.exists() and '/warp-xray.py' not in WRAPPER.read_text():
        raise ValueError('warp-xray 命令不属于本工具，拒绝删除。')
    if WARP_REPO.exists():
        lines = [s.strip() for s in WARP_REPO.read_text().splitlines() if s.strip() and not s.lstrip().startswith('#')]
        if any('https://pkg.cloudflareclient.com/' not in s for s in lines):
            raise ValueError('Cloudflare 源文件含其他内容，拒绝自动删除。')
    # A shared signing key must not be removed while another source uses it.
    if WARP_KEY.exists():
        for source in WARP_REPO.parent.iterdir():
            if source != WARP_REPO and source.is_file() and source.suffix in ('.list', '.sources'):
                if str(WARP_KEY) in source.read_text():
                    raise ValueError('其他 APT 源仍引用 WARP 密钥，请先人工处理：' + str(source))
    old = Path('/usr/local/bin/warp-google')
    oldunit = Path('/etc/systemd/system/warp-google.service')
    chain = (run(['iptables', '-w', '5', '-t', 'nat', '-S', 'WARP_GOOGLE'],
                 check=False, capture=True).returncode == 0) if shutil.which('iptables') else False
    if old.exists():
        if not all(s in old.read_text() for s in ('WARP_GOOGLE', 'redsocks -c /etc/redsocks.conf', '--to-ports 12345')):
            raise ValueError('存在未知旧透明代理脚本，请先人工清理。')
        if oldunit.exists() and 'ExecStop=/usr/local/bin/warp-google stop' not in oldunit.read_text():
            raise ValueError('存在未知旧透明代理服务，请先人工清理。')
    elif oldunit.exists() or chain:
        raise ValueError('旧透明代理标记不完整，请先人工清理其服务/规则；本次未修改配置。')
    binary = service_context(m['service'], m['config'])
    validate(binary, plan / 'original.json')


def restore_for_uninstall(plan, m):
    if digest(m['config']) == m['candidate_sha256']:
        restore(argparse.Namespace(plan=str(plan)))
    else:
        # The file may already be restored, but the live process may still have
        # old rules. Restart it before disconnecting WARP.
        restart_xray(m['service'])
    service_context(m['service'], m['config'])
    if digest(m['config']) != m['original_sha256']:
        raise ValueError('恢复后 config 再次变化，停止卸载 WARP。')


def stop_health():
    for path in HEALTH_UNITS:
        if path.exists():
            if path.suffix == '.timer':
                run(['systemctl', 'disable', '--now', path.name])
            else:
                run(['systemctl', 'stop', path.name])


def remove_uninstall_components():
    stop_health()
    for path in HEALTH_UNITS:
        path.unlink(missing_ok=True)
    run(['systemctl', 'daemon-reload'])
    if shutil.which('warp-cli'):
        run(['warp-cli', '--accept-tos', 'disconnect'], check=False)
    state = run(['systemctl', 'show', 'warp-svc.service', '-p', 'LoadState', '--value'], capture=True)
    if state.stdout.strip() != 'not-found':
        run(['systemctl', 'disable', '--now', 'warp-svc.service'])
    apt(['purge', '-y', 'cloudflare-warp'])
    for path in (WARP_REPO, WARP_KEY):
        path.unlink(missing_ok=True)
    for path in WARP_DIRS:
        if path.exists():
            if path.is_symlink() or path.resolve() != path.absolute():
                raise ValueError('WARP 数据路径变化，拒绝删除：' + str(path))
            shutil.rmtree(path)
    run(['systemctl', 'daemon-reload'])


def finish_uninstall(archive):
    # Called only after a validated restore and successful package purge.
    WRAPPER.unlink(missing_ok=True)
    for path in (INSTALL_ROOT, BASE):
        if path.exists():
            if path.is_symlink() or path.resolve() != path.absolute():
                raise ValueError('工具目录路径变化，拒绝删除：' + str(path))
            shutil.rmtree(path)
    record = load(archive / 'uninstall.json')
    record['stage'] = 'complete'
    save(archive / 'uninstall.json', record)
    ui.section('卸载完成')
    ui.message('ok', '原始 config 已恢复，WARP、健康检查与管理工具已移除。')
    ui.detail('保留的原始配置及卸载前备份', archive)
    ui.message('info', 'Xray 与共享依赖仍然保留，旧透明代理不会重新启用。')


def uninstall(args):
    ui.section('完整卸载 · 恢复原始配置', '先验证备份与服务，再恢复配置，最后移除组件。')
    plan, m = select_uninstall_plan(args.plan)
    preflight_uninstall(plan, m)
    ui.detail('将恢复的配置文件', m['config'])
    ui.detail('原始备份', plan / 'original.json')
    ui.message('warn', '将移除 WARP（含本地注册数据）、健康检查、源/密钥和本工具。')
    ui.message('warn', '这台服务器上其他使用 WARP 的程序也会受影响。')
    if ui.ask('输入 UNINSTALL 确认完整卸载', hint='直接回车或输入其他内容取消。') != 'UNINSTALL':
        ui.message('info', '已取消，未修改配置。')
        return
    # Recheck after user review; never overwrite a concurrent edit.
    checked_uninstall_plan(plan)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + str(time.time_ns())[-6:]
    archive = ARCHIVES / stamp
    archive.mkdir(mode=0o700, parents=True)
    shutil.copytree(BASE, archive / 'state', symlinks=True)
    shutil.copy2(m['config'], archive / 'config-before-uninstall.json')
    for path in [*HEALTH_UNITS, WARP_REPO, WARP_KEY]:
        if path.exists():
            shutil.copy2(path, archive / path.name)
    save(archive / 'uninstall.json', {'config': m['config'], 'service': m['service'],
                                    'plan': str(plan), 'stage': 'backed-up'})
    ui.section('01 · 恢复 Xray 原配置')
    restore_for_uninstall(plan, m)
    # An offline WARP must not prevent removing legacy rules during uninstall.
    if Path('/usr/local/bin/warp-google').exists():
        cleanup(argparse.Namespace(plan=str(plan)), restoring=True)
    # Preserve the legacy cleanup backup created after the initial snapshot.
    shutil.copytree(BASE, archive / 'state', dirs_exist_ok=True, symlinks=True)
    save(archive / 'uninstall.json', {'config': m['config'], 'service': m['service'],
                                    'plan': str(plan), 'stage': 'config-restored'})
    ui.section('02 · 移除 WARP 与健康检查')
    remove_uninstall_components()
    save(archive / 'uninstall.json', {'config': m['config'], 'service': m['service'],
                                    'plan': str(plan), 'stage': 'package-removed'})
    finish_uninstall(archive)


def watchdog(_):
    import fcntl
    lock = open('/run/warp-xray-health.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    statepath = BASE / 'health-state.json'
    state = load(statepath) if statepath.exists() else {'failures': 0, 'last_restart': 0}
    if probe()[0]:
        if state['failures']:
            ui.message('ok', 'WARP 代理恢复。')
        state['failures'] = 0
    else:
        state['failures'] += 1
        if state['failures'] >= 3 and time.time() - state['last_restart'] >= 900:
            ui.message('warn', '连续代理请求失败，正在恢复 warp-svc。')
            state['last_restart'] = time.time()
            save(statepath, state)
            run(['systemctl', 'restart', 'warp-svc.service'])
            time.sleep(3)
            warp(['connect'])
    save(statepath, state)


def enable_health(_):
    target = Path('/usr/local/lib/warp-xray/warp-xray.py')
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.resolve() != Path(__file__).resolve():
        shutil.copyfile(__file__, target)
    target.chmod(0o700)
    Path('/etc/systemd/system/warp-xray-health.service').write_text(
        '[Unit]\nDescription=Check WARP SOCKS5 data path\nAfter=network-online.target warp-svc.service\n'
        '[Service]\nType=oneshot\nExecStart=/usr/bin/python3 /usr/local/lib/warp-xray/warp-xray.py watchdog\n'
        'TimeoutStartSec=90\nUMask=0077\n')
    Path('/etc/systemd/system/warp-xray-health.timer').write_text(
        '[Unit]\nDescription=Periodic WARP proxy health check\n[Timer]\nOnBootSec=2min\n'
        'OnUnitActiveSec=1min\nRandomizedDelaySec=10s\n[Install]\nWantedBy=timers.target\n')
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', '--now', 'warp-xray-health.timer'])
    ui.message('ok', '健康检查已启用。')
    ui.text('约每分钟检查；连续 3 次失败后恢复服务，恢复间隔至少 15 分钟。', 'muted')


def status(_):
    ui.section('运行状态', '服务状态与真实代理请求分别检查。')
    if not shutil.which('warp-cli'):
        ui.message('warn', '尚未检测到 WARP 客户端。')
        ui.command('warp-xray install-warp')
        return
    client = run(['warp-cli', 'status'], check=False, capture=True)
    ui.detail('WARP 客户端', client.stdout.strip() or client.stderr.strip() or '未返回状态')
    for name, label in [('warp-svc.service', 'WARP 服务'), ('warp-xray-health.timer', '健康检查计时器')]:
        result = run(['systemctl', 'is-active', name], check=False, capture=True)
        state = result.stdout.strip() or 'unknown'
        ui.message('ok' if state == 'active' else 'warn', label + '：' + state)
    enabled = run(['systemctl', 'is-enabled', 'warp-xray-health.timer'], check=False, capture=True)
    ui.detail('健康检查开机启动', enabled.stdout.strip() or '未启用')
    ui.section('出口连通性', '下面测试 WARP 代理出口，不等于 Xray 分流规则已命中。')
    for family in (None, 'ipv4', 'ipv6'):
        ok, fields, error = probe(family)
        label = {None: '自动选择', 'ipv4': 'IPv4 目标', 'ipv6': 'IPv6 目标'}[family]
        ui.message('ok' if ok else 'warn', label + ('：代理请求通过' if ok else '：代理请求未通过'))
        if fields.get('ip'):
            ui.detail('出口 IP', fields['ip'])
        if error:
            ui.text(error, 'muted')
        print('', flush=True)
    ui.text('IPv4 / IPv6 使用对应目标域名测试，不是 VPS 入站双栈测试。', 'muted')


def ask_config():
    while True:
        config = ui.ask('Xray 配置文件绝对路径', default='/usr/local/etc/xray/config.json',
                        hint='需要具体 JSON 文件，例如 config.json，不能只填写目录。')
        if not Path(config).is_absolute():
            ui.message('warn', '请填写以 / 开头的绝对路径。')
            continue
        try:
            return config, load(config)
        except (OSError, ValueError) as exc:
            ui.message('warn', str(exc))


def ask_service():
    while True:
        service = ui.ask('systemd 服务名', default='xray.service', hint='填写服务名即可，不需要 /etc/systemd/system/ 路径。')
        if service.startswith('/') and service.endswith('.service'):
            service = Path(service).name
            ui.message('info', '已识别服务名：' + service)
        if re.fullmatch(r'[A-Za-z0-9_.@-]+\.service', service):
            return service
        ui.message('warn', '服务名应类似 xray.service。')


def setup(_):
    ui.section('WARP + Xray · 设置向导', 'Google 流量分流 · Debian 12 / 13+')
    ui.text('修改前会备份并校验；应用配置时会短暂重启 Xray。', 'muted')
    ui.section('01 · 准备 WARP')
    if ui.confirm('安装 / 复用官方 WARP，并设置本地代理？', hint='选择 y 表示接受 Cloudflare 条款；现有注册会保留。'):
        install_warp(None)
    if not probe()[0]:
        raise ValueError('WARP SOCKS5 未就绪，先运行 warp-xray install-warp。')
    ui.message('ok', 'WARP 本地代理可用。')
    ui.section('02 · 确认 Xray 配置方式')
    if ui.confirm('Xray 是否由面板或多文件配置管理？', hint='例如 3x-ui / Marzban 或 -confdir；选择 y 将只导出配置片段。'):
        dest = ui.ask('片段导出目录', default='/root/warp-xray-fragments')
        export(argparse.Namespace(output=dest, domains=None, socks_style='legacy'))
        ui.message('info', '请在面板合并出站与路由。配置片段已生成，向导结束。')
        return
    config, cfg = ask_config()
    service = ask_service()
    if managed_present(cfg):
        ui.section('已检测到 WARP 分流')
        ui.message('info', '跳过重复配置，保留原始备份。')
        ui.command('warp-xray status')
        ui.command('warp-xray uninstall')
        if ui.confirm('启用 / 更新健康检查？'):
            enable_health(None)
        return
    rules = cfg.get('routing', {}).get('rules', [])
    ui.section('03 · 设置 Google 分流')
    index = ask_rule_index(rules)
    sniff = ui.confirm('为缺少嗅探配置的入站补充域名嗅探？',
                       hint='辅助识别 HTTP / TLS / QUIC 域名，仅用于路由；已有嗅探设置保留。')
    ui.section('04 · 备份与校验候选配置')
    plan = prepare(argparse.Namespace(config=config, service=service, rule_index=index,
                                      add_sniffing=sniff, domains=None))
    ui.detail('配置文件', config)
    ui.detail('服务', service)
    ui.detail('新规则插入位置', index)
    if not ui.confirm('现在应用候选配置并重启 Xray？', hint='会短暂中断现有连接；启动失败时尝试恢复原配置。'):
        ui.message('info', '已保留候选配置，尚未修改 Xray。')
        return
    apply(argparse.Namespace(plan=str(plan)))
    ui.section('05 · 可选维护功能')
    if Path('/usr/local/bin/warp-google').exists():
        if ui.confirm('备份并清理已识别的旧透明代理？', hint='适用于最初的 warp-g.sh，避免旧防火墙规则叠加。'):
            cleanup(argparse.Namespace(plan=str(plan)))
    if ui.confirm('启用健康检查与有限自动恢复？', hint='约每分钟检查；连续 3 次失败才恢复 WARP，恢复间隔至少 15 分钟。'):
        enable_health(None)
    ui.section('配置完成')
    ui.message('ok', 'Xray 分流配置已应用。')
    ui.message('info', '请用客户端测试 Google、其他网站与 IPv4 / IPv6。')
    ui.command('warp-xray status')


def disable_health(_):
    run(['systemctl', 'disable', '--now', 'warp-xray-health.timer'])
    ui.message('ok', '健康检查已暂停，当前 WARP 连接不会被断开。')


def menu(_):
    while True:
        ui.section('WARP + Xray · 管理菜单', 'Google 流量分流 · Debian 12 / 13+')
        for item in ('1  安装 / 配置 Google 分流', '2  查看运行状态与出口',
                     '3  开启 / 更新健康检查', '4  暂停健康检查',
                     '5  仅恢复 Xray 原配置（保留 WARP）',
                     '6  完整卸载并恢复原配置', '0  退出'):
            ui.text(item)
        choice = ui.ask('请选择操作', default='0')
        if choice == '0':
            ui.message('info', '已退出。')
            return
        try:
            if choice == '1':
                setup(None)
            elif choice == '2':
                status(None)
            elif choice == '3':
                enable_health(None)
            elif choice == '4':
                disable_health(None)
            elif choice == '5':
                directory = ui.ask('备份计划目录', hint='留空自动查找唯一匹配的已应用备份。')
                plan, m = select_uninstall_plan(directory or None)
                ui.detail('恢复目标', m['config'])
                if digest(m['config']) == m['original_sha256']:
                    ui.message('info', '文件内容已经是原配置；如需重新加载，请检查 Xray 服务。')
                elif ui.confirm('恢复原配置并重启 Xray？', hint='仅恢复配置，WARP 与健康检查保留。'):
                    restore(argparse.Namespace(plan=str(plan)))
            elif choice == '6':
                uninstall(argparse.Namespace(plan=None))
                return
            else:
                ui.message('warn', '请选择 0 到 6。')
                continue
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            report_error(exc)
        ui.ask('按回车返回管理菜单')


def report_error(exc):
    print('', flush=True)
    ui.message('error', str(exc))
    if isinstance(exc, subprocess.CalledProcessError):
        diagnostic = (exc.stderr or '').strip() or (exc.stdout or '').strip()
        if diagnostic:
            ui.text('命令返回信息：', 'muted')
            ui.text(diagnostic)
    print('', flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--no-color', action='store_true', help='关闭终端颜色（也支持 NO_COLOR）')
    sub = p.add_subparsers(dest='command')
    for cmd in ('install-warp', 'status', 'enable-health', 'watchdog', 'disable-health', 'setup', 'menu'):
        sub.add_parser(cmd)
    e = sub.add_parser('export', help='只导出面板/多文件配置需要合并的片段')
    e.add_argument('--output', required=True)
    e.add_argument('--domains')
    e.add_argument('--socks-style', choices=['legacy', 'current'], default='legacy')
    s = sub.add_parser('prepare', help='备份并生成候选配置，不重启服务')
    s.add_argument('--config', required=True)
    s.add_argument('--service', default='xray.service')
    s.add_argument('--rule-index', type=int)
    s.add_argument('--domains')
    s.add_argument('--add-sniffing', action='store_true')
    for cmd in ('apply', 'restore', 'cleanup-legacy'):
        q = sub.add_parser(cmd)
        q.add_argument('--plan', required=True)
    u = sub.add_parser('uninstall', help='先恢复原始 Xray 配置，再完整卸载 WARP 和工具；保留备份')
    u.add_argument('--plan', help='指定原始计划；默认寻找唯一匹配的已应用备份')
    args = p.parse_args()
    ui.no_color = args.no_color
    args.command = args.command or 'menu'
    root()
    import fcntl
    # Serialize operator actions; watchdog uses its own lock to avoid nested locking.
    lock = open('/run/warp-xray-admin.lock', 'w')
    if args.command != 'watchdog':
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    actions = {'menu': menu, 'uninstall': uninstall, 'setup': setup, 'install-warp': install_warp, 'prepare': prepare, 'apply': apply,
               'restore': restore, 'export': export, 'cleanup-legacy': cleanup,
               'enable-health': enable_health, 'watchdog': watchdog, 'status': status,
               'disable-health': disable_health}
    actions[args.command](args)


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print('', flush=True)
        ui.message('info', '操作已中止，终端日志已保留。')
        sys.exit(130)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        report_error(exc)
        sys.exit(1)
