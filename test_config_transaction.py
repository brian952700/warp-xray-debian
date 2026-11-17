import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('warp_xray', Path(__file__).with_name('warp-xray.py'))
wx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wx)


class ConfigTransactionTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.directory = Path(workspace.name)
        self.original = b'{"outbounds":[{"protocol":"freedom","tag":"direct"}]}\n'
        self.edited = b'{"outbounds":[{"protocol":"freedom","tag":"direct"}],"log":{}}\n'
        self.current = self.directory / 'config.json'
        self.current.write_bytes(self.original)
        self.args = argparse.Namespace(config=str(self.current), service='xray.service',
                                       rule_index=0, domains=None, add_sniffing=False)
        for patcher in (patch.object(wx, 'BASE', self.directory),
                        patch.object(wx, 'service_context', return_value='xray'),
                        patch.object(wx, 'validate'),
                        patch.object(wx.os, 'chown', create=True)):
            self.addCleanup(patcher.stop)
            patcher.start()

    def test_prepare_keeps_original_snapshot_after_external_edit(self):
        wx.validate.side_effect = lambda *_: self.current.write_bytes(self.edited)
        plan = wx.prepare(self.args)
        manifest = wx.load(plan / 'manifest.json')
        self.assertEqual((plan / 'original.json').read_bytes(), self.original)
        self.assertEqual(manifest['original_sha256'], wx.digest(plan / 'original.json'))
        self.assertNotEqual(manifest['original_sha256'], wx.digest(self.current))
        self.assertNotIn('log', wx.load(plan / 'candidate.json'))
        with self.assertRaisesRegex(ValueError, '运行配置已经变化'):
            wx.apply(argparse.Namespace(plan=str(plan)))
        self.assertEqual(self.current.read_bytes(), self.edited)

    def test_interactive_snapshot_rejects_changed_rules(self):
        self.args.expected_config = wx.load(self.current)
        self.current.write_bytes(self.edited)
        with self.assertRaisesRegex(ValueError, '选择规则期间已变更'):
            wx.prepare(self.args)
        self.assertEqual(list(self.directory.glob('plan-*')), [])

    def test_apply_rechecks_after_probe_and_rejects_changed_target(self):
        plan = wx.prepare(self.args)

        def probe():
            self.current.write_bytes(self.edited)
            return True, {}, ''

        with patch.object(wx, 'probe', side_effect=probe), patch.object(wx, 'restart_xray') as restart:
            with self.assertRaisesRegex(ValueError, '写入前已变更'):
                wx.apply(argparse.Namespace(plan=str(plan)))
        restart.assert_not_called()
        self.assertEqual(self.current.read_bytes(), self.edited)

    def test_apply_rejects_damaged_rollback_backup_before_restart(self):
        plan = wx.prepare(self.args)
        (plan / 'original.json').write_bytes(b'Damaged')
        with patch.object(wx, 'probe') as probe, patch.object(wx, 'restart_xray') as restart:
            with self.assertRaisesRegex(ValueError, '原始备份校验失败'):
                wx.apply(argparse.Namespace(plan=str(plan)))
        probe.assert_not_called()
        restart.assert_not_called()
        self.assertEqual(self.current.read_bytes(), self.original)

    def test_failed_start_uses_verified_rollback_bytes(self):
        plan = wx.prepare(self.args)

        with patch.object(wx, 'probe', return_value=(True, {}, '')), \
                patch.object(wx, 'restart_xray') as restart:
            def fail_once(*_):
                if restart.call_count == 1:
                    (plan / 'original.json').write_bytes(b'Damaged after validation')
                    raise RuntimeError('service failed')
            restart.side_effect = fail_once
            with self.assertRaisesRegex(ValueError, 'Xray 已重新启动'):
                wx.apply(argparse.Namespace(plan=str(plan)))
        self.assertEqual(self.current.read_bytes(), self.original)

    def test_restore_uses_actual_live_bytes_if_saved_candidate_is_damaged(self):
        plan = wx.prepare(self.args)
        candidate = (plan / 'candidate.json').read_bytes()
        self.current.write_bytes(candidate)
        (plan / 'candidate.json').write_bytes(b'Damaged')
        with patch.object(wx, 'restart_xray', side_effect=[RuntimeError('service failed'), None]):
            with self.assertRaisesRegex(ValueError, 'Xray 已重新启动'):
                wx.restore(argparse.Namespace(plan=str(plan)))
        wx.service_context.assert_any_call('xray.service', str(self.current.resolve()), allow_stopped=True)
        self.assertEqual(self.current.read_bytes(), candidate)


class StoppedServiceTests(unittest.TestCase):
    def test_stopped_service_resolves_effective_execstart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            executable = path / 'xray'
            executable.touch()
            conf = path / 'config with spaces.json'
            data = [[str(executable), [str(executable), 'run', '-config', str(conf)],
                     False, 0, 0, 0, 0, 0, 0, 0]]
            results = [
                subprocess.CompletedProcess([], 0, json.dumps(
                    {'type': 'o', 'data': ['/org/freedesktop/systemd1/unit/xray_2eservice']}), ''),
                subprocess.CompletedProcess([], 0, json.dumps({'type': 'a(sasbttttuii)', 'data': data}), ''),
            ]
            with patch.object(wx, 'service_properties', side_effect=[
                    {'MainPID': '0', 'ActiveState': 'failed'},
                    {'LoadState': 'loaded', 'WorkingDirectory': str(path)}]), \
                    patch.object(wx, 'run', side_effect=results):
                self.assertEqual(wx.service_context('xray.service', conf, allow_stopped=True),
                                 str(executable.resolve()))

    def test_stopped_service_rejects_namespace_or_env_indirection(self):
        for setting in ('EnvironmentFiles', 'BindPaths', 'RootImage'):
            with self.subTest(setting=setting), patch.object(wx, 'service_properties',
                    return_value={'LoadState': 'loaded', setting: '/custom/path'}):
                with self.assertRaisesRegex(ValueError, '无法确认配置路径'):
                    wx.stopped_service_context('xray.service', '/etc/xray/config.json')


if __name__ == '__main__':
    unittest.main()
