import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import call, patch

from test_warp_xray import wx


FAILURE = (False, {}, '检测超时')
HEALTHY = (True, {'warp': 'on'}, '')


class HealthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.state = self.directory / 'health-state.json'
        self.timer = self.directory / 'warp-xray-health.timer'
        self.unit = self.directory / 'warp-xray-health.service'
        self.wrapper = self.directory / 'warp-xray'
        self.wrapper.touch()
        patches = contextlib.ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(patch.object(wx, 'HEALTH_UNITS', [self.timer, self.unit]))
        patches.enter_context(patch.object(wx, 'WRAPPER', self.wrapper))
        patches.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def test_three_failures_required_and_success_resets_count(self):
        with patch.object(wx, 'probe', side_effect=[FAILURE, FAILURE, HEALTHY]) as probe:
            wx.check_warp_health(self.state)
            self.assertEqual(wx.load(self.state)['failures'], 1)
            wx.check_warp_health(self.state)
            self.assertEqual(wx.load(self.state)['failures'], 2)
            wx.check_warp_health(self.state)
            self.assertEqual(wx.load(self.state)['failures'], 0)
        self.assertEqual(probe.call_args_list, [call(timeout=8)] * 3)

    def test_fallback_success_avoids_restart(self):
        wx.save(self.state, {'failures': 2, 'last_restart': 0})
        with patch.object(wx, 'probe', side_effect=[FAILURE, HEALTHY]) as probe, \
                patch.object(wx, 'recover_warp') as recover, patch.object(wx, 'run') as run:
            wx.check_warp_health(self.state)
        self.assertEqual(probe.call_args_list, [call(timeout=8), call(timeout=6, url=wx.HEALTH_FALLBACK_URL)])
        recover.assert_not_called()
        run.assert_not_called()
        self.assertEqual(wx.load(self.state)['failures'], 0)

    def test_reconnect_success_avoids_daemon_restart(self):
        wx.save(self.state, {'failures': 2, 'last_restart': 0})
        with patch.object(wx, 'probe', return_value=FAILURE), \
                patch.object(wx, 'recover_warp', return_value=True) as recover, patch.object(wx, 'run') as run:
            wx.check_warp_health(self.state)
        recover.assert_called_once_with()
        run.assert_not_called()
        self.assertEqual(wx.load(self.state), {'failures': 0, 'last_restart': 0})

    def test_restart_uses_bounded_reconnect_and_verifies_recovery(self):
        wx.save(self.state, {'failures': 2, 'last_restart': 0})
        with patch.object(wx, 'probe', return_value=FAILURE), \
                patch.object(wx, 'recover_warp', side_effect=[False, True]) as recover, \
                patch.object(wx, 'run') as run, patch.object(wx.time, 'time', return_value=1000):
            wx.check_warp_health(self.state)
        self.assertEqual(recover.call_args_list, [call(), call(connect_seconds=12)])
        run.assert_called_once_with(['systemctl', 'restart', 'warp-svc.service'], timeout=20)
        self.assertEqual(wx.load(self.state), {'failures': 0, 'last_restart': 1000})

    def test_timed_out_restart_is_rate_limited(self):
        wx.save(self.state, {'failures': 2, 'last_restart': 0})
        with patch.object(wx, 'probe', return_value=FAILURE), \
                patch.object(wx, 'recover_warp', return_value=False), \
                patch.object(wx, 'run', side_effect=subprocess.TimeoutExpired('systemctl', 20)) as run, \
                patch.object(wx.time, 'time', side_effect=[1000, 1000, 1060]):
            with self.assertRaises(subprocess.TimeoutExpired):
                wx.check_warp_health(self.state)
            wx.check_warp_health(self.state)
        run.assert_called_once()
        self.assertEqual(wx.load(self.state), {'failures': 3, 'last_restart': 1000})

    def test_corrupt_state_can_recover(self):
        self.state.write_text('{"failures":')
        with patch.object(wx, 'probe', return_value=HEALTHY):
            wx.check_warp_health(self.state)
        self.assertEqual(wx.load(self.state), {'failures': 0, 'last_restart': 0})

    def test_atomic_recovery_write_preserves_previous_file_on_failure(self):
        self.state.write_text('original', encoding='utf-8')
        with patch.object(wx.os, 'replace', side_effect=OSError('cannot replace')):
            with self.assertRaises(OSError):
                wx.write_health_file(self.state, 'replacement')
        self.assertEqual(self.state.read_text(), 'original')
        self.assertEqual(list(self.directory.glob('.health-state.json-*')), [])

    def test_reconnect_waits_through_daemon_startup(self):
        with patch.object(wx.time, 'monotonic', side_effect=[0, 0, 2, 3]), \
                patch.object(wx.time, 'sleep'), \
                patch.object(wx, 'warp', side_effect=[OSError('not ready'), None]) as connect, \
                patch.object(wx, 'wait_for_health', return_value=True) as verify:
            self.assertTrue(wx.recover_warp(connect_seconds=12))
        self.assertEqual(connect.call_count, 2)
        verify.assert_called_once_with(10)

    def test_reconnect_can_verify_on_alternate_endpoint(self):
        with patch.object(wx.time, 'monotonic', side_effect=[0, 0, 6, 7]), \
                patch.object(wx.time, 'sleep'), \
                patch.object(wx, 'probe', side_effect=[FAILURE, HEALTHY]) as probe:
            self.assertTrue(wx.wait_for_health(10))
        self.assertEqual(probe.call_args_list, [call(timeout=6, url=None),
                                                call(timeout=3, url=wx.HEALTH_FALLBACK_URL)])

    def test_migration_preserves_other_settings_and_does_not_enable_timer(self):
        self.unit.write_text('[Service]\n' + wx.LEGACY_HEALTH_EXEC + '\nEnvironment=NO_COLOR=1\n')
        with patch.object(wx, 'run') as run:
            wx.update_health(None)
            wx.update_health(None)
        self.assertIn(wx.HEALTH_EXEC, self.unit.read_text().splitlines())
        self.assertIn('Environment=NO_COLOR=1', self.unit.read_text().splitlines())
        self.assertTrue(wx.owns_health_unit(self.unit))
        self.assertFalse(self.timer.exists())
        run.assert_called_once_with(['systemctl', 'daemon-reload'], timeout=10)

    def test_migration_rolls_back_unit_when_reload_fails(self):
        content = '[Service]\n' + wx.LEGACY_HEALTH_EXEC + '\n'
        self.unit.write_text(content)
        with patch.object(wx, 'run', side_effect=OSError('systemd unavailable')):
            with self.assertRaises(OSError):
                wx.update_health(None)
        self.assertEqual(self.unit.read_text(), content)

    def test_pause_stops_timer_and_inflight_job(self):
        self.timer.touch()
        self.unit.touch()
        with patch.object(wx, 'run') as run:
            wx.disable_health(None)
        self.assertEqual(run.call_args_list, [
            call(['systemctl', 'disable', '--now', self.timer.name]),
            call(['systemctl', 'stop', self.unit.name]),
        ])


if __name__ == '__main__':
    unittest.main()
