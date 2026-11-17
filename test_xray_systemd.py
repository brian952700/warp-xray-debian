"""Exercise real Xray and systemd with a disposable loopback configuration."""

from argparse import Namespace
import os
from pathlib import Path
import runpy
import socket
import subprocess
import sys
import tempfile
from unittest.mock import patch
from uuid import uuid4


def systemctl(*args, check=True):
    return subprocess.run(['systemctl', *args], check=check, timeout=20)


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.geteuid() != 0:
        raise SystemExit('Run only as root in the GitHub Actions systemd test job.')
    binary = Path(sys.argv[1]).resolve(strict=True)
    helpers = runpy.run_path('warp-xray.py')
    unit_name = f'warp-xray-test-{uuid4().hex}.service'
    unit = Path('/run/systemd/system') / unit_name

    # Keep this port reserved until the failed Xray candidate has rolled back.
    with tempfile.TemporaryDirectory(prefix='warp-xray test ', dir='/run') as temp, socket.socket() as collision:
        work = Path(temp)
        collision.bind(('127.0.0.1', 0))
        collision.listen()
        with socket.socket() as original_port:
            original_port.bind(('127.0.0.1', 0))
            port = original_port.getsockname()[1]
        live = work / 'config.json'
        helpers['save'](live, {
            'inbounds': [{'listen': '127.0.0.1', 'port': port,
                          'protocol': 'socks', 'settings': {'auth': 'noauth'}}],
            'outbounds': [{'tag': 'direct', 'protocol': 'freedom'}],
        })
        original_bytes = live.read_bytes()
        base = work / 'plans'
        base.mkdir()
        setup = Namespace(config=live, service=unit_name, rule_index=0, domains=None, add_sniffing=True)
        try:
            unit.write_text(f'[Service]\nType=simple\nWorkingDirectory=/run\n'
                            f'ExecStart="{binary}" run -config "{live}"\n')
            systemctl('daemon-reload')
            helpers['restart_xray'](unit_name)
            with patch.dict(helpers['apply'].__globals__, BASE=base,
                            probe=lambda: (True, {'warp': 'on'}, '')):
                failed_plan = helpers['prepare'](setup)
                candidate_path = failed_plan / 'candidate.json'
                candidate = helpers['load'](candidate_path)
                candidate['inbounds'][0]['port'] = collision.getsockname()[1]
                helpers['save'](candidate_path, candidate)
                manifest_path = failed_plan / 'manifest.json'
                manifest = helpers['load'](manifest_path)
                manifest['candidate_sha256'] = helpers['digest'](candidate_path)
                helpers['save'](manifest_path, manifest)
                # A conflict passes Xray's syntax check and fails during start.
                helpers['validate'](binary, candidate_path)
                try:
                    helpers['apply'](Namespace(plan=failed_plan))
                except ValueError as exc:
                    if '已恢复操作前的配置' not in str(exc):
                        raise
                else:
                    raise AssertionError('A conflicting listen port should have failed to start Xray')
                assert live.read_bytes() == original_bytes
                assert not (failed_plan / 'applied').exists()
                systemctl('is-active', '--quiet', unit_name)

                good_plan = helpers['prepare'](setup)
                helpers['apply'](Namespace(plan=good_plan))
                assert live.read_bytes() == (good_plan / 'candidate.json').read_bytes()
                systemctl('stop', unit_name)
                assert helpers['service_properties'](unit_name, 'MainPID')['MainPID'] == '0'
                helpers['restore'](Namespace(plan=good_plan))
                assert live.read_bytes() == original_bytes
                systemctl('is-active', '--quiet', unit_name)
        except Exception:
            subprocess.run(['journalctl', '-u', unit_name, '-n', '30', '--no-pager'], timeout=10)
            raise
        finally:
            systemctl('stop', unit_name, check=False)
            unit.unlink(missing_ok=True)
            systemctl('daemon-reload')


if __name__ == '__main__':
    main()
