import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import subprocess

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('wx', ROOT / 'warp-xray.py')
wx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wx)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {'inbounds': [{'protocol': 'vless', 'settings': {'clients': [{'id': 'secret'}]}}],
                    'outbounds': [{'tag': 'direct', 'protocol': 'freedom'},
                                  {'tag': 'deny', 'protocol': 'blackhole'}],
                    'routing': {'domainStrategy': 'AsIs', 'rules': [
                        {'type': 'field', 'inboundTag': ['api'], 'outboundTag': 'api'},
                        {'type': 'field', 'ip': ['geoip:private'], 'outboundTag': 'deny'},
                        {'type': 'field', 'network': 'tcp,udp', 'outboundTag': 'direct'}]}}

    def test_preserve_default_order_secrets_original(self):
        before = json.dumps(self.cfg)
        out = wx.patch_config(self.cfg, wx.DOMAINS, 2)
        self.assertEqual(json.dumps(self.cfg), before)
        self.assertEqual(out['outbounds'][:2], self.cfg['outbounds'])
        self.assertEqual(out['inbounds'], self.cfg['inbounds'])
        self.assertEqual(out['routing']['rules'][:2], self.cfg['routing']['rules'][:2])
        self.assertEqual(out['routing']['rules'][-1], self.cfg['routing']['rules'][-1])
        self.assertEqual(out['routing']['rules'][2]['outboundTag'], wx.BLOCK)
        self.assertEqual(out['routing']['rules'][3]['outboundTag'], wx.TAG)
        self.assertEqual(out['routing']['domainStrategy'], 'AsIs')

    def test_sniff_only_missing(self):
        existing = {'enabled': False, 'destOverride': ['tls']}
        self.cfg['inbounds'] += [{'protocol': 'trojan', 'sniffing': existing}, {'protocol': 'dokodemo-door'}]
        out = wx.patch_config(self.cfg, wx.DOMAINS, 2, True)
        self.assertTrue(out['inbounds'][0]['sniffing']['routeOnly'])
        self.assertEqual(out['inbounds'][1]['sniffing'], existing)
        self.assertNotIn('sniffing', out['inbounds'][2])

    def test_reject_no_default(self):
        with self.assertRaises(ValueError):
            wx.patch_config({}, wx.DOMAINS, 0)

    def test_reject_collision_and_bad_index(self):
        out = wx.patch_config(self.cfg, wx.DOMAINS, 0)
        with self.assertRaises(ValueError):
            wx.patch_config(out, wx.DOMAINS, 0)
        for index in (-1, 4):
            with self.assertRaises(ValueError):
                wx.patch_config(self.cfg, wx.DOMAINS, index)

    def test_current_socks(self):
        out = wx.patch_config(self.cfg, wx.DOMAINS, 0, socks_style='current')
        self.assertEqual(out['outbounds'][2]['settings'], {'address': '127.0.0.1', 'port': 40000})

    def test_package_operations_have_no_short_timeout(self):
        with patch.object(wx, 'run') as run:
            wx.apt(['install', '-y', 'cloudflare-warp'])
            self.assertEqual(run.call_args.kwargs['timeout'], None)
            self.assertEqual(run.call_args.args[0], [
                'apt-get', '-o', 'DPkg::Lock::Timeout=300',
                'install', '-y', 'cloudflare-warp'])

    def test_network_probes_keep_short_timeout(self):
        with patch.object(wx, 'run', return_value=subprocess.CompletedProcess([], 0, 'warp=on\n', '')) as run:
            wx.probe()
            self.assertEqual(run.call_args.kwargs['timeout'], 18)

    def test_setup_decline_never_applies(self):
        with patch('builtins.input', side_effect=['n', 'n', str(ROOT / 'config.json'), '', '2', 'n', 'n']), \
                patch.object(wx, 'probe', return_value=(True, {}, '')), \
                patch.object(wx, 'load', return_value=self.cfg), \
                patch.object(wx, 'prepare', return_value=Path('/var/lib/warp-xray/plan-test')), \
                patch.object(wx, 'apply') as apply, patch.object(wx, 'install_warp') as install:
            wx.setup(None)
            apply.assert_not_called()
            install.assert_not_called()

    def test_setup_panel_exports_without_config_write(self):
        with patch('builtins.input', side_effect=['n', 'y', '/root/fragments']), \
                patch.object(wx, 'probe', return_value=(True, {}, '')), \
                patch.object(wx, 'export') as export, patch.object(wx, 'prepare') as prepare:
            wx.setup(None)
            export.assert_called_once()
            prepare.assert_not_called()

    def test_reject_duplicate_keys_jsonc(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'
            for content in ('{"outbounds":[],"outbounds":[{}]}', '{/*comment*/"a":1}'):
                path.write_text(content)
                with self.assertRaises(ValueError):
                    wx.load(path)

    def test_domain_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'domains.txt'
            args = type('Args', (), {'domains': str(path)})()
            path.write_text('# hi\nGoogle.com\ngoogle.com\nyoutube.com\n')
            self.assertEqual(wx.domains_for(args), ['google.com', 'youtube.com'])
            for value in ('*.google.com', 'https://google.com', 'a..com', 'domain:google.com'):
                path.write_text(value)
                with self.assertRaises(ValueError):
                    wx.domains_for(args)

    def test_probe_requires_real_warp_and_success(self):
        for code, output, expected in [(0, 'ip=1.2.3.4\nwarp=on\n', True),
                                       (0, 'warp=off\n', False), (1, 'warp=on\n', False),
                                       (0, 'warp=plus\n', True), (0, 'HTTP error', False)]:
            with patch.object(wx, 'run', return_value=subprocess.CompletedProcess([], code, output, '')):
                self.assertEqual(wx.probe()[0], expected)

    def test_proxy_family_is_destination_not_curl_switch(self):
        with patch.object(wx, 'run', return_value=subprocess.CompletedProcess([], 0, 'warp=on\n', '')) as r:
            wx.probe('ipv6')
            argv = r.call_args.args[0]
            self.assertEqual(argv[-1], 'https://ipv6.cloudflare.com/cdn-cgi/trace')
            self.assertNotIn('-6', argv)
            self.assertIn('socks5h://127.0.0.1:40000', argv)

    def test_apply_rollback_and_hash_guards(self):
        with tempfile.TemporaryDirectory() as temp:
            plan = Path(temp)
            current = plan / 'live.json'
            current.write_text('{"original":true}')
            (plan / 'original.json').write_bytes(current.read_bytes())
            (plan / 'candidate.json').write_text('{"changed":true}')
            wx.save(plan / 'manifest.json', {'config': str(current), 'service': 'xray.service',
                                           'original_sha256': wx.digest(current),
                                           'candidate_sha256': wx.digest(plan / 'candidate.json')})
            args = type('Args', (), {'plan': str(plan)})()
            with patch.object(wx, 'service_context', return_value='xray'), \
                    patch.object(wx, 'validate'), patch.object(wx, 'probe', return_value=(True, {}, '')), \
                    patch.object(wx, 'restart_xray', side_effect=[RuntimeError('failed'), None]), \
                    patch.object(wx.os, 'chown', create=True):
                with self.assertRaises(ValueError):
                    wx.apply(args)
            self.assertEqual(current.read_bytes(), (plan / 'original.json').read_bytes())
            self.assertFalse((plan / 'applied').exists())
            current.write_text('{"someone_else_changed_it":true}')
            with self.assertRaises(ValueError):
                wx.apply(args)

    def test_apply_restore_roundtrip_and_refuse_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            plan = Path(temp)
            current = plan / 'live.json'
            current.write_text('{"original":true}')
            (plan / 'original.json').write_bytes(current.read_bytes())
            (plan / 'candidate.json').write_text('{"changed":true}')
            wx.save(plan / 'manifest.json', {'config': str(current), 'service': 'xray.service',
                                           'original_sha256': wx.digest(current),
                                           'candidate_sha256': wx.digest(plan / 'candidate.json')})
            args = type('Args', (), {'plan': str(plan)})()
            with patch.object(wx, 'service_context', return_value='xray'), patch.object(wx, 'validate'), \
                    patch.object(wx, 'probe', return_value=(True, {}, '')), \
                    patch.object(wx, 'restart_xray'), patch.object(wx.os, 'chown', create=True):
                wx.apply(args)
                self.assertTrue((plan / 'applied').exists())
                self.assertEqual(current.read_bytes(), (plan / 'candidate.json').read_bytes())
                wx.restore(args)
                self.assertEqual(current.read_bytes(), (plan / 'original.json').read_bytes())
            with self.assertRaises(ValueError):
                wx.restore(args)


if __name__ == '__main__':
    unittest.main(verbosity=2)
