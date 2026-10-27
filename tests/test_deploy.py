"""Deployment guards and rollback without Docker, GitHub credentials or live users."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("deploy", ROOT / "server" / "deploy.py")
assert spec and spec.loader
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)
probe_spec = importlib.util.spec_from_file_location("deploy_probe", ROOT / "server" / "deploy_probe.py")
assert probe_spec and probe_spec.loader
probe = importlib.util.module_from_spec(probe_spec)
probe_spec.loader.exec_module(probe)


class Deploy(unittest.TestCase):
    def test_gate_requires_exact_successful_github_actions_check(self):
        for checks in ([], [{"name": "full-suite", "conclusion": "failure", "app": {"slug": "github-actions"}}],
                       [{"name": "full-suite", "conclusion": "success", "app": {"slug": "other"}}]):
            with mock.patch.object(deploy, "github", return_value={"check_runs": checks}):
                self.assertFalse(deploy.ci_passed("a" * 40))
        with mock.patch.object(deploy, "github", return_value={"check_runs": [
                {"name": "full-suite", "status": "completed", "conclusion": "success",
                 "head_sha": "a" * 40, "app": {"slug": "github-actions"}}]}):
            self.assertTrue(deploy.ci_passed("a" * 40))

    def test_continuity_requires_all_registrations_and_previously_connected_users(self):
        baseline = {"registrations": ["a", "b"], "connected": ["a"]}
        self.assertTrue(deploy.continuity(baseline, baseline))
        self.assertTrue(deploy.continuity(baseline, {"registrations": ["a", "b", "c"], "connected": ["a"]}))
        self.assertFalse(deploy.continuity(baseline, {"registrations": ["a"], "connected": ["a"]}))
        self.assertFalse(deploy.continuity(baseline, {"registrations": ["a", "b"], "connected": []}))

    def test_newer_failed_rerun_blocks_older_success(self):
        checks = [{"id": 1, "name": "full-suite", "status": "completed", "conclusion": "success",
                   "head_sha": "a" * 40, "app": {"slug": "github-actions"}},
                  {"id": 2, "name": "full-suite", "status": "completed", "conclusion": "failure",
                   "head_sha": "a" * 40, "app": {"slug": "github-actions"}}]
        with mock.patch.object(deploy, "github", return_value={"check_runs": checks}):
            self.assertFalse(deploy.ci_passed("a" * 40))

    def test_interrupted_backup_unpauses_original_and_retains_volume(self):
        journal = {"old": "old-id", "sha": "a" * 40, "previous": "b" * 40, "baseline": {}}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(deploy, "command") as command, \
                mock.patch.object(deploy, "wait_healthy"):
            command.return_value = json.dumps([{"Id": "old-id", "Name": "/livechat-xr-relay", "State": {"Paused": True}}])
            deploy.rollback(journal, Path(tmp))
            command.assert_any_call("docker", "unpause", "old-id")
            command.assert_any_call("docker", "start", "old-id")
            self.assertEqual((Path(tmp) / "failed").read_text(), "a" * 40)
            self.assertEqual((Path(tmp) / "deployed").read_text(), "b" * 40)

    def test_health_failure_restores_original_container_without_restoring_live_data(self):
        calls = []
        def command(*args):
            calls.append(args)
            if args[:2] == ("docker", "inspect"):
                return json.dumps([{"Id": args[2], "Name": "previous" if args[2] == "old-id" else "livechat-xr-relay", "State": {"Paused": False}}])
            return ""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(deploy, "command", side_effect=command), \
                mock.patch.object(deploy, "wait_healthy", side_effect=[RuntimeError("unhealthy"), None]):
            with self.assertRaisesRegex(RuntimeError, "rolled back"):
                deploy.swap("a" * 40, "sha256:candidate", "old-id", {"registrations": ["a"], "connected": ["a"]}, Path(tmp))
        self.assertIn(("docker", "start", "old-id"), calls)
        self.assertNotIn(("docker", "rm", "old-id"), calls)
        self.assertFalse(any("volume" in c or "extract" in c for c in calls))

    def test_success_keeps_original_container_for_rollback(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(deploy, "command") as command, \
                mock.patch.object(deploy, "wait_healthy"):
            deploy.swap("a" * 40, "sha256:candidate", "old-id", {"registrations": ["a"], "connected": ["a"]}, Path(tmp))
        command.assert_any_call("docker", "rename", "old-id", "livechat-xr-relay-previous-aaaaaaaaaaaa")
        self.assertFalse(any(c.args[:2] == ("docker", "rm") for c in command.call_args_list))

    def test_pre_swap_failure_does_not_remove_current_container(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(deploy, "command", side_effect=RuntimeError("stop failed")) as command:
            with self.assertRaisesRegex(RuntimeError, "stop failed"):
                deploy.swap("a" * 40, "sha256:candidate", "old-id", {}, Path(tmp))
        self.assertEqual(command.call_args_list, [mock.call("docker", "stop", "--time", "30", "old-id")])

    def test_runtime_preflight_rejects_configuration_drift(self):
        good = {"Id": "old", "HostConfig": {"NetworkMode": "livechat-xr", "RestartPolicy": {"Name": "unless-stopped"},
                "PortBindings": {"13300/tcp": [{"HostIp": "192.168.1.240", "HostPort": "13300"}]}},
                "Mounts": [{"Name": "livechat-xr-data", "Destination": "/data", "RW": True}]}
        deploy.validate_container(good)
        good["HostConfig"]["NetworkMode"] = "host"
        with self.assertRaisesRegex(RuntimeError, "configuration drift"):
            deploy.validate_container(good)

    def test_failed_commit_is_not_retried_automatically(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / "failed").write_text("a" * 40)
            with mock.patch.object(deploy, "target", return_value="a" * 40), mock.patch.object(deploy, "ci_passed") as gate:
                deploy.poll(state)
            gate.assert_not_called()

    def test_only_runtime_input_changes_deploy(self):
        for path in ("server/server.py", "server/Dockerfile", "app/chat.py"):
            self.assertTrue(deploy.runtime_changed([path]))
        self.assertFalse(deploy.runtime_changed(["README.md", "app/livechat_xr.py", "tests/test_server.py"]))

    def test_probe_status_does_not_confuse_disconnected_with_connected(self):
        for status, connected in (("TikTok: connected to @test", True), ("disconnected", False), ("offline", False)):
            parser = probe.Status()
            parser.feed("<b>Status:</b> " + status + "</div>")
            self.assertEqual(": connected to " in parser.value, connected)

    def test_probe_requires_home_and_protected_admin(self):
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        with mock.patch.object(probe.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "admin is not protected"):
                probe.snapshot()
        response.__enter__.return_value.status = 500
        with mock.patch.object(probe.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "home unhealthy"):
                probe.snapshot()

    def test_probe_against_real_loopback_relay(self):
        import test_server
        fixture = test_server.Server()
        fixture.setUp()
        try:
            fixture.post("/register", name="Fixture", email="fixture@example.com", platform="tiktok",
                         channel="fixture", webhook=test_server.HOOK)
            token = next(iter(test_server.server.regs))
            test_server.server.status[token] = "TikTok: connected to @fixture"
            with mock.patch.dict(probe.os.environ, {"PORT": str(fixture.srv.server_port), "DATA_DIR": str(test_server.server.DATA)}):
                result = probe.snapshot()
                self.assertEqual(len(result["registrations"]), 1)
                self.assertEqual(result["connected"], result["registrations"])
                self.assertNotIn(token, json.dumps(result))
                test_server.server.status[token] = "offline"
                self.assertEqual(probe.snapshot()["connected"], [])
        finally:
            fixture.tearDown()
            fixture.srv.server_close()
            fixture.doCleanups()


@unittest.skipUnless(os.name == 'posix', 'real Unix socket/process lifecycle requires Linux')
class RebindSocketLifecycle(unittest.TestCase):
    """Fake Docker control, but real isolated exporters and persistent socket inode."""
    def setUp(self):
        import select
        import signal
        import subprocess
        import sys

        # Reuse the outer transaction's disposable files and strict container identities.
        fixture_spec = importlib.util.spec_from_file_location('socket_rebind_fixture', ROOT / 'tests/test_inspector_rebind.py')
        assert fixture_spec and fixture_spec.loader
        fixture = importlib.util.module_from_spec(fixture_spec)
        fixture_spec.loader.exec_module(fixture)
        self.case = fixture.Transaction()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.rebind, self.native = fixture.rebind, fixture.deploy
        self.socket = self.case.root / 'projection.sock'
        self.processes = []
        self.by_id = {}
        self.start_errors = []
        self.exits = {}
        self.force_stop = False
        self.cleanup_failure = False

        def cleanup():
            for process in self.processes:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=10)
        self.addCleanup(cleanup)

        def start(identity):
            # Use the product listener/server_close, not a dictionary or a socket mock.
            program = '''import signal, sys
from pathlib import Path
sys.path[:0] = [str(Path(sys.argv[1]) / 'server'), str(Path(sys.argv[1]) / 'app')]
import inspection
server = inspection.listener(None, projection=object(), socket_path=sys.argv[2])
def stop(*_):
    if sys.argv[3] == 'fail':
        raise OSError('fixture cleanup failure')
    server.server_close()
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
print('READY', flush=True)
while True:
    signal.pause()
'''
            process = subprocess.Popen([sys.executable, '-B', '-c', program, str(ROOT), str(self.socket),
                                        'fail' if self.cleanup_failure else 'clean'],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.processes.append(process)
            assert process.stdout is not None
            ready, _, _ = select.select([process.stdout], [], [], 15)
            self.assertTrue(ready, 'exporter readiness deadline exceeded')
            if process.stdout.readline() != b'READY\n':
                out, err = process.communicate(timeout=10)
                self.start_errors.append(err.decode())
                raise RuntimeError('disposable exporter could not bind: ' + err.decode())
            self.by_id[identity] = process
            return process
        self.start = start

        def command(*args, **kwargs):
            identity = args[-1]
            if args[:2] == ('docker', 'run'):
                result = self.case.command(*args, **kwargs)
                self.candidate_process = start(result)
                self.candidate_inode = self.socket.stat().st_ino
                return result
            if args[:2] in (('docker', 'rm'), ('docker', 'stop')) and identity in self.by_id:
                process = self.by_id[identity]
                if process.poll() is None:
                    if self.force_stop or args[:2] == ('docker', 'rm') and '-f' in args:
                        process.kill()  # docker rm -f bypasses the product's server_close.
                    else:
                        process.send_signal(signal.SIGTERM)
                process.communicate(timeout=10)
                self.exits[identity] = process.returncode
                code = process.returncode if process.returncode >= 0 else 128 - process.returncode
                self.case.find(identity)['State'].update(Running=False, ExitCode=code)
            if args[:2] == ('docker', 'start'):
                # Docker start is idempotent for a running container. Native
                # auto-rollback may already have restored OLD before recover.
                process = self.by_id.get(identity)
                if process is None or process.poll() is not None:
                    start(identity)  # A stale pathname must really fail with EADDRINUSE.
            return self.case.command(*args, **kwargs)
        self.case.stack.enter_context(mock.patch.object(self.native, 'command', side_effect=command))

    def assert_exporter_restored(self):
        import socket
        self.assertEqual(self.case.find(self.native.NAME)['Id'], self.rebind.OLD)
        self.assertIn(self.rebind.OLD, self.by_id, '\n'.join(self.start_errors))
        self.assertIsNone(self.by_id[self.rebind.OLD].poll())
        self.assertIsNotNone(self.candidate_process.poll())
        self.assertNotEqual(self.socket.stat().st_ino, self.candidate_inode)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(self.socket))

    def test_explicit_outer_recovery_rebinds_real_socket(self):
        with mock.patch.object(self.rebind, 'exporter', side_effect=self.rebind.Refused('fixture-acceptance-failure')):
            self.assertEqual(self.case.run_cli('apply'), 1)
        self.assertIsNone(self.candidate_process.poll())
        result = self.case.run_cli('recover')
        self.assertEqual(result, 0, self.case.output + '\n'.join(self.start_errors))
        self.assert_exporter_restored()
        self.case.assert_restored()
        self.assertEqual(self.case.run_cli('recover'), 0, self.case.output)

    def test_native_auto_rollback_rebinds_real_socket(self):
        with mock.patch.object(self.native, 'wait_healthy', side_effect=[RuntimeError('fixture-health-failure'), None]):
            self.assertEqual(self.case.run_cli('apply'), 1)
        self.assert_exporter_restored()
        self.assertFalse((self.case.state / 'transaction.json').exists())
        self.assertEqual(self.case.run_cli('recover'), 0, self.case.output)
        self.case.assert_restored()


    def assert_shutdown_refused(self, native):
        before = len(self.case.calls)
        if native:
            with mock.patch.object(self.native, 'wait_healthy', side_effect=RuntimeError('fixture-health-failure')):
                self.assertEqual(self.case.run_cli('apply'), 1)
            self.assertTrue((self.case.state / 'transaction.json').exists())
        else:
            with mock.patch.object(self.rebind, 'exporter', side_effect=self.rebind.Refused('fixture-acceptance-failure')):
                self.assertEqual(self.case.run_cli('apply'), 1)
            self.assertEqual(self.case.run_cli('recover'), 1)
            self.assertIn('candidate-shutdown-incomplete', self.case.output)
        self.assertFalse(any(c[:2] in (('docker', 'rm'), ('docker', 'start')) for c in self.case.calls[before:]))
        self.assertIsNotNone(self.case.find('c' * 64))
        self.assertIsNotNone(self.case.find(self.rebind.OLD))
        self.assertEqual(self.socket.stat().st_ino, self.candidate_inode)
        self.assertIsNotNone(self.candidate_process.poll())
        journal = json.loads((self.case.state / self.rebind.JOURNAL).read_bytes())
        self.assertEqual(journal['phase'], 'swap-intent' if native else 'recovering')

    def test_explicit_recovery_forced_stop_retains_socket_and_journal(self):
        self.force_stop = True
        self.assert_shutdown_refused(native=False)

    def test_native_rollback_forced_stop_retains_socket_and_journal(self):
        self.force_stop = True
        self.assert_shutdown_refused(native=True)

    def test_explicit_recovery_cleanup_failure_retains_socket_and_journal(self):
        self.cleanup_failure = True
        self.assert_shutdown_refused(native=False)

    def test_native_rollback_cleanup_failure_retains_socket_and_journal(self):
        self.cleanup_failure = True
        self.assert_shutdown_refused(native=True)

    def test_recovery_never_unlinks_replacement_socket_inode(self):
        import socket
        with mock.patch.object(self.rebind, 'exporter', side_effect=self.rebind.Refused('fixture-acceptance-failure')):
            self.assertEqual(self.case.run_cli('apply'), 1)
        self.socket.unlink()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as foreign:
            foreign.bind(str(self.socket))
            inode = self.socket.stat().st_ino
            self.assertEqual(self.case.run_cli('recover'), 1)
            self.assertEqual(self.candidate_process.returncode, 0)
            self.assertEqual(self.socket.stat().st_ino, inode)
            self.assertTrue(self.start_errors, 'restored bind must refuse foreign socket')
            self.assertEqual(json.loads((self.case.state / self.rebind.JOURNAL).read_bytes())['phase'], 'recovering')

    def test_recovery_identity_drift_never_stops_real_candidate(self):
        with mock.patch.object(self.rebind, 'exporter', side_effect=self.rebind.Refused('fixture-acceptance-failure')):
            self.assertEqual(self.case.run_cli('apply'), 1)
        self.case.find('c' * 64)['Image'] = 'sha256:foreign'
        before = len(self.case.calls)
        self.assertEqual(self.case.run_cli('recover'), 1)
        self.assertIn('candidate-identity-drift', self.case.output)
        self.assertIsNone(self.candidate_process.poll())
        self.assertEqual(self.socket.stat().st_ino, self.candidate_inode)
        self.assertFalse(any(c[:2] in (('docker', 'stop'), ('docker', 'rm'), ('docker', 'start')) for c in self.case.calls[before:]))


if __name__ == "__main__":
    unittest.main()
