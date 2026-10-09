"""Deployment guards and rollback without Docker, GitHub credentials or live users."""
import importlib.util
import json
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


if __name__ == "__main__":
    unittest.main()
