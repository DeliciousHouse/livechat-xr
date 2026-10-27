"""Outer rebind command; disposable control files, no host/provider access."""
import contextlib
import importlib.util
import io
import json
import os
import tempfile
from unittest import mock
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class EntryPoint(unittest.TestCase):
    def test_entrypoint_refuses_missing_private_bindings_without_host_access(self):
        path = ROOT / 'tools/inspector_rebind.py'
        self.assertTrue(path.is_file(), 'operator entry point is missing')
        spec = importlib.util.spec_from_file_location('inspector_rebind', path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = module.main(['apply', '--bindings', str(ROOT / 'absent-rebind-bindings.json')])
        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), 'rebind: bindings-unavailable\n')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rebind = load('inspector_rebind', ROOT / 'tools/inspector_rebind.py')
deploy = load('rebind_native_fixture', ROOT / 'server/deploy.py')


class Transaction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.env = self.root / 'runtime.env'
        self.env.write_bytes(b'PRIVATE=fixture-do-not-print\r\nINSPECTION_REVISION=82de651bbc1a4f74dc4dc840fd29e081a9174f89\r\nOTHER=preserve\n')
        self.env.chmod(0o600)
        self.unit = self.root / 'inspector.service'
        self.unit.write_bytes(b'[Unit]\nDescription=Cache inspection\n[Service]\nUser=nobody\nNoNewPrivileges=yes\nProtectSystem=strict\nWorkingDirectory=/opt/livechat-inspection/82de651bbc1a4f74dc4dc840fd29e081a9174f89\nExecStart=/opt/livechat-inspection/82de651bbc1a4f74dc4dc840fd29e081a9174f89/.venv/bin/python -B server/inspection.py viewer --socket /run/livechat-inspection/projection.sock --port 8787 --revision 82de651bbc1a4f74dc4dc840fd29e081a9174f89 --digest ' + b'a' * 64 + b'\nIPAddressDeny=any\nIPAddressAllow=localhost\n')
        self.unit.chmod(0o644)
        for name, body in [('deployed', b'd' * 40 + b'\n'), ('result.json', b'{"old":"exact bytes"}\n'), ('failed', b'f' * 40 + b'\n')]:
            (self.state / name).write_bytes(body)
            (self.state / name).chmod(0o600)
        self.bindings = self.root / 'bindings.json'
        self.calls = []
        self.swaps = 0
        self.baseline = {'registrations': ['a' * 64], 'connected': ['a' * 64]}
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in [('STATE', self.state), ('ENV_FILE', self.env), ('UNIT_FILE', self.unit),
                            ('STAGE', self.root / 'stage'), ('CONTROLLER', ROOT / 'server/deploy.py')]:
            self.stack.enter_context(mock.patch.object(rebind, name, value))
        self.old = self.info(rebind.OLD, deploy.NAME, True)
        self.collision = self.info('9655caef36a2' + '0' * 52, rebind.PREVIOUS, False)
        self.containers = {self.old['Id']: self.old, self.collision['Id']: self.collision}
        self.stack.enter_context(mock.patch.object(rebind, 'load_controller', return_value=deploy))
        self.stack.enter_context(mock.patch.object(rebind, 'stage_source', side_effect=lambda: rebind.STAGE.mkdir(exist_ok=True)))
        self.stack.enter_context(mock.patch.object(rebind, 'exporter', return_value='unavailable'))
        self.stack.enter_context(mock.patch.object(rebind, 'surfaces', return_value='unavailable'))
        self.stack.enter_context(mock.patch.object(rebind, 'proxy_policy', return_value='fixture-proxy'))
        self.stack.enter_context(mock.patch.object(rebind, 'viewer_binding'))
        self.stack.enter_context(mock.patch.object(rebind, 'sandbox', return_value='sandbox-fixture-hash'))
        self.stack.enter_context(mock.patch.object(deploy, 'command', side_effect=self.command))
        self.stack.enter_context(mock.patch.object(deploy, 'probe', return_value=self.baseline))
        self.stack.enter_context(mock.patch.object(deploy, 'wait_healthy', side_effect=lambda before: self.assertTrue(deploy.continuity(before, self.baseline))))
        self.stack.enter_context(mock.patch.object(deploy, 'ENV_FILE', self.env))
        if os.name != 'posix':
            self.stack.enter_context(mock.patch.object(rebind, 'native_lock', side_effect=lambda: contextlib.nullcontext()))
        self.original = {p: (p.read_bytes(), p.stat().st_mode & 0o777, p.stat().st_mtime_ns) for p in self.paths()}
        self.assertEqual(self.run_cli('capture'), 0, self.output)

    def paths(self):
        return [self.env, self.unit] + [self.state / n for n in ('deployed', 'result.json', 'failed')]

    def info(self, identity, name, running):
        return {'Id': identity, 'Name': '/' + name, 'Image': rebind.IMAGE,
                'Config': {'Env': ['PRIVATE=fixture-do-not-print', 'INSPECTION_REVISION=' + rebind.OLD_REV]},
                'State': {'Running': running, 'Paused': False},
                'HostConfig': {'NetworkMode': 'livechat-xr', 'RestartPolicy': {'Name': 'unless-stopped'},
                               'PortBindings': {'13300/tcp': [{'HostIp': '192.168.1.240', 'HostPort': '13300'}]}},
                'Mounts': [{'Name': 'livechat-xr-data', 'Destination': '/data', 'RW': True}]}

    def find(self, key):
        return next((c for c in self.containers.values() if c['Id'] == key or c['Name'] == '/' + key), None)

    def command(self, *args, **kwargs):
        self.calls.append(args)
        if args[:2] == ('docker', 'ps'):
            return '\n'.join(self.containers)
        if args[:2] == ('docker', 'inspect'):
            c = self.find(args[2])
            if c is None: raise RuntimeError('not found')
            return json.dumps([c])
        if args[:2] == ('docker', 'rename'):
            c = self.find(args[2])
            self.assertIsNotNone(c)
            self.assertIsNone(self.find(args[3]), 'rename must not overwrite')
            c['Name'] = '/' + args[3]
        elif args[:2] == ('docker', 'stop'):
            self.find(args[-1])['State']['Running'] = False
        elif args[:2] == ('docker', 'start'):
            self.find(args[-1])['State']['Running'] = True
        elif args[:2] == ('docker', 'rm'):
            self.assertNotEqual(args[-1], rebind.OLD)
            del self.containers[args[-1]]
        elif args[:2] == ('docker', 'run'):
            self.swaps += 1
            c = self.info('c' * 64, deploy.NAME, True)
            c['Config']['Env'][1] = 'INSPECTION_REVISION=' + rebind.REV
            self.containers[c['Id']] = c
            return c['Id']
        elif args[0] != 'systemctl':
            self.fail('unexpected operation ' + args[0])
        return ''

    def run_cli(self, mode):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            argv = [mode, '--bindings', str(self.bindings)]
            if mode == 'capture': argv += ['--proxy-origin', 'https://inspection.example.invalid']
            result = rebind.main(argv)
        self.assertNotIn('fixture-do-not-print', output.getvalue())
        self.assertNotIn('a' * 64, output.getvalue())
        self.output = output.getvalue()
        return result

    def assert_restored(self):
        for path, (body, mode, mtime) in self.original.items():
            self.assertEqual(path.read_bytes(), body, path.name)
            if os.name == 'posix': self.assertEqual(path.stat().st_mode & 0o777, mode)
            self.assertEqual(path.stat().st_mtime_ns, mtime)
        self.assertEqual(self.find(deploy.NAME)['Id'], rebind.OLD)
        self.assertTrue(self.find(rebind.OLD)['State']['Running'])
        self.assertEqual(self.find(rebind.PREVIOUS)['Id'], self.collision['Id'])
        self.assertFalse((self.state / 'transaction.json').exists())
        self.assertFalse(any('volume' in c or 'prune' in c or 'build' in c or 'fetch' in c for c in self.calls))

    def test_native_journal_cleared_failure_restores_current_snapshot(self):
        def fail_new(revision, digest, **kwargs):
            if revision == rebind.REV:
                self.assertFalse((self.state / 'transaction.json').exists())
                raise rebind.Refused('exporter-binding')
            return 'unavailable'
        with mock.patch.object(rebind, 'surfaces', side_effect=fail_new):
            self.assertEqual(self.run_cli('apply'), 1)
        self.assertEqual(self.swaps, 1)
        self.assertEqual(self.run_cli('recover'), 0, self.output)
        self.assertEqual(self.run_cli('recover'), 0)
        self.assert_restored()
        self.assertEqual(self.swaps, 1)

    def test_unknown_controller_record_drift_is_not_overwritten(self):
        with mock.patch.object(rebind, 'exporter', side_effect=rebind.Refused('exporter-binding')):
            self.assertEqual(self.run_cli('apply'), 1)
        (self.state / 'deployed').write_bytes(b'9' * 40)
        calls = len(self.calls)
        self.assertEqual(self.run_cli('recover'), 1)
        self.assertIn('recovery-record-drift', self.output)
        self.assertEqual((self.state / 'deployed').read_bytes(), b'9' * 40)
        self.assertFalse(any(c[:2] in (('docker', 'rm'), ('docker', 'rename'), ('docker', 'start')) for c in self.calls[calls:]))

    def test_interrupt_each_durable_outer_boundary(self):
        phases = ('prepared', 'env-intent', 'env-written', 'collision-intent', 'collision-archived',
                  'swap-intent', 'native-swapped', 'exporter-accepted', 'unit-intent', 'unit-written',
                  'reload-intent', 'reloaded', 'restart-intent', 'restarted', 'accepted', 'committed')
        for phase in phases:
            for after in (False, True):
                with self.subTest(phase=phase, after=after):
                    case = Transaction()
                    case.setUp()
                    try:
                        save = rebind.Transaction.save
                        def interrupted(transaction, current):
                            if current == phase and not after: raise KeyboardInterrupt()
                            save(transaction, current)
                            if current == phase and after: raise KeyboardInterrupt()
                        with mock.patch.object(rebind.Transaction, 'save', interrupted):
                            with self.assertRaises(KeyboardInterrupt): case.run_cli('apply')
                        swaps = case.swaps
                        if phase == 'committed' and after:
                            self.assertEqual(case.run_cli('status'), 0, case.output)
                        elif (case.state / rebind.JOURNAL).exists():
                            self.assertEqual(case.run_cli('apply'), 1)
                            self.assertEqual(case.run_cli('recover'), 0, case.output)
                            self.assertEqual(case.run_cli('recover'), 0, case.output)
                            case.assert_restored()
                        else:
                            case.assert_restored()
                        self.assertEqual(case.swaps, swaps, 'restart must not repeat swap')
                    finally:
                        case.doCleanups()

    def test_interrupt_command_and_native_record_boundaries(self):
        boundaries = ('collision-rename', 'stop', 'old-rename', 'run', 'reload', 'restart',
                      'transaction.json', 'deployed', 'result.json')
        for boundary in boundaries:
            for after in (False, True):
                with self.subTest(boundary=boundary, after=after):
                    case = Transaction()
                    case.setUp()
                    try:
                        fired = False
                        def hit(key, action):
                            nonlocal fired
                            selected = key == boundary and not fired
                            if selected:
                                fired = True
                                if not after: raise KeyboardInterrupt()
                            result = action()
                            if selected and after: raise KeyboardInterrupt()
                            return result
                        def command(*args, **kwargs):
                            key = ''
                            if args[:2] == ('docker', 'rename'): key = 'collision-rename' if args[2] == case.collision['Id'] else 'old-rename'
                            if args[:2] == ('docker', 'stop'): key = 'stop'
                            if args[:2] == ('docker', 'run'): key = 'run'
                            if args[:2] == ('systemctl', 'daemon-reload'): key = 'reload'
                            if args[:2] == ('systemctl', 'restart'): key = 'restart'
                            return hit(key, lambda: case.command(*args, **kwargs))
                        stamp = deploy.stamp
                        def stamping(state, name, text):
                            return hit(name, lambda: stamp(state, name, text))
                        with mock.patch.object(deploy, 'command', side_effect=command), mock.patch.object(deploy, 'stamp', side_effect=stamping):
                            with self.assertRaises(KeyboardInterrupt): case.run_cli('apply')
                        self.assertTrue(fired)
                        swaps = case.swaps
                        self.assertEqual(case.run_cli('recover'), 0, case.output)
                        self.assertEqual(case.run_cli('recover'), 0, case.output)
                        case.assert_restored()
                        self.assertEqual(case.swaps, swaps)
                    finally:
                        case.doCleanups()

    def test_recovery_interruption_at_each_effect_is_idempotent(self):
        boundaries = ('env', 'unit', 'remove', 'old-rename', 'start', 'collision-rename',
                      'reload', 'restart', 'deployed', 'result.json', 'failed', 'recovered')
        for boundary in boundaries:
            for after in (False, True):
                with self.subTest(boundary=boundary, after=after):
                    case = Transaction()
                    case.setUp()
                    try:
                        with mock.patch.object(rebind, 'exporter', side_effect=rebind.Refused('exporter-binding')):
                            self.assertEqual(case.run_cli('apply'), 1)
                        fired = False
                        def hit(key, action):
                            nonlocal fired
                            selected = key == boundary and not fired
                            if selected:
                                fired = True
                                if not after: raise KeyboardInterrupt()
                            result = action()
                            if selected and after: raise KeyboardInterrupt()
                            return result
                        def command(*args, **kwargs):
                            key = ''
                            if args[:2] == ('docker', 'rm'): key = 'remove'
                            if args[:2] == ('docker', 'rename'): key = 'collision-rename' if args[2] == case.collision['Id'] else 'old-rename'
                            if args[:2] == ('docker', 'start'): key = 'start'
                            if args[:2] == ('systemctl', 'daemon-reload'): key = 'reload'
                            if args[:2] == ('systemctl', 'restart'): key = 'restart'
                            return hit(key, lambda: case.command(*args, **kwargs))
                        restore, save = rebind.restore, rebind.Transaction.save
                        def restoring(path, value):
                            key = 'env' if path == case.env else 'unit' if path == case.unit else path.name
                            return hit(key, lambda: restore(path, value))
                        def saving(transaction, phase):
                            return hit(phase, lambda: save(transaction, phase))
                        with mock.patch.object(deploy, 'command', side_effect=command), mock.patch.object(rebind, 'restore', side_effect=restoring), mock.patch.object(rebind.Transaction, 'save', saving):
                            with self.assertRaises(KeyboardInterrupt): case.run_cli('recover')
                        self.assertTrue(fired)
                        self.assertEqual(case.run_cli('recover'), 0, case.output)
                        self.assertEqual(case.run_cli('recover'), 0, case.output)
                        case.assert_restored()
                        self.assertEqual(case.swaps, 1)
                    finally:
                        case.doCleanups()

    def test_native_failure_auto_rollback_then_outer_recovery(self):
        with mock.patch.object(deploy, 'wait_healthy', side_effect=[RuntimeError('fixture-do-not-print'), None]):
            self.assertEqual(self.run_cli('apply'), 1)
        self.assertEqual(self.find(deploy.NAME)['Id'], rebind.OLD)
        self.assertFalse((self.state / 'transaction.json').exists())
        self.assertEqual(self.run_cli('recover'), 0, self.output)
        self.assert_restored()

    def test_preflight_drift_and_pending_transaction_never_mutate(self):
        for field in ('env', 'unit', 'deployed', 'container', 'collision', 'pending'):
            with self.subTest(field=field):
                case = Transaction()
                case.setUp()
                try:
                    if field in ('env', 'unit'): getattr(case, field).write_bytes(b'unknown owner bytes')
                    elif field == 'deployed': (case.state / 'deployed').write_bytes(b'7' * 40)
                    elif field == 'container': case.old['Image'] = 'sha256:foreign'
                    elif field == 'collision': case.collision['State']['Running'] = True
                    else: (case.state / 'transaction.json').write_bytes(b'{}')
                    before = len(case.calls)
                    self.assertEqual(case.run_cli('apply'), 1)
                    self.assertEqual(case.swaps, 0)
                    self.assertFalse((case.state / rebind.JOURNAL).exists())
                    self.assertTrue(all(c[:2] in (('docker', 'inspect'), ('docker', 'ps')) for c in case.calls[before:]))
                finally: case.doCleanups()

    def test_missing_and_duplicate_env_revision_refused(self):
        for body in (b'OTHER=only\n', self.original[self.env][0] * 2):
            with self.subTest(body_length=len(body)):
                self.env.write_bytes(body)
                self.assertEqual(self.run_cli('apply'), 1)
                self.assertIn('env-revision-line-count', self.output)
                self.assertEqual(self.swaps, 0)

    def test_collision_new_owner_is_not_overwritten_during_recovery(self):
        with mock.patch.object(rebind, 'exporter', side_effect=rebind.Refused('exporter-binding')):
            self.assertEqual(self.run_cli('apply'), 1)
        # Original OLD must take PREVIOUS -> canonical first; a foreign owner then
        # claims the released collision name. Recovery must retain both containers.
        old_name = self.old['Name']
        self.old['Name'] = '/' + deploy.NAME
        self.containers.pop('c' * 64)
        foreign = self.info('e' * 64, rebind.PREVIOUS, False)
        self.containers[foreign['Id']] = foreign
        self.assertEqual(self.run_cli('recover'), 1)
        self.assertEqual(self.find(rebind.PREVIOUS)['Id'], foreign['Id'])
        self.assertIn('rename-destination-owned', self.output)
        self.assertNotEqual(self.collision['Name'], '/' + rebind.PREVIOUS)

    def test_unit_bytes_keep_security_lines_and_exact_command(self):
        changed, _ = rebind.unit_binding(self.original[self.unit][0])
        original_other = [line for line in self.original[self.unit][0].splitlines(True) if not line.startswith((b'WorkingDirectory=', b'ExecStart='))]
        changed_other = [line for line in changed.splitlines(True) if not line.startswith((b'WorkingDirectory=', b'ExecStart='))]
        self.assertEqual(changed_other, original_other)
        command = next(line for line in changed.splitlines() if line.startswith(b'ExecStart='))
        self.assertEqual(command.decode(), 'ExecStart=' + rebind.INTERPRETER + ' -B server/inspection.py viewer --socket /run/livechat-inspection/projection.sock --port 8787 --revision ' + rebind.REV + ' --digest ' + rebind.DIGEST)

    def test_absent_result_and_failed_marker_remain_absent_after_recovery(self):
        self.bindings.unlink()
        for name in ('result.json', 'failed'):
            (self.state / name).unlink()
            self.original.pop(self.state / name)
        self.assertEqual(self.run_cli('capture'), 0)
        with mock.patch.object(rebind, 'exporter', side_effect=rebind.Refused('exporter-binding')):
            self.assertEqual(self.run_cli('apply'), 1)
        self.assertEqual(self.run_cli('recover'), 0, self.output)
        self.assertFalse((self.state / 'result.json').exists())
        self.assertFalse((self.state / 'failed').exists())
        self.assert_restored()

    def test_failed_staging_or_preflight_leaves_runtime_pristine(self):
        with mock.patch.object(rebind, 'stage_source', side_effect=OSError('fixture-do-not-print')):
            self.assertEqual(self.run_cli('apply'), 1)
        self.assertEqual(self.swaps, 0)
        self.assertFalse((self.state / rebind.JOURNAL).exists())
        self.assert_restored()

    def test_unknown_candidate_is_never_removed(self):
        with mock.patch.object(rebind, 'exporter', side_effect=rebind.Refused('exporter-binding')):
            self.assertEqual(self.run_cli('apply'), 1)
        self.containers['c' * 64]['Config']['Env'].append('FOREIGN=owner')
        before = len(self.calls)
        self.assertEqual(self.run_cli('recover'), 1)
        self.assertIn('candidate-identity-drift', self.output)
        self.assertFalse(any(c[:2] == ('docker', 'rm') for c in self.calls[before:]))

    def test_stale_original_cas_refuses_already_rebound_host(self):
        self.env.write_bytes(rebind.revision_line(self.env.read_bytes(), rebind.OLD_REV, rebind.REV))
        self.old['Config']['Env'][1] = 'INSPECTION_REVISION=' + rebind.REV
        self.assertEqual(self.run_cli('apply'), 1)
        self.assertIn('env-revision-drift', self.output)
        self.assertEqual(self.swaps, 0)
        self.assertFalse((self.state / rebind.JOURNAL).exists())

    def test_fsync_and_atomic_replace_failures_leave_recoverable_state(self):
        for target in ('journal', 'env', 'unit'):
            for after in (False, True):
                with self.subTest(target=target, after=after):
                    case = Transaction()
                    case.setUp()
                    try:
                        replace = rebind.os.replace
                        fired = False
                        destination = case.state / rebind.JOURNAL if target == 'journal' else getattr(case, target)
                        def replacing(source, dest):
                            nonlocal fired
                            selected = Path(dest) == destination and not fired
                            if selected:
                                fired = True
                                if not after: raise OSError('fixture-do-not-print')
                            replace(source, dest)
                            if selected and after: raise OSError('fixture-do-not-print')
                        with mock.patch.object(rebind.os, 'replace', side_effect=replacing):
                            self.assertEqual(case.run_cli('apply'), 1)
                        self.assertTrue(fired)
                        if (case.state / rebind.JOURNAL).exists():
                            self.assertEqual(case.run_cli('recover'), 0, case.output)
                        case.assert_restored()
                    finally: case.doCleanups()
        # A file fsync failure must precede rename/mutation.
        with mock.patch.object(rebind.os, 'fsync', side_effect=OSError('fixture-do-not-print')):
            self.assertEqual(self.run_cli('apply'), 1)
        self.assertEqual(self.swaps, 0)
        self.assertFalse((self.state / rebind.JOURNAL).exists())
        self.assert_restored()

    def test_snapshot_and_journal_tampering_refused(self):
        with mock.patch.object(rebind, 'exporter', side_effect=rebind.Refused('exporter-binding')):
            self.assertEqual(self.run_cli('apply'), 1)
        path = self.state / rebind.JOURNAL
        original = path.read_bytes()
        for field in ('snapshot', 'baseline', 'archive'):
            with self.subTest(field=field):
                journal = json.loads(original)
                if field == 'snapshot': journal['snapshots']['env']['body'] = 'eA=='
                elif field == 'baseline': journal['baseline']['connected'] = []
                else: journal['archive'] = 'foreign-name'
                path.write_bytes(json.dumps(journal).encode())
                self.assertEqual(self.run_cli('recover'), 1)
        path.write_bytes(original)
        self.assertEqual(self.run_cli('recover'), 0, self.output)
        self.assert_restored()

    def test_acceptance_requires_effective_viewer_process_binding(self):
        with mock.patch.object(rebind, 'viewer_binding', create=True, side_effect=rebind.Refused('viewer-process-binding')):
            self.assertEqual(self.run_cli('apply'), 1)
        self.assertFalse((self.state / rebind.JOURNAL).exists(), 'preflight must check running viewer')

    def test_proxy_policy_drift_prevents_outer_commit(self):
        with mock.patch.object(rebind, 'proxy_policy', side_effect=lambda origin, revision=None, *args: 'changed-proxy' if revision == rebind.REV else 'fixture-proxy'):
            self.assertEqual(self.run_cli('apply'), 1)
        self.assertNotEqual(json.loads((self.state / rebind.JOURNAL).read_bytes())['phase'], 'committed')

    def test_rollback_identity_drift_prevents_commit(self):
        def drift(*args):
            self.old['Image'] = 'sha256:foreign'
            return 'unavailable'
        with mock.patch.object(rebind, 'exporter', side_effect=drift):
            self.assertEqual(self.run_cli('apply'), 1)
        self.assertIn('old-container-identity-drift', self.output)
        self.assertNotEqual(json.loads((self.state / rebind.JOURNAL).read_bytes())['phase'], 'committed')

    def test_success_is_committed_only_after_acceptance(self):
        self.assertEqual(self.run_cli('apply'), 0, self.output)
        self.assertEqual(self.swaps, 1)
        self.assertIn(rebind.REV.encode(), self.env.read_bytes())
        self.assertEqual(self.run_cli('apply'), 1)
        self.assertEqual(self.swaps, 1)
        self.assertEqual(self.run_cli('status'), 0)
        self.assertEqual(json.loads((self.state / 'inspector-rebind.json').read_bytes())['phase'], 'committed')


class LocalGuards(unittest.TestCase):
    def test_controller_hash_and_reviewed_engine_are_independent_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'deploy.py'
            source = (ROOT / 'server/deploy.py').read_bytes()
            path.write_bytes(source)
            (path.parent / 'deploy_probe.py').write_bytes((ROOT / 'server/deploy_probe.py').read_bytes())
            with mock.patch.object(rebind, 'CONTROLLER', path):
                with mock.patch.object(rebind, 'CONTROLLER_HASH', '0' * 64):
                    with self.assertRaisesRegex(rebind.Refused, 'controller-hash-mismatch'):
                        rebind.load_controller()
                with mock.patch.object(rebind, 'CONTROLLER_HASH', rebind.digest(source)), mock.patch.object(rebind, 'ENV_FILE', Path.home() / '.livechat-xr.env'):
                    controller = rebind.load_controller()
                    self.assertEqual(tuple(__import__('inspect').signature(controller.swap).parameters), ('sha', 'image', 'old', 'baseline', 'state'))
                changed = source.replace(b'def continuity(before, after):', b'def continuity(before, after, extra=None):')
                path.write_bytes(changed)
                with mock.patch.object(rebind, 'CONTROLLER_HASH', rebind.digest(changed)):
                    with self.assertRaisesRegex(rebind.Refused, 'controller-source-custody-mismatch'):
                        rebind.load_controller()

    def test_stage_archive_tracks_every_byte_and_refuses_drift(self):
        import tarfile
        files = {name: (ROOT / name).read_bytes().replace(b'\r\n', b'\n') for name in
                 ('server/inspection.py', 'server/network.py', 'server/backup.py', 'app/chat.py', 'server/server.py')}
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w') as tar:
            for name, body in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(body)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(body))
        entries = list(files.items())
        def git(*args):
            if args[0] == 'rev-parse': return rebind.REV.encode() + b'\n'
            if 'archive' in args: return archive.getvalue()
            if args[0] == 'ls-tree':
                return b''.join(b'100644 blob ' + str(i).encode() + b'\t' + name.encode() + b'\0' for i, (name, _) in enumerate(entries))
            if args[0] == 'cat-file': return entries[int(args[2])][1]
            self.fail('unexpected git command')
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(rebind, 'git', side_effect=git):
            stage = Path(directory).resolve() / rebind.REV
            with mock.patch.object(rebind, 'STAGE', stage):
                rebind.stage_source()
                rebind.stage_source()
                self.assertEqual((stage / 'server/inspection.py').read_bytes(), files['server/inspection.py'])
                (stage / 'server/inspection.py').write_bytes(b'changed')
                with self.assertRaisesRegex(rebind.Refused, 'stage-bytes-drift'): rebind.stage_source()

    @unittest.skipUnless(os.name == 'posix', 'native Linux flock/xattr semantics; Windows transaction tests mock only lock')
    def test_native_timer_lock_rejects_without_replacing_inode(self):
        import fcntl
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(rebind, 'STATE', Path(directory)):
            path = Path(directory) / 'lock'
            path.touch(mode=0o600)
            inode = path.stat().st_ino
            with path.open('r+') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(rebind.Refused, 'native-lock-busy'):
                    with rebind.native_lock(): self.fail('second lock admitted')
            with rebind.native_lock(): self.assertEqual(path.stat().st_ino, inode)

    @unittest.skipUnless(os.name == 'posix', 'native Linux metadata/xattrs')
    def test_atomic_restore_keeps_owner_mode_mtime_and_xattrs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'private'
            path.write_bytes(b'fixture')
            path.chmod(0o600)
            os.setxattr(path, 'user.fixture', b'value')
            saved = rebind.snapshot(path, True)
            rebind.atomic(path, b'new', saved)
            rebind.restore(path, saved)
            self.assertEqual(rebind.snapshot(path, True), saved)
            with mock.patch.object(rebind, 'sync_dir', side_effect=OSError('fixture')):
                with self.assertRaises(OSError): rebind.atomic(path, b'changed', saved)
            rebind.restore(path, saved)
            self.assertEqual(rebind.snapshot(path, True), saved)

    def test_sandbox_properties_fail_closed_on_dropins_or_hardening_drift(self):
        fields = {key: '' for key in rebind.SANDBOX}
        fields.update(User='nobody', NoNewPrivileges='yes', ProtectSystem='strict', ProtectHome='yes', FragmentPath=str(rebind.UNIT_FILE))
        controller = mock.Mock()
        controller.command.return_value = '\n'.join(k + '=' + v for k, v in fields.items())
        original = rebind.sandbox(controller)
        self.assertEqual(len(original), 64)
        for key, value in [('DropInPaths', '/unknown'), ('NoNewPrivileges', 'no'), ('User', 'root')]:
            changed = dict(fields, **{key: value})
            controller.command.return_value = '\n'.join(k + '=' + v for k, v in changed.items())
            with self.assertRaises(rebind.Refused): rebind.sandbox(controller)

    def test_effective_process_argv_and_cwd_are_checked(self):
        args = [rebind.INTERPRETER, '-B', 'server/inspection.py', 'viewer', '--socket', '/run/livechat-inspection/projection.sock', '--port', '8787', '--revision', rebind.REV, '--digest', rebind.DIGEST]
        controller = mock.Mock()
        controller.command.return_value = 'WorkingDirectory=' + str(rebind.STAGE) + '\nActiveState=active\nMainPID=1234\nExecStart=argv[]=' + ' '.join(args) + '\n'
        with mock.patch.object(Path, 'open', return_value=io.BytesIO(b'\0'.join(v.encode() for v in args) + b'\0')), mock.patch.object(rebind.os, 'readlink', return_value=str(rebind.STAGE)):
            rebind.viewer_binding(controller, rebind.REV, rebind.DIGEST)
        with mock.patch.object(Path, 'open', return_value=io.BytesIO(b'foreign\0')), mock.patch.object(rebind.os, 'readlink', return_value=str(rebind.STAGE)):
            with self.assertRaisesRegex(rebind.Refused, 'viewer-process-binding'):
                rebind.viewer_binding(controller, rebind.REV, rebind.DIGEST)
        with mock.patch.object(Path, 'open', return_value=io.BytesIO(b'\0'.join(v.encode() for v in args) + b'\0')), mock.patch.object(rebind.os, 'readlink', return_value='/wrong-stage'):
            with self.assertRaisesRegex(rebind.Refused, 'viewer-process-stage'):
                rebind.viewer_binding(controller, rebind.REV, rebind.DIGEST)

    def test_existing_proxy_checks_include_binding_and_refuse_redirect_authority(self):
        footer = ('Source revision ' + rebind.REV + ' Artifact digest ' + rebind.DIGEST).encode()
        def request(method, path, origin=None):
            if path == '/live' and method == 'GET': return 503, b'Cache unavailable</h2>' + footer
            return (405 if method == 'POST' else 410 if path.startswith('/go/') else 404), b''
        with mock.patch.object(rebind, 'request', side_effect=request):
            self.assertEqual(rebind.proxy_policy('https://inspection.example.invalid'), rebind.proxy_policy('https://inspection.example.invalid', rebind.REV, rebind.DIGEST, 'unavailable'))
            with self.assertRaisesRegex(rebind.Refused, 'proxy-source-binding'):
                rebind.proxy_policy('https://inspection.example.invalid', 'b' * 40, rebind.DIGEST, 'unavailable')
        with mock.patch.object(rebind.http.client, 'HTTPSConnection') as connection:
            for origin in ('http://inspection.example.invalid', 'https://user:secret@inspection.example.invalid', 'https://inspection.example.invalid/path', 'https://inspection.example.invalid:123'):
                with self.assertRaises(rebind.Refused): rebind.request('GET', '/live', origin=origin)
            connection.assert_not_called()

    def test_current_corrected_exporter_and_old_viewer_truthfully_recover_503(self):
        footer = ('Source revision ' + rebind.OLD_REV + ' Artifact digest ' + 'a' * 64).encode()
        def request(method, path, unix=False):
            if path == '/live' and method == 'GET': return 503, b'Cache unavailable</h2>' + footer
            return (405 if method == 'POST' else 410 if path.startswith('/go/') else 404), b''
        with mock.patch.object(rebind, 'exporter', return_value='observed-empty') as exporter, mock.patch.object(rebind, 'request', side_effect=request):
            self.assertEqual(rebind.surfaces(rebind.OLD_REV, 'a' * 64, exporter_digest=rebind.DIGEST), 'unavailable')
            exporter.assert_called_once_with(rebind.OLD_REV, rebind.DIGEST)

    def test_exporter_reuses_actual_inspection_validator(self):
        with mock.patch.object(rebind, 'STAGE', ROOT):
            module = rebind.inspector()
            with tempfile.TemporaryDirectory() as directory:
                runtime = module.network.Network(Path(directory) / 'absent', lambda p, c: c, config={})
                packet = module.Projection(runtime, rebind.REV).read()
                body = module.encode(packet)
                with mock.patch.object(rebind, 'request', return_value=(200, body)):
                    self.assertEqual(rebind.exporter(rebind.REV, rebind.DIGEST), 'unavailable')
                    with self.assertRaisesRegex(ValueError, 'source binding mismatch'):
                        rebind.exporter('b' * 40, rebind.DIGEST)
                self.assertFalse(runtime.store.path.exists())

    def test_truthful_503_binding_and_negative_routes(self):
        footer = ('Source revision ' + rebind.REV + ' Artifact digest ' + rebind.DIGEST).encode()
        def request(method, path, unix=False):
            if path == '/live' and method == 'GET': return 503, b'Cache unavailable</h2>' + footer
            if method == 'POST': return 405, b''
            return (410 if path.startswith('/go/') else 404), b''
        with mock.patch.object(rebind, 'exporter', return_value='unavailable'), mock.patch.object(rebind, 'request', side_effect=request):
            self.assertEqual(rebind.surfaces(rebind.REV, rebind.DIGEST), 'unavailable')
        with mock.patch.object(rebind, 'exporter', return_value='unavailable'), mock.patch.object(rebind, 'request', return_value=(200, footer)):
            with self.assertRaisesRegex(rebind.Refused, 'viewer-availability-status'):
                rebind.surfaces(rebind.REV, rebind.DIGEST)


if __name__ == '__main__':
    unittest.main()
