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

BASE = Path('/var/lib/warp-xray')
TAG = 'warp-google-managed'
BLOCK = 'warp-google-udp-managed'
DOMAINS = ['google.com', 'googleapis.com', 'gstatic.com', 'googleusercontent.com',
           'googlevideo.com', 'youtube.com', 'youtu.be', 'ytimg.com',
           'ggpht.com', 'googleadservices.com', 'googlesyndication.com',
           'doubleclick.net', 'google.co.jp', 'google.co.uk', 'google.com.hk']


def run(argv, check=True, capture=False, timeout=90):
    return subprocess.run([str(x) for x in argv], check=check, text=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None, timeout=timeout)


def apt(args):
    # Package hooks (including initramfs) can legitimately take many minutes.
    # Never kill apt/dpkg using the short timeout used for service commands.
    return run(['apt-get', '-o', 'DPkg::Lock::Timeout=300'] + args, timeout=None)


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
    # Reject JSONC, duplicate keys, and malformed JSON rather than silently discard settings.
    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('重复 JSON 字段：' + key)
            result[key] = value
        return result
    return json.loads(Path(path).read_text(encoding='utf-8-sig'), object_pairs_hook=unique)


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
    run([binary, 'run', '-test', '-config', path])


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
    run(['warp-cli', '--accept-tos'] + args)


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
    run(['systemctl', 'enable', '--now', 'warp-svc.service'])
    # Do not delete registrations, licenses or account settings.
    existing = run(['warp-cli', '--accept-tos', 'registration', 'show'], check=False, capture=True)
    if existing.returncode:
        warp(['registration', 'new'])
    warp(['mode', 'proxy'])
    warp(['proxy', 'port', '40000'])
    warp(['connect'])
    for _ in range(12):
        if probe()[0]:
            print('WARP SOCKS5 实际请求成功。下一步 export 或 prepare。')
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
    print('已导出：', out, '；需合并到面板/全部配置中并由真实 Xray 校验，不是完整配置。')


def prepare(args):
    path = Path(args.config).resolve(strict=True)
    cfg = load(path)
    count = len(cfg.get('routing', {}).get('rules', []))
    if args.rule_index is None and count:
        print('已有路由规则（仅显示类型与出站，不显示用户凭据）：')
        for i, rule in enumerate(cfg['routing']['rules']):
            print(i, 'outbound=', rule.get('outboundTag', ''), 'fields=', ','.join(rule.keys()))
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
        print('尝试新版 SOCKS 出站字段格式……')
        modified = patch_config(cfg, domains_for(args), index, args.add_sniffing, 'current')
        save(candidate, modified)
        validate(binary, candidate)
    save(plan / 'manifest.json', {'config': str(path), 'service': args.service, 'binary': binary,
                                'original_sha256': digest(path), 'candidate_sha256': digest(candidate),
                                'domains': domains_for(args), 'rule_index': index})
    print('候选配置通过当前 Xray 校验，尚未修改运行配置。计划目录：', plan)
    print('检查 candidate.json 后运行：sudo python3 warp-xray.py apply --plan', plan)
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
    print('已应用。现有旧透明代理仍需 cleanup-legacy；请用客户端测试 Google 和非 Google 网站。')
    print('恢复命令：sudo python3 warp-xray.py restore --plan', plan)


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
    print('只恢复了 Xray 配置；不会重新启用旧透明代理或删除 WARP。')


def cleanup(args):
    # Only accept the known legacy script, never execute an unknown file as root.
    old = Path('/usr/local/bin/warp-google')
    if not old.exists():
        raise ValueError('未发现旧脚本标记，不自动清理。')
    text = old.read_text()
    if not all(s in text for s in ('WARP_GOOGLE', 'redsocks -c /etc/redsocks.conf', '--to-ports 12345')):
        raise ValueError('旧脚本与已知版本不符，请人工检查。')
    plan = Path(args.plan).resolve(strict=True)
    m = load(plan / 'manifest.json')
    if not (plan / 'applied').exists() or digest(m['config']) != m['candidate_sha256']:
        raise ValueError('请先应用并验证 Xray 分流计划，再清理旧配置。')
    if not probe()[0]:
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
    print('旧分流规则、开机服务和已识别管理命令已清理。备份：', backup)
    print('未终止/卸载 redsocks；确认无其他用途后可自行停用。回滚 Xray 不会恢复旧透明代理。')


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
            print('WARP 代理恢复。')
        state['failures'] = 0
    else:
        state['failures'] += 1
        if state['failures'] >= 3 and time.time() - state['last_restart'] >= 900:
            print('连续代理请求失败；恢复 warp-svc。不会重启 Xray、服务器或重注册账号。')
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
    print('已启用：连续 3 次失败后恢复服务；两次恢复至少间隔 15 分钟。')


def status(_):
    run(['warp-cli', 'status'], check=False)
    run(['systemctl', 'status', 'warp-xray-health.timer', '--no-pager'], check=False)
    for family in (None, 'ipv4', 'ipv6'):
        ok, fields, error = probe(family)
        print(family or 'auto', 'OK' if ok else 'FAIL', '出口=', fields.get('ip', '?'),
              'warp=', fields.get('warp', '?'), error)
    print('ipv4/ipv6 使用对应目标域名测试 WARP 出口；不是 VPS 入站双栈测试。')


def setup(_):
    print('WARP + Xray 设置向导：操作前备份，应用时短暂重启 Xray。')
    if input('安装/复用官方 WARP 并设为本地代理（接受 Cloudflare 条款）？[y/N] ').lower() == 'y':
        install_warp(None)
    if not probe()[0]:
        raise ValueError('WARP SOCKS5 未就绪，先运行 warp-xray install-warp。')
    if input('是否由面板或多文件配置管理 Xray？[y/N] ').lower() == 'y':
        dest = input('片段导出目录 [/root/warp-xray-fragments]：').strip() or '/root/warp-xray-fragments'
        export(argparse.Namespace(output=dest, domains=None, socks_style='legacy'))
        print('请在面板合并出站和路由；不直接修改面板配置，不自动清理旧透明代理。')
        return
    config = input('Xray 实际配置绝对路径：').strip()
    if not Path(config).is_absolute():
        raise ValueError('需要绝对路径。')
    service = input('systemd 服务名 [xray.service]：').strip() or 'xray.service'
    cfg = load(config)
    rules = cfg.get('routing', {}).get('rules', [])
    for i, rule in enumerate(rules):
        print(i, 'outbound=', rule.get('outboundTag', ''), 'fields=', ','.join(rule.keys()))
    print('Google 规则需位于 API/安全规则之后、兜底规则之前；不会改变默认出站。')
    index = int(input(f'插入位置（0 到 {len(rules)}）：')) if rules else 0
    sniff = input('对缺少嗅探配置的代理入站补充 routeOnly 嗅探？[y/N] ').lower() == 'y'
    plan = prepare(argparse.Namespace(config=config, service=service, rule_index=index,
                                      add_sniffing=sniff, domains=None))
    print('候选配置和域名列表已存放在', plan)
    if input('现在应用候选配置并重启 Xray？[y/N] ').lower() != 'y':
        print('尚未修改 Xray；之后可用 warp-xray apply --plan 路径。')
        return
    apply(argparse.Namespace(plan=str(plan)))
    if Path('/usr/local/bin/warp-google').exists():
        if input('备份并清理已识别的旧 warp-g.sh 透明代理设置？[y/N] ').lower() == 'y':
            cleanup(argparse.Namespace(plan=str(plan)))
    if input('启用 WARP 定时连通性检查及有限自动恢复？[y/N] ').lower() == 'y':
        enable_health(None)
    print('请用客户端测试 Google/非 Google 网站并确认双栈；服务启动成功不代表分流验证完成。')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    for cmd in ('install-warp', 'status', 'enable-health', 'watchdog', 'disable-health', 'setup'):
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
    args = p.parse_args()
    root()
    import fcntl
    # Serialize operator actions; watchdog uses its own lock to avoid nested locking.
    lock = open('/run/warp-xray-admin.lock', 'w')
    if args.command != 'watchdog':
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    actions = {'setup': setup, 'install-warp': install_warp, 'prepare': prepare, 'apply': apply,
               'restore': restore, 'export': export, 'cleanup-legacy': cleanup,
               'enable-health': enable_health, 'watchdog': watchdog, 'status': status,
               'disable-health': lambda _: run(['systemctl', 'disable', '--now', 'warp-xray-health.timer'])}
    actions[args.command](args)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, EOFError, subprocess.SubprocessError) as exc:
        print('失败：', exc, file=sys.stderr)
        sys.exit(1)
