"""Source-only capture checks. Never launch a browser, namespace or real fixture."""
import ast
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import qa_capture as capture


def png(width=1, height=1):
    def chunk(kind, body):
        return struct.pack("!I", len(body)) + kind + body + struct.pack("!I", zlib.crc32(kind + body))
    return (capture.PNG + chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))


class Capture(unittest.TestCase):
    def setUp(self):
        # All process and namespace boundaries fail closed unless explicitly mocked below.
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.launch = self.stack.enter_context(mock.patch.object(capture.subprocess, "Popen",
            side_effect=AssertionError("real OS launch prohibited")))
        self.stack.enter_context(mock.patch.object(capture, "load_fixture",
            side_effect=AssertionError("real fixture prohibited")))
        self.stack.enter_context(mock.patch.object(capture, "identity",
            side_effect=AssertionError("real process inspection prohibited")))
        self.stack.enter_context(mock.patch.object(capture, "namespaces",
            side_effect=AssertionError("real namespace inspection prohibited")))
        self.deadline = self.stack.enter_context(mock.patch.object(capture, "deadline", side_effect=lambda *a: contextlib.nullcontext()))
        self.verify_inside = capture.verify_inside
        self.stack.enter_context(mock.patch.object(capture, "verify_inside", return_value={"pid": "pid:[2]"}))
        self.stack.enter_context(mock.patch.object(capture.sys, "platform", "linux"))
        if os.name != "posix":
            self.stack.enter_context(mock.patch.object(capture.os, "getuid", return_value=1000, create=True))

    def test_scenarios_and_browser_arguments(self):
        self.assertEqual(capture.SCENARIOS, ("normal", "delayed", "closed-dm"))
        source = Path(__file__).resolve().parent.parent / "tools/qa_relay.py"
        values = [ast.literal_eval(node.value) for node in ast.parse(source.read_text(encoding="utf-8")).body
                  if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "SCENARIOS" for t in node.targets)]
        self.assertEqual(values, [capture.SCENARIOS])
        args = capture.browser_argv(43123)
        self.assertEqual(args, [str(capture.RUNTIME / "browser/chrome"), "--headless",
            "--user-data-dir=/qa/browser-profile", "--no-first-run", "--disable-background-networking",
            "--disable-sync", "--screenshot=/qa/capture/home.png", "http://127.0.0.1:43123/"])
        for bad in (0, 65536, True, "43123", None):
            with self.subTest(port=bad), self.assertRaises(capture.Refused):
                capture.browser_argv(bad)
        self.assertEqual(set(capture.ENV), {"HOME", "TMPDIR", "PATH", "LANG", "FONTCONFIG_FILE"})
        self.assertTrue(all("/qa" in capture.ENV[k] for k in ("HOME", "TMPDIR")))

    def test_plan_is_closed_and_home_only(self):
        plan = capture.example_plan()
        capture.validate_plan(plan)
        args = capture.sandbox_argv(plan, "/approved/plan.json", "normal")
        self.assertEqual(args[:len(capture.SANDBOX)], capture.SANDBOX)
        self.assertIn("--as-pid-1", args)
        self.assertNotIn("--share-net", args)
        self.assertNotIn("--no-sandbox", args)
        self.assertEqual(args[-6:], [str(capture.RUNTIME / "venv/bin/python"), "-I", "-B",
            str(capture.RUNTIME / "launch-viewonly"), "--inside", "normal"])
        for change in ({"scenario": "unknown"}, {"url": "https://example.invalid"},
                       {"environment": {"TOKEN": "forbidden"}}, {"output": "/somewhere"}):
            with self.subTest(change=change), self.assertRaises(capture.Refused):
                capture.validate_plan(dict(plan, **change))
        for target in ("/etc", "/home", "/run", "/qa", "/usr", "/proc", "/dev", "/tmp"):
            altered = dict(plan, resources=[dict(source="/usr/lib/libexample.so", target=target, sha256="0" * 64)])
            with self.subTest(target=target), self.assertRaises(capture.Refused):
                capture.validate_plan(altered)
        self.launch.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "Linux exclusive file boundary")
    def test_output_is_exclusive_and_capture_checked_before_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "capture.png"
            source.write_bytes(png())
            with mock.patch.object(capture, "OUTPUT", root / "output"), \
                    mock.patch.object(capture, "checked_directory", side_effect=lambda p: p):
                out = capture.create_output()
                with self.assertRaises(FileExistsError):
                    capture.create_output()
                result = capture.copy_png(source, out)
                self.assertEqual(result["dimensions"], [1, 1])
                self.assertEqual(result["sha256"], hashlib.sha256(png()).hexdigest())
                self.assertEqual((out / "home.png").read_bytes(), png())
                with self.assertRaises(FileExistsError):
                    capture.copy_png(source, out)
                capture.write_receipt(out, {"synthetic": True})
                with self.assertRaises(FileExistsError):
                    capture.write_receipt(out, {})
                self.assertTrue(json.loads((out / "receipt.json").read_text())["synthetic"])
                with self.assertRaises(capture.Refused):
                    capture.write_receipt(out, {"x": "x" * 65536})
                for bad in (b"not png", png(1921, 1), png(1, 1081), png()[:-1],
                            png()[:20] + b"bad crc" + png()[27:], png() + b"tail"):
                    source.write_bytes(bad)
                    with self.subTest(size=len(bad)), self.assertRaises(capture.Refused):
                        capture.copy_png(source, out)
                source.write_bytes(b"x" * (capture.PNG_LIMIT + 1))
                with self.assertRaises(capture.Refused):
                    capture.copy_png(source, out)
                source.unlink()
                source.mkdir()
                with self.assertRaises(capture.Refused):
                    capture.copy_png(source, out)

    @unittest.skipUnless(os.name == "posix", "Linux no-follow and ownership boundary")
    def test_symlinks_and_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = root / "good.png"
            good.write_bytes(png())
            link = root / "link.png"
            link.symlink_to(good)
            with self.assertRaises(OSError):
                capture.read_regular(link, capture.PNG_LIMIT)
            with mock.patch.object(capture, "OUTPUT", root / "output"):
                out = capture.create_output()
                self.assertEqual(out.stat().st_mode & 0o777, 0o700)
                capture.copy_png(good, out)
                self.assertEqual((out / "home.png").stat().st_mode & 0o777, 0o600)
                with mock.patch.object(capture.os, "getuid", return_value=os.getuid() + 1):
                    with self.assertRaises(capture.Refused):
                        capture.read_regular(good, capture.PNG_LIMIT)

    def test_inner_success_and_failure_always_exit_fixture(self):
        for failure in (None, capture.Refused("stream-limit"), TimeoutError(), KeyboardInterrupt()):
            events = []
            srv = mock.Mock(server_port=43123, server_address=("127.0.0.1", 43123))
            srv.fixture_http_thread.is_alive.return_value = False
            srv.fixture_loop_thread.is_alive.return_value = False
            srv.fileno.return_value = -1
            @contextlib.contextmanager
            def fixture(**kwargs):
                self.assertEqual(kwargs, dict(port=0, network_fixture=True, scenario="normal"))
                events.append("enter")
                try:
                    yield srv
                finally:
                    events.append("exit")
            with mock.patch.object(capture, "load_fixture", return_value=fixture), \
                    mock.patch.object(capture, "checked_directory"), \
                    mock.patch.object(capture, "prepare_internal"), \
                    mock.patch.object(capture, "identity", return_value={"pid": 1, "start": "123"}), \
                    mock.patch.object(capture, "namespaces", return_value={"pid": "pid:[2]"}), \
                    mock.patch.object(capture.os, "getpid", return_value=1), \
                    mock.patch.object(capture, "read_plan", return_value=capture.example_plan()), \
                    mock.patch.object(capture, "run_owned", side_effect=failure,
                                      return_value={"exit": 0, "joined": True}) as run, \
                    mock.patch.object(capture, "copy_png", return_value={"sha256": "0" * 64}) as copy, \
                    mock.patch.object(capture, "write_receipt") as write:
                result = capture.inside("normal")
                self.assertEqual(events, ["enter", "exit"])
                self.assertEqual(result, 0 if failure is None else 1)
                receipt = write.call_args.args[1]
                self.assertEqual(receipt["status"], "captured" if failure is None else "failed")
                self.assertTrue(receipt["fixture_joined"])
                self.assertEqual(copy.call_count, int(failure is None))
                self.assertEqual(run.call_args.kwargs["timeout"], 20)
                self.assertNotIn("stream-limit", json.dumps(receipt) if failure is None else "")

    def test_invalid_scenario_bind_and_cleanup_refuse(self):
        with self.assertRaises(capture.Refused):
            capture.inside("unknown")
        for address, port in (("0.0.0.0", 43), ("localhost", 43), ("127.0.0.1", True)):
            srv = mock.Mock(server_port=port, server_address=(address, port))
            fixture = mock.Mock(return_value=contextlib.nullcontext(srv))
            with mock.patch.object(capture, "load_fixture", return_value=fixture), \
                    mock.patch.object(capture, "checked_directory"), \
                    mock.patch.object(capture, "prepare_internal"), \
                    mock.patch.object(capture.os, "getpid", return_value=1), \
                    mock.patch.object(capture, "identity", return_value={}), \
                    mock.patch.object(capture, "namespaces", return_value={}), \
                    mock.patch.object(capture, "read_plan", return_value=capture.example_plan()), \
                    mock.patch.object(capture, "write_receipt") as write, \
                    mock.patch.object(capture, "run_owned") as run:
                self.assertEqual(capture.inside("normal"), 1)
                run.assert_not_called()
                self.assertEqual(write.call_args.args[1]["status"], "failed")

    def test_supervisor_caps_and_joins_without_logging(self):
        process = mock.Mock(pid=123)
        process.wait.return_value = 0
        process.poll.return_value = 0
        process.returncode = 0
        for problem in (None, capture.Refused("stream-limit"), TimeoutError(), KeyboardInterrupt()):
            process.stdout, process.stderr = io.BytesIO(), io.BytesIO()
            with mock.patch.object(capture.subprocess, "Popen", return_value=process) as launch, \
                    mock.patch.object(capture.os, "pidfd_open", return_value=42, create=True), \
                    mock.patch.object(capture.signal, "pidfd_send_signal", create=True), \
                    mock.patch.object(capture, "identity", return_value={"pid": 123, "start": "42"}), \
                    mock.patch.object(capture, "namespaces", return_value={}), \
                    mock.patch.object(capture, "drain", side_effect=problem, return_value=[0, 0]), \
                    mock.patch.object(capture, "join_owned", return_value=True) as join:
                if problem is None:
                    self.assertTrue(capture.run_owned(["fixed"], timeout=20)["joined"])
                else:
                    with self.assertRaises(type(problem)):
                        capture.run_owned(["fixed"], timeout=20)
                join.assert_called_once()
                kwargs = launch.call_args.kwargs
                self.assertEqual(kwargs["env"], capture.ENV)
                self.assertFalse(kwargs["shell"])
                self.assertTrue(kwargs["close_fds"])
                self.assertEqual(kwargs["stdin"], capture.subprocess.DEVNULL)
                self.assertNotIn("start_new_session", kwargs)
                self.assertTrue(process.stdout.closed)
                self.assertTrue(process.stderr.closed)
                self.assertIn(mock.call(20), self.deadline.call_args_list)

    def test_fixture_startup_exit_and_live_thread_failures_never_copy(self):
        for phase in ("startup", "exit", "live-thread"):
            srv = mock.Mock(server_port=43123, server_address=("127.0.0.1", 43123))
            srv.fixture_http_thread.is_alive.return_value = phase == "live-thread"
            srv.fixture_loop_thread.is_alive.return_value = False
            srv.fileno.return_value = -1
            context = mock.MagicMock()
            context.__enter__.return_value = srv
            if phase == "startup":
                context.__enter__.side_effect = TimeoutError()
            if phase == "exit":
                context.__exit__.side_effect = TimeoutError()
            with self.subTest(phase=phase), \
                    mock.patch.object(capture, "load_fixture", return_value=mock.Mock(return_value=context)), \
                    mock.patch.object(capture, "checked_directory"), \
                    mock.patch.object(capture, "prepare_internal"), \
                    mock.patch.object(capture, "identity", return_value={}), \
                    mock.patch.object(capture.os, "getpid", return_value=1), \
                    mock.patch.object(capture, "read_plan", return_value=capture.example_plan()), \
                    mock.patch.object(capture, "run_owned") as run, \
                    mock.patch.object(capture, "copy_png") as copy, \
                    mock.patch.object(capture, "write_receipt") as write:
                self.assertEqual(capture.inside("normal"), 1)
                context.__exit__.assert_called_once()
                copy.assert_not_called()
                self.assertEqual(run.call_count, int(phase != "startup"))
                self.assertFalse(write.call_args.args[1]["fixture_joined"])

    def test_parent_requires_child_success_and_independent_hash(self):
        for failure in (None, "hash", "cleanup", "scenario", "exit"):
            child = dict(status="captured", synthetic=True, fixture_joined=True, scenario="normal",
                         source_commit="0" * 40, browser=dict(exit=0, joined=True),
                         capture=dict(sha256=hashlib.sha256(png()).hexdigest()))
            if failure == "hash":
                child["capture"]["sha256"] = "0" * 64
            elif failure == "cleanup":
                child["fixture_joined"] = False
            elif failure == "scenario":
                child["scenario"] = "closed-dm"
            elif failure == "exit":
                child["browser"]["exit"] = 1
            with self.subTest(failure=failure), \
                    mock.patch.object(capture, "preflight", return_value=capture.example_plan()), \
                    mock.patch.object(capture, "create_output", return_value=capture.OUTPUT), \
                    mock.patch.object(capture, "identity", return_value={}), \
                    mock.patch.object(capture, "namespaces", return_value={}), \
                    mock.patch.object(capture, "run_owned"), \
                    mock.patch.object(capture, "read_regular", side_effect=[json.dumps(child).encode(), png()]), \
                    mock.patch.object(capture, "write_receipt") as write:
                result = capture.outside("/approved/plan.json", "0" * 64, "normal")
                self.assertEqual(result, int(failure is not None))
                self.assertEqual(write.call_args.args[1]["status"], "captured" if failure is None else "failed")

    def test_parent_failure_preserves_static_receipt(self):
        with mock.patch.object(capture, "preflight", return_value=capture.example_plan()), \
                mock.patch.object(capture, "create_output", return_value=capture.OUTPUT), \
                mock.patch.object(capture, "identity", return_value={"pid": 3, "start": "4"}), \
                mock.patch.object(capture, "namespaces", return_value={}), \
                mock.patch.object(capture, "run_owned", side_effect=TimeoutError("private raw text")) as run, \
                mock.patch.object(capture, "write_receipt") as write:
            self.assertEqual(capture.outside("/approved/plan.json", "0" * 64, "normal"), 1)
            self.assertEqual(run.call_args.kwargs["timeout"], 45)
            receipt = write.call_args.args[1]
            self.assertEqual(receipt["status"], "failed")
            self.assertNotIn("private raw text", json.dumps(receipt))
            self.assertEqual(write.call_args.args[2], "parent.json")

    def test_stream_drain_limits_both_pipes_and_deadline(self):
        # Selector, pipe reads and process inspection are all simulated.
        for stream in (0, 1):
            selector = mock.MagicMock()
            selector.__enter__.return_value = selector
            selector.get_map.return_value = {1: True}
            key = mock.Mock(data=stream)
            selector.select.return_value = [(key, 1)]
            process = mock.Mock(pid=123)
            with mock.patch.object(capture.selectors, "DefaultSelector", return_value=selector), \
                    mock.patch.object(capture.os, "set_blocking", create=True), \
                    mock.patch.object(capture.os, "read", return_value=b"x" * 8192) as read, \
                    mock.patch.object(capture, "remember_descendants"), \
                    mock.patch.object(capture.time, "monotonic", return_value=0):
                with self.assertRaises(capture.Refused):
                    capture.drain(process, {}, 20)
                self.assertEqual(read.call_count, 9)
        with mock.patch.object(capture.selectors, "DefaultSelector", return_value=selector), \
                mock.patch.object(capture.os, "set_blocking", create=True), \
                mock.patch.object(capture.time, "monotonic", side_effect=[0, 21]):
            with self.assertRaises(TimeoutError):
                capture.drain(process, {}, 20)

    def test_pid_identity_is_required_before_signalling(self):
        owned = {123: ({"pid": 123, "start": "old"}, 42)}
        with mock.patch.object(capture, "identity", return_value={"pid": 123, "start": "new"}), \
                mock.patch.object(capture.signal, "pidfd_send_signal", create=True) as send:
            with self.assertRaises(capture.Refused):
                capture.signal_owned(owned, capture.signal.SIGTERM)
            send.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "Linux pidfd cleanup boundary")
    def test_cleanup_escalates_and_reaps_exact_children(self):
        process = mock.Mock(pid=123)
        record = {"pid": 123, "start": "42"}
        owned = {123: (record, 10), 124: ({"pid": 124, "start": "43"}, 11)}
        ticks = iter(range(100))
        killed = []
        def send(records, sig):
            killed.append(sig)
        def inspect(pid):
            if capture.signal.SIGKILL in killed:
                raise FileNotFoundError()
            return owned[pid][0]
        with mock.patch.object(capture, "remember_descendants"), \
                mock.patch.object(capture, "signal_owned", side_effect=send), \
                mock.patch.object(capture, "identity", side_effect=inspect), \
                mock.patch.object(capture.os, "close") as close, \
                mock.patch.object(capture.os, "waitpid", create=True) as reap, \
                mock.patch.object(capture.time, "monotonic", side_effect=lambda: next(ticks)), \
                mock.patch.object(capture.time, "sleep"):
            self.assertTrue(capture.join_owned(process, owned))
            self.assertIn(capture.signal.SIGTERM, killed)
            self.assertIn(capture.signal.SIGKILL, killed)
            reap.assert_called_with(124, capture.os.WNOHANG)
            self.assertEqual(close.call_args_list, [mock.call(10), mock.call(11)])

    def test_inside_attestation_requires_distinct_namespaces_and_clean_env(self):
        observed = {name: "new" for name in ("pid", "mnt", "user", "net")}
        creation = {"namespaces": {name: "old" for name in observed}}
        with mock.patch.dict(capture.os.environ, capture.ENV, clear=True), \
                mock.patch.object(capture.sys, "flags", mock.Mock(isolated=1, no_user_site=1)), \
                mock.patch.object(capture.sys, "dont_write_bytecode", True), \
                mock.patch.object(capture, "read_regular", return_value=json.dumps(creation).encode()), \
                mock.patch.object(capture, "namespaces", return_value=observed):
            self.assertEqual(self.verify_inside(), observed)
            for name in observed:
                with self.subTest(namespace=name):
                    creation["namespaces"][name] = "new"
                    with mock.patch.object(capture, "read_regular", return_value=json.dumps(creation).encode()):
                        with self.assertRaises(capture.Refused):
                            self.verify_inside()
                    creation["namespaces"][name] = "old"
            with mock.patch.dict(capture.os.environ, {"PRIVATE": "not inherited"}):
                with self.assertRaises(capture.Refused):
                    self.verify_inside()

    @unittest.skipUnless(os.name == "posix", "Linux provenance filesystem")
    def test_plan_hash_and_qa_tree_symlinks_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            runtime.mkdir()
            tree = runtime / "python"
            tree.mkdir()
            (tree / "file").write_bytes(b"inert bytes, never executed")
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(capture.example_plan()))
            with mock.patch.object(capture, "RUNTIME", runtime), \
                    mock.patch.object(capture.os, "getuid", return_value=1000):
                with self.assertRaises(capture.Refused):
                    capture.preflight(str(plan_path), "0" * 64)
            with mock.patch.object(capture, "RUNTIME", runtime):
                before = capture.tree_digest(tree)
                (tree / "file").write_bytes(b"changed")
                self.assertNotEqual(capture.tree_digest(tree), before)
                (tree / "escape").symlink_to(plan_path)
                with self.assertRaises(capture.Refused):
                    capture.tree_digest(tree)


if __name__ == "__main__":
    unittest.main()
