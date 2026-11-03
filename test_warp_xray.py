import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import subprocess
import contextlib
import io

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

    def test_setup_existing_config_stops_before_insertion_prompt(self):
        cfg = wx.patch_config(self.cfg, wx.DOMAINS, 2)
        with patch('builtins.input', side_effect=['n', 'n', str(ROOT / 'config.json'), '', 'n']) as ask, \
                patch.object(wx, 'probe', return_value=(True, {}, '')), \
                patch.object(wx, 'load', return_value=cfg), \
                patch.object(wx, 'prepare') as prepare, patch.object(wx, 'apply') as apply:
            wx.setup(None)
            prepare.assert_not_called()
            apply.assert_not_called()
            self.assertFalse(any('插入位置' in call.args[0] for call in ask.call_args_list))

    def test_prepare_existing_rules_refuses_before_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'
            # Rule-only remnants also block an automatic insertion.
            wx.save(path, {'routing': {'rules': [{'outboundTag': wx.TAG}]}})
            with patch.object(wx, 'service_context') as service:
                with self.assertRaisesRegex(ValueError, '重复配置'):
                    wx.prepare(type('Args', (), {'config': str(path)})())
                service.assert_not_called()

    def test_directory_path_error_is_actionable(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, '不能填写目录'):
                wx.load(temp)

    def test_output_has_no_ansi_when_redirected(self):
        output = io.StringIO()
        with patch.object(wx.sys, 'stdout', output):
            wx.ui.message('ok', '配置已完成')
        self.assertIn('[成功]', output.getvalue())
        self.assertNotIn('\033[', output.getvalue())

    def test_no_color_environment_disables_tty_colors(self):
        output = io.StringIO()
        with patch.object(output, 'isatty', return_value=True), \
                patch.object(wx.sys, 'stdout', output), patch.dict(wx.os.environ, {'NO_COLOR': ''}):
            wx.ui.message('warn', '请检查')
        self.assertNotIn('\033[', output.getvalue())

    def test_invalid_route_index_can_be_corrected(self):
        with patch.object(wx.ui, 'ask', side_effect=['foo', '-1', '99', '2']) as ask:
            self.assertEqual(wx.ask_rule_index(self.cfg['routing']['rules']), 2)
            self.assertEqual(ask.call_count, 4)

    def test_directory_config_input_can_be_corrected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'
            wx.save(path, self.cfg)
            with patch.object(wx.ui, 'ask', side_effect=[temp, str(path)]) as ask:
                config, cfg = wx.ask_config()
            self.assertEqual(config, str(path))
            self.assertEqual(cfg, self.cfg)
            self.assertEqual(ask.call_count, 2)

    def test_service_full_path_is_normalized(self):
        with patch.object(wx.ui, 'ask', return_value='/etc/systemd/system/xray.service'):
            self.assertEqual(wx.ask_service(), 'xray.service')

    def test_menu_exit_never_changes_system(self):
        with patch.object(wx.ui, 'ask', return_value='0'), patch.object(wx, 'setup') as setup, \
                patch.object(wx, 'uninstall') as uninstall, patch.object(wx, 'run') as run:
            wx.menu(None)
            setup.assert_not_called()
            uninstall.assert_not_called()
            run.assert_not_called()

    def test_hidden_command_failure_still_displays_diagnostics(self):
        output = io.StringIO()
        exc = subprocess.CalledProcessError(1, ['xray'], output='invalid config')
        with patch.object(wx.sys, 'stdout', output):
            wx.report_error(exc)
        self.assertIn('invalid config', output.getvalue())
        self.assertIn('[失败]', output.getvalue())

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


class UninstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / 'state'
        self.plan = self.state / 'plan-first'
        self.plan.mkdir(parents=True)
        self.config = self.root / 'config.json'
        self.original = {'outbounds': [{'protocol': 'freedom', 'tag': 'direct'}]}
        self.candidate = wx.patch_config(self.original, wx.DOMAINS, 0)
        wx.save(self.plan / 'original.json', self.original)
        wx.save(self.plan / 'candidate.json', self.candidate)
        wx.save(self.config, self.candidate)
        self.manifest = {'config': str(self.config), 'service': 'xray.service',
                         'original_sha256': wx.digest(self.plan / 'original.json'),
                         'candidate_sha256': wx.digest(self.plan / 'candidate.json')}
        wx.save(self.plan / 'manifest.json', self.manifest)
        (self.plan / 'applied').write_text('applied')
        self.install = self.root / 'installed'
        self.install.mkdir()
        (self.install / 'warp-xray.py').write_text('test')
        self.wrapper = self.root / 'warp-xray'
        self.wrapper.write_text('exec /usr/local/lib/warp-xray/warp-xray.py')
        self.archives = self.root / 'archives'
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in {'BASE': self.state, 'ARCHIVES': self.archives,
                            'INSTALL_ROOT': self.install, 'WRAPPER': self.wrapper,
                            'HEALTH_UNITS': [self.root / 'warp-xray-health.timer', self.root / 'warp-xray-health.service'],
                            'WARP_REPO': self.root / 'cloudflare-client.list',
                            'WARP_KEY': self.root / 'cloudflare-key.gpg',
                            'WARP_DIRS': [self.root / 'warp-state', self.root / 'warp-log']}.items():
            self.stack.enter_context(patch.object(wx, name, value))
        self.stack.enter_context(patch.object(wx, 'service_context', return_value='xray'))
        self.stack.enter_context(patch.object(wx, 'validate'))
        self.stack.enter_context(patch.object(wx.os, 'chown', create=True))
        self.stack.enter_context(patch.object(wx.shutil, 'which', return_value=None))

    def args(self):
        return type('Args', (), {'plan': None})()

    def test_automatic_plan_works_for_old_installations(self):
        self.assertEqual(wx.select_uninstall_plan()[0], self.plan)

    def test_missing_backup_stops(self):
        (self.plan / 'original.json').unlink()
        with self.assertRaises(ValueError):
            wx.select_uninstall_plan()

    def test_corrupted_backup_stops(self):
        (self.plan / 'original.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, '哈希'):
            wx.checked_uninstall_plan(self.plan)

    def test_modified_live_config_stops(self):
        self.config.write_text('{"later":"edit"}')
        with self.assertRaisesRegex(ValueError, '后续修改'):
            wx.checked_uninstall_plan(self.plan)

    def test_backup_with_managed_rules_stops(self):
        wx.save(self.plan / 'original.json', self.candidate)
        self.manifest['original_sha256'] = wx.digest(self.plan / 'original.json')
        wx.save(self.plan / 'manifest.json', self.manifest)
        with self.assertRaisesRegex(ValueError, '不是可恢复'):
            wx.checked_uninstall_plan(self.plan)

    def test_multiple_live_configurations_are_not_chosen_arbitrarily(self):
        other = self.state / 'plan-second'
        other.mkdir()
        for name in ('original.json', 'candidate.json'):
            (other / name).write_bytes((self.plan / name).read_bytes())
        m = dict(self.manifest)
        m['config'] = str(self.root / 'other.json')
        wx.save(m['config'], self.candidate)
        wx.save(other / 'manifest.json', m)
        (other / 'applied').write_text('applied')
        with self.assertRaisesRegex(ValueError, '唯一'):
            wx.select_uninstall_plan()
        with self.assertRaisesRegex(ValueError, '另一个配置'):
            wx.preflight_uninstall(self.plan, self.manifest)

    def test_cancel_does_not_restore_or_remove(self):
        with patch('builtins.input', return_value='n'), patch.object(wx, 'preflight_uninstall'), \
                patch.object(wx, 'restore_for_uninstall') as restore, \
                patch.object(wx, 'remove_uninstall_components') as remove:
            wx.uninstall(self.args())
            restore.assert_not_called()
            remove.assert_not_called()
            self.assertFalse(self.archives.exists())

    def test_success_restores_before_removal_and_archives_backups(self):
        def remove():
            self.assertEqual(wx.digest(self.config), self.manifest['original_sha256'])
        with patch('builtins.input', return_value='UNINSTALL'), patch.object(wx, 'preflight_uninstall'), \
                patch.object(wx, 'restart_xray'), patch.object(wx, 'remove_uninstall_components', side_effect=remove):
            wx.uninstall(self.args())
        self.assertEqual(wx.load(self.config), self.original)
        self.assertFalse(self.wrapper.exists())
        self.assertFalse(self.install.exists())
        self.assertFalse(self.state.exists())
        archive = next(self.archives.iterdir())
        self.assertEqual(wx.load(archive / 'state' / self.plan.name / 'original.json'), self.original)
        self.assertEqual(wx.load(archive / 'uninstall.json')['stage'], 'complete')

    def test_restore_failure_keeps_warp_and_tools(self):
        with patch('builtins.input', return_value='UNINSTALL'), patch.object(wx, 'preflight_uninstall'), \
                patch.object(wx, 'restart_xray', side_effect=[RuntimeError('start failed'), None]), \
                patch.object(wx, 'remove_uninstall_components') as remove:
            with self.assertRaises(ValueError):
                wx.uninstall(self.args())
            remove.assert_not_called()
        self.assertEqual(wx.load(self.config), self.candidate)
        self.assertTrue(self.wrapper.exists())
        self.assertTrue(self.state.exists())

    def test_package_failure_leaves_restored_config_and_retryable_tools(self):
        with patch('builtins.input', return_value='UNINSTALL'), patch.object(wx, 'preflight_uninstall'), \
                patch.object(wx, 'restart_xray'), \
                patch.object(wx, 'remove_uninstall_components', side_effect=RuntimeError('apt failed')):
            with self.assertRaises(RuntimeError):
                wx.uninstall(self.args())
        self.assertEqual(wx.load(self.config), self.original)
        self.assertTrue(self.wrapper.exists())
        self.assertEqual(wx.select_uninstall_plan()[0], self.plan)

    def test_already_restored_file_still_restarts_live_xray(self):
        wx.save(self.config, self.original)
        with patch.object(wx, 'restart_xray') as restart:
            wx.restore_for_uninstall(self.plan, self.manifest)
            restart.assert_called_once_with('xray.service')

    def test_unknown_health_unit_blocks_removal(self):
        wx.HEALTH_UNITS[0].write_text('[Timer]\nOnBootSec=1min\n')
        with self.assertRaisesRegex(ValueError, '单元内容不符'):
            wx.preflight_uninstall(self.plan, self.manifest)

    def test_original_health_unit_formats_pass_preflight(self):
        wx.HEALTH_UNITS[0].write_text(
            '[Unit]\nDescription=Periodic WARP proxy health check\n'
            '[Timer]\nOnUnitActiveSec=1min\n')
        wx.HEALTH_UNITS[1].write_text(
            '[Service]\nExecStart=/usr/bin/python3 /usr/local/lib/warp-xray/warp-xray.py watchdog\n')
        wx.preflight_uninstall(self.plan, self.manifest)

    def test_shared_source_key_blocks_removal(self):
        wx.WARP_KEY.write_text('key')
        (self.root / 'another.sources').write_text('Signed-By: ' + str(wx.WARP_KEY))
        with self.assertRaisesRegex(ValueError, '其他 APT 源'):
            wx.preflight_uninstall(self.plan, self.manifest)

    def test_remove_components_purges_only_warp_without_autoremove(self):
        wx.WARP_REPO.write_text('deb https://pkg.cloudflareclient.com/ trixie main')
        wx.WARP_KEY.write_text('key')
        for path in wx.WARP_DIRS:
            path.mkdir()
            (path / 'state').write_text('state')
        with patch.object(wx, 'stop_health') as stop, \
                patch.object(wx, 'run', return_value=subprocess.CompletedProcess([], 0, 'loaded\n', '')) as run, \
                patch.object(wx, 'apt') as apt:
            wx.remove_uninstall_components()
            stop.assert_called_once()
            apt.assert_called_once_with(['purge', '-y', 'cloudflare-warp'])
            self.assertTrue(any(c.args[0] == ['systemctl', 'disable', '--now', 'warp-svc.service'] for c in run.call_args_list))
        self.assertFalse(wx.WARP_REPO.exists())
        self.assertFalse(wx.WARP_KEY.exists())
        self.assertTrue(all(not p.exists() for p in wx.WARP_DIRS))
        self.assertTrue(self.wrapper.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
