"""Explicit, bounded Dora-only outer transaction; never called by the timer.

Bindings are current-state observations, not authorization. See docs/inspector-rebind.md.
Only fixed diagnostic codes leave this process; private snapshots stay under STATE.
"""
import argparse
import ast
import base64
from contextlib import contextmanager
import hashlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid
import urllib.parse

REV = '8df72f06e03e7ee80669f6ebe1e38c5fc7b87309'
DIGEST = '67c0df0523e296c2dbc80163c7691b4fd4236386c5e74b2b29bf4d86f6e4fcd8'
OLD_REV = '82de651bbc1a4f74dc4dc840fd29e081a9174f89'
OLD = '139ae8ea5618791ac28e1f60284f6d00690ca30a42f7e324d10a5375ea598562'
IMAGE = 'sha256:b3520b1147b287abd6adb77422c6a94026d0b0da7e20688cd159121c6d263df4'
STATE = Path('/home/node/.local/share/livechat-xr-deploy')
CONTROLLER = Path('/home/node/.local/lib/livechat-xr-deploy/deploy.py')
CONTROLLER_HASH = '4a4b46351d6d98a81401ca9d61d776f185c88fadb8e134943f093ff8b9e4b228'
ENV_FILE = Path('/home/node/.livechat-xr.env')
UNIT_FILE = Path('/etc/systemd/system/livechat-xr-inspector.service')
STAGE = Path('/opt/livechat-inspection') / REV
OLD_STAGE = PurePosixPath('/opt/livechat-inspection') / OLD_REV
INTERPRETER = str(OLD_STAGE / '.venv/bin/python')
SERVICE = 'livechat-xr-inspector.service'
NAME = 'livechat-xr-relay'
PREVIOUS = NAME + '-previous-' + REV[:12]
JOURNAL = 'inspector-rebind.json'
LIMIT = 1048576
ROOT = Path(__file__).resolve().parents[1]


class Refused(Exception):
    """Only literal diagnostic codes, never exception/command input text."""


def require(ok, code):
    if not ok:
        raise Refused(code)


def digest(body):
    return hashlib.sha256(body).hexdigest()


def packed(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def sync_dir(path):
    if os.name == 'posix':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def snapshot(path, private=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    require(stat.S_ISREG(info.st_mode) and info.st_size <= LIMIT and info.st_nlink == 1, 'unsafe-control-file')
    if private and os.name == 'posix':
        require(info.st_uid == os.geteuid() and not info.st_mode & 0o077, 'private-file-permissions')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0))
    with os.fdopen(fd, 'rb') as stream:
        actual = os.fstat(stream.fileno())
        require((actual.st_dev, actual.st_ino) == (info.st_dev, info.st_ino), 'control-file-race')
        body = stream.read(LIMIT + 1)
    require(len(body) <= LIMIT, 'control-file-bound')
    attrs = {k: base64.b64encode(os.getxattr(path, k, follow_symlinks=False)).decode()
             for k in os.listxattr(path, follow_symlinks=False)} if hasattr(os, 'listxattr') else {}
    return dict(body=base64.b64encode(body).decode(), sha256=digest(body), mode=stat.S_IMODE(info.st_mode),
                uid=info.st_uid, gid=info.st_gid, mtime=info.st_mtime_ns, xattrs=attrs)


def contents(value):
    require(isinstance(value, dict), 'snapshot-invalid')
    body = base64.b64decode(value['body'], validate=True)
    require(len(body) <= LIMIT and digest(body) == value['sha256'], 'snapshot-integrity')
    return body


def atomic(path, body, metadata=None):
    # Temp is on the destination filesystem. No chmod/chown of an existing inode.
    fd, name = tempfile.mkstemp(prefix='.inspector-rebind-', dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(body)
            stream.flush()
            if metadata is not None:
                if os.name == 'posix':
                    os.fchown(stream.fileno(), metadata['uid'], metadata['gid'])
                    os.fchmod(stream.fileno(), metadata['mode'])
                for key, value in metadata['xattrs'].items():
                    os.setxattr(tmp, key, base64.b64decode(value), follow_symlinks=False)
                os.utime(tmp, ns=(metadata['mtime'], metadata['mtime']))
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        sync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def restore(path, value):
    if value is None:
        path.unlink(missing_ok=True)
        sync_dir(path.parent)
    else:
        atomic(path, contents(value), value)


@contextmanager
def native_lock():
    import fcntl
    info = STATE.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077, 'state-permissions')
    fd = os.open(STATE / 'lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid(), 'lock-identity')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused('native-lock-busy') from None
        yield
    finally:
        os.close(fd)


def load_controller():
    installed = contents(snapshot(CONTROLLER))
    require(digest(installed) == CONTROLLER_HASH, 'controller-hash-mismatch')
    reviewed = ast.parse((ROOT / 'server/deploy.py').read_bytes())
    tree = ast.parse(installed)
    # Hash pins the installed file; AST pins every engine dependency to reviewed source.
    names = {'command', 'validate_container', 'probe', 'continuity', 'wait_healthy', 'swap', 'rollback', 'stamp'}
    def engine(module):
        return {n.name: ast.dump(n, include_attributes=False) for n in module.body
                if isinstance(n, ast.FunctionDef) and n.name in names}
    require(engine(tree) == engine(reviewed) and set(engine(tree)) == names, 'controller-source-custody-mismatch')
    spec = importlib.util.spec_from_file_location('_installed_rebind_controller', CONTROLLER)
    require(spec is not None and spec.loader is not None, 'controller-load')
    module = importlib.util.module_from_spec(spec)
    # Execute the exact hashed bytes, not a raced second read or a cached .pyc.
    exec(compile(installed, str(CONTROLLER), 'exec'), module.__dict__)
    require(module.NAME == NAME and module.ENV_FILE == ENV_FILE and module.NETWORK == 'livechat-xr'
            and module.VOLUME == 'livechat-xr-data' and module.BIND == '192.168.1.240:13300:13300', 'controller-globals')
    probe = snapshot(module.PROBE)
    require(probe is not None and contents(probe).replace(b'\r\n', b'\n') == (ROOT / 'server/deploy_probe.py').read_bytes().replace(b'\r\n', b'\n'), 'probe-source-custody')
    return module


def git(*args):
    result = subprocess.run(['git', '--git-dir', str(STATE / 'repo'), *args], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=120)
    require(result.returncode == 0, 'local-git-failed')
    return result.stdout


def stage_source():
    require(STAGE.is_absolute() and STAGE.resolve() == STAGE and STAGE != OLD_STAGE, 'stage-alias')
    require(git('rev-parse', REV + '^{commit}').strip() == REV.encode(), 'stage-revision')
    archive = git('-c', 'core.autocrlf=false', 'archive', '--format=tar', REV)
    expected = {}
    for entry in git('ls-tree', '-r', '-z', REV).split(b'\0'):
        if not entry: continue
        meta, name = entry.split(b'\t', 1)
        mode, kind, oid = meta.split()
        require(kind == b'blob' and mode in (b'100644', b'100755'), 'stage-nonregular-tracked-file')
        expected[name.decode()] = (git('cat-file', 'blob', oid.decode()), int(mode[-3:], 8))
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        found = {}
        for member in tar.getmembers():
            if member.isdir(): continue
            require(member.isfile() and member.name in expected, 'stage-archive-entry')
            stream = tar.extractfile(member)
            require(stream is not None, 'stage-archive-entry')
            found[member.name] = stream.read()
        require(set(found) == set(expected) and all(found[k] == v[0] for k, v in expected.items()), 'stage-tracked-bytes')
    h = hashlib.sha256()
    for name in ('inspection.py', 'network.py', 'backup.py', 'chat.py', 'server.py'):
        path = ('app/' if name == 'chat.py' else 'server/') + name
        h.update(name.encode() + b'\0' + expected[path][0].replace(b'\r\n', b'\n') + b'\0')
    require(h.hexdigest() == DIGEST, 'stage-source-digest')
    if not STAGE.exists():
        stage = Path(tempfile.mkdtemp(prefix='.' + REV + '-', dir=STAGE.parent))
        for name, (body, mode) in expected.items():
            dest = stage / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open('xb') as stream:
                stream.write(body)
                stream.flush()
                os.fchmod(stream.fileno(), mode) if os.name == 'posix' else None
                os.fsync(stream.fileno())
        # Public source only, readable by the already-sandboxed nobody viewer.
        for directory, children, _ in os.walk(stage, topdown=False):
            Path(directory).chmod(0o755)
            sync_dir(Path(directory))
        os.rename(stage, STAGE)
        sync_dir(STAGE.parent)
    actual = {}
    for directory, dirs, files in os.walk(STAGE, followlinks=False):
        directory_info = Path(directory).lstat()
        if os.name == 'posix':
            require(stat.S_IMODE(directory_info.st_mode) == 0o755 and directory_info.st_uid in (0, os.geteuid()), 'stage-directory-metadata')
        require(not any((Path(directory) / d).is_symlink() for d in dirs), 'stage-alias')
        for name in files:
            path = Path(directory) / name
            require(not path.is_symlink() and path.is_file(), 'stage-alias')
            relative = path.relative_to(STAGE).as_posix()
            require(relative in expected, 'stage-untracked-file')
            if os.name == 'posix':
                info = path.lstat()
                require(stat.S_IMODE(info.st_mode) == expected[relative][1] and info.st_uid in (0, os.geteuid()), 'stage-file-metadata')
            actual[relative] = path.read_bytes()
    require(set(actual) == set(expected) and all(actual[k] == v[0] for k, v in expected.items()), 'stage-bytes-drift')


def revision_line(body, before, after):
    lines = body.splitlines(keepends=True)
    matches = [i for i, line in enumerate(lines) if re.match(rb'[ \t]*(?:export[ \t]+)?INSPECTION_REVISION[ \t]*=', line)]
    require(len(matches) == 1, 'env-revision-line-count')
    index = matches[0]
    line = lines[index]
    ending = b'\r\n' if line.endswith(b'\r\n') else b'\n' if line.endswith(b'\n') else b''
    require(line == b'INSPECTION_REVISION=' + before.encode() + ending, 'env-revision-drift')
    lines[index] = b'INSPECTION_REVISION=' + after.encode() + ending
    return b''.join(lines)


def unit_binding(body):
    lines = body.splitlines(keepends=True)
    def one(key):
        matches = [i for i, line in enumerate(lines) if re.match(rb'[ \t]*' + key + rb'[ \t]*=', line)]
        require(len(matches) == 1, 'unit-binding-line-count')
        return matches[0]
    wd, command = one(b'WorkingDirectory'), one(b'ExecStart')
    require(lines[wd].rstrip(b'\r\n') == b'WorkingDirectory=' + str(OLD_STAGE).encode(), 'unit-stage-drift')
    args = shlex.split(lines[command].decode().strip().split('=', 1)[1])
    # Retain an existing clean-env prefix exactly; never widen its environment.
    prefix = ['/usr/bin/env', '-i', 'PATH=/usr/bin:/bin', 'HOME=/nonexistent', 'LANG=C.UTF-8']
    tail = args[len(prefix):] if args[:len(prefix)] == prefix else args
    if tail[:2] == [INTERPRETER, '-B']: tail = [tail[0]] + tail[2:]
    expected = [INTERPRETER, 'server/inspection.py', 'viewer', '--socket', '/run/livechat-inspection/projection.sock',
                '--port', '8787', '--revision', OLD_REV, '--digest']
    require(tail[:-1] == expected and re.fullmatch('[0-9a-f]{64}', tail[-1]) is not None, 'unit-command-drift')
    old_digest = tail[-1]
    ending = b'\r\n' if lines[wd].endswith(b'\r\n') else b'\n' if lines[wd].endswith(b'\n') else b''
    lines[wd] = b'WorkingDirectory=' + str(STAGE).encode() + ending
    command_ending = b'\r\n' if lines[command].endswith(b'\r\n') else b'\n' if lines[command].endswith(b'\n') else b''
    new = (prefix if args[:len(prefix)] == prefix else []) + [INTERPRETER, '-B'] + expected[1:-3] + ['--revision', REV, '--digest', DIGEST]
    lines[command] = b'ExecStart=' + ' '.join(new).encode() + command_ending
    return b''.join(lines), old_digest


SANDBOX = ('User', 'Group', 'DynamicUser', 'NoNewPrivileges', 'ProtectSystem', 'ProtectHome', 'PrivateTmp',
           'PrivateDevices', 'RestrictAddressFamilies', 'IPAddressDeny', 'IPAddressAllow', 'IPIngressFilterPath',
           'IPEgressFilterPath', 'SystemCallFilter', 'CapabilityBoundingSet', 'AmbientCapabilities',
           'ReadOnlyPaths', 'ReadWritePaths', 'InaccessiblePaths', 'BindPaths', 'BindReadOnlyPaths',
           'Environment', 'EnvironmentFiles', 'RootDirectory', 'FragmentPath', 'DropInPaths')


def sandbox(controller):
    text = controller.command('systemctl', 'show', SERVICE, '--property=' + ','.join(SANDBOX))
    fields = dict(line.split('=', 1) for line in text.splitlines())
    require(set(fields) == set(SANDBOX), 'sandbox-properties-unavailable')
    require(fields['FragmentPath'] == str(UNIT_FILE) and not fields['DropInPaths'], 'unit-fragment-or-dropin-drift')
    require(fields['User'] == 'nobody' and fields['NoNewPrivileges'] == 'yes'
            and fields['ProtectSystem'] == 'strict' and fields['ProtectHome'] in ('yes', 'true'), 'sandbox-hardening')
    return digest(text.encode())


def viewer_binding(controller, revision, source_digest):
    text = controller.command('systemctl', 'show', SERVICE, '--property=WorkingDirectory,ExecStart,ActiveState,MainPID')
    fields = dict(line.split('=', 1) for line in text.splitlines())
    stage = str(STAGE) if revision == REV else str(OLD_STAGE)
    require(fields['WorkingDirectory'] == stage and fields['ActiveState'] == 'active'
            and re.fullmatch('[1-9][0-9]*', fields['MainPID']) is not None, 'viewer-process-binding')
    args = [INTERPRETER, '-B', 'server/inspection.py', 'viewer', '--socket', '/run/livechat-inspection/projection.sock',
            '--port', '8787', '--revision', revision, '--digest', source_digest]
    with (Path('/proc') / fields['MainPID'] / 'cmdline').open('rb') as stream:
        command = stream.read(4097)
    actual = command.rstrip(b'\0').decode().split('\0')
    valid = [args] if revision == REV else [args, [args[0]] + args[2:]]
    require(len(command) <= 4096 and actual in valid and ' '.join(actual) in fields['ExecStart'], 'viewer-process-binding')
    require(os.readlink(Path('/proc') / fields['MainPID'] / 'cwd') == stage, 'viewer-process-stage')


def request(method, path, unix=False, origin=None):
    if origin is not None:
        peer = urllib.parse.urlsplit(origin)
        require(peer.scheme == 'https' and peer.hostname is not None and peer.port in (None, 443)
                and not peer.username and not peer.password and peer.path in ('', '/')
                and not peer.query and not peer.fragment, 'proxy-origin-invalid')
        connection = http.client.HTTPSConnection(peer.hostname, 443, timeout=5)
    elif unix:
        import socket
        connection = http.client.HTTPConnection('localhost', timeout=3)
        connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.sock.settimeout(3)
        connection.sock.connect('/run/livechat-inspection/projection.sock')
    else:
        connection = http.client.HTTPConnection('127.0.0.1', 8787, timeout=3)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        body = response.read(262145)
        require(len(body) <= 262144, 'surface-bound')
        require(response.getheader('Location') is None, 'unexpected-redirect')
        return response.status, body
    finally:
        connection.close()


def inspector():
    # Import only after tracked bytes/digest were verified. Never starts collectors.
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(STAGE / 'server'))
    spec = importlib.util.spec_from_file_location('_rebind_inspection', STAGE / 'server/inspection.py')
    require(spec is not None and spec.loader is not None, 'inspector-load')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    require(module.source_digest() == DIGEST, 'inspector-source-digest')
    return module


def exporter(revision, source_digest):
    status, body = request('GET', '/projection', unix=True)
    require(status == 200, 'exporter-unavailable')
    return inspector().validate(body, revision, source_digest)['availability']


def surfaces(revision, source_digest, *, exporter_digest=None):
    exporter_digest = source_digest if exporter_digest is None else exporter_digest
    availability = exporter(revision, exporter_digest)
    # The original incident can be an older viewer against current image bytes.
    # Restoring that CURRENT snapshot truthfully restores a 503, not a stale image.
    if exporter_digest != source_digest:
        availability = 'unavailable'
    status, body = request('GET', '/live')
    require(status == (503 if availability == 'unavailable' else 200), 'viewer-availability-status')
    require(('Source revision ' + revision).encode() in body and ('Artifact digest ' + source_digest).encode() in body, 'viewer-binding')
    require((b'Cache unavailable</h2>' in body) == (availability == 'unavailable'), 'viewer-availability-content')
    for method, path, expected in [('POST', '/live', 405), ('GET', '/admin', 404), ('GET', '/projection', 404),
                                    ('GET', '/live?query=1', 404), ('GET', '/go/' + 'z' * 22, 410)]:
        require(request(method, path)[0] == expected, 'viewer-route-policy')
    return availability


def proxy_policy(origin, revision=None, source_digest=None, availability=None):
    require(isinstance(origin, str) and origin, 'proxy-origin-required')
    checks = []
    for method, path, allowed in [('POST', '/live', (404, 405)), ('GET', '/admin', (404,)),
                                  ('GET', '/projection', (404,)), ('GET', '/live?query=1', (400, 404)),
                                  ('GET', '/go/' + 'z' * 22, (410,))]:
        status, _ = request(method, path, origin=origin)
        require(status in allowed, 'proxy-route-policy')
        checks.append((method, path, status))
    if revision is not None:
        status, body = request('GET', '/live', origin=origin)
        require(status == (503 if availability == 'unavailable' else 200), 'proxy-availability-status')
        require(('Source revision ' + revision).encode() in body and ('Artifact digest ' + source_digest).encode() in body,
                'proxy-source-binding')
        require((b'Cache unavailable</h2>' in body) == (availability == 'unavailable'), 'proxy-availability-content')
    return digest(packed(checks))


class Transaction:
    def __init__(self, controller, origin=None):
        self.origin = origin
        self.c = controller
        self.raw_command = controller.command
        self.path = STATE / JOURNAL
        self.j: dict = {}

    def save(self, phase):
        self.j['phase'] = phase
        body = packed(self.j)
        require(len(body) <= LIMIT, 'outer-journal-bound')
        atomic(self.path, body)

    def inspect(self, key, optional=False):
        # Missing names are established by successful inventory, not a failed inspect.
        try:
            value = json.loads(self.raw_command('docker', 'inspect', key))
        except RuntimeError:
            if optional:
                ids = self.raw_command('docker', 'ps', '-aq', '--no-trunc').splitlines()
                for identity in ids:
                    info = self.inspect(identity)
                    require(info['Name'].lstrip('/') != key and identity != key, 'docker-inspection-unavailable')
                return None
            raise Refused('container-inspection-unavailable') from None
        require(isinstance(value, list) and len(value) == 1, 'container-inspection-invalid')
        return value[0]

    def files(self):
        return {'env': ENV_FILE, 'unit': UNIT_FILE, **{n: STATE / n for n in ('deployed', 'result.json', 'failed')}}

    def capture(self):
        require(not self.path.exists() and not (STATE / 'transaction.json').exists(), 'pending-transaction')
        values = {n: snapshot(p, n == 'env') for n, p in self.files().items()}
        require(values['env'] is not None and values['unit'] is not None and values['deployed'] is not None, 'current-snapshot-missing')
        for name in ('env', 'unit'):
            path = self.files()[name]
            require(path.is_absolute() and path.parent.resolve() == path.parent
                    and os.access(path.parent, os.W_OK | os.X_OK), 'control-directory-not-writable')
            if os.name == 'posix':
                require(values[name]['uid'] == os.geteuid(), 'control-file-owner; no-privilege-fallback')
        revision_line(contents(values['env']), OLD_REV, REV)
        _, old_digest = unit_binding(contents(values['unit']))
        current = self.inspect(NAME)
        self.c.validate_container(current)
        require(current['Id'] == OLD and current['Image'] == IMAGE and current['State']['Running']
                and not current['State'].get('Paused'), 'current-container-drift')
        require('INSPECTION_REVISION=' + OLD_REV in current['Config']['Env'], 'container-env-drift')
        collision = self.inspect(PREVIOUS)
        require(re.fullmatch('9655caef36a2[0-9a-f]{52}', collision['Id']) is not None
                and not collision['State']['Running'] and not collision['State'].get('Paused'), 'collision-identity-drift')
        require(re.fullmatch(rb'[0-9a-f]{40}\s*', contents(values['deployed'])) is not None, 'deployed-state-invalid')
        return dict(schema=1, revision=REV, source_digest=DIGEST, old=OLD, image=IMAGE,
                    files={n: {k: v for k, v in s.items() if k != 'body'} if s else None for n, s in values.items()},
                    container=digest(packed({k: current[k] for k in ('Id', 'Name', 'Image', 'Config', 'HostConfig', 'Mounts')})),
                    collision=collision['Id'], sandbox=sandbox(self.c), old_digest=old_digest,
                    proxy_origin=self.origin, proxy_policy=proxy_policy(self.origin))

    def preflight(self, bindings):
        require(self.capture() == bindings, 'preflight-binding-drift')
        stage_source()
        viewer_binding(self.c, OLD_REV, bindings['old_digest'])
        availability = surfaces(OLD_REV, bindings['old_digest'], exporter_digest=DIGEST)
        require(proxy_policy(self.origin, OLD_REV, bindings['old_digest'], availability) == bindings['proxy_policy'], 'proxy-policy-drift')
        # Recompare immediately before journal admission, including after staging/probes.
        require(self.capture() == bindings, 'preflight-binding-drift')

    def expected_env(self):
        return revision_line(contents(self.j['snapshots']['env']), OLD_REV, REV)

    def expected_unit(self):
        return unit_binding(contents(self.j['snapshots']['unit']))[0]

    def old_binding(self, info):
        self.c.validate_container(info)
        identity = {k: info[k] for k in ('Id', 'Name', 'Image', 'Config', 'HostConfig', 'Mounts')}
        identity['Name'] = '/' + NAME  # name is the only reversible identity field
        require(info['Id'] == OLD and info['Image'] == IMAGE
                and digest(packed(identity)) == self.j['bindings']['container'], 'old-container-identity-drift')

    def candidate(self, info):
        self.c.validate_container(info)
        expected = [v if not v.startswith('INSPECTION_REVISION=') else 'INSPECTION_REVISION=' + REV
                    for v in self.j['container_env']]
        require(info['Id'] != OLD and re.fullmatch('[0-9a-f]{64}', info['Id']) is not None
                and info['Name'] == '/' + NAME and info['Image'] == IMAGE
                and sorted(info['Config']['Env']) == sorted(expected), 'candidate-identity-drift')
        known = self.j.get('candidate')
        require(known is None or known == info['Id'], 'candidate-identity-drift')
        return info['Id']

    def guarded_command(self, *args, **kwargs):
        if args[:2] == ('docker', 'rename'):
            source, destination = args[2:]
            info = self.inspect(source)
            require(info['Id'] in (OLD, self.j['bindings']['collision']) and not info['State']['Running'], 'rename-identity-drift')
            allowed = ((NAME, PREVIOUS), (PREVIOUS, NAME)) if info['Id'] == OLD else ((PREVIOUS, self.j['archive']), (self.j['archive'], PREVIOUS))
            require((info['Name'].lstrip('/'), destination) in allowed, 'rename-source-binding-drift')
            require(self.inspect(destination, optional=True) is None, 'rename-destination-owned')
        elif args[:2] == ('docker', 'rm'):
            info = self.inspect(args[-1])
            require(self.j['phase'] in ('recovering', 'swap-intent'), 'remove-outside-recovery')
            self.candidate(info)
        elif args[:2] == ('docker', 'run'):
            require(self.j['phase'] == 'swap-intent' and self.inspect(NAME, optional=True) is None, 'run-identity-drift')
        elif args[:2] in (('docker', 'start'), ('docker', 'stop'), ('docker', 'unpause')):
            old = self.inspect(OLD)
            require(args[-1] == OLD, 'old-container-identity-drift')
            self.old_binding(old)
        result = self.raw_command(*args, **kwargs)
        if args[:2] == ('docker', 'run'):
            identity = self.candidate(self.inspect(NAME))
            require(result == identity, 'run-result-identity')
            self.j['candidate'] = identity
            self.save('swap-intent')
        return result

    def apply(self, bindings):
        self.preflight(bindings)
        baseline = self.c.probe()
        require(set(baseline) == {'registrations', 'connected'}
                and all(isinstance(v, list) and len(v) <= 10000
                        and all(isinstance(x, str) and re.fullmatch('[0-9a-f]{64}', x) for x in v)
                        and len(set(v)) == len(v) for v in baseline.values())
                and set(baseline['connected']) <= set(baseline['registrations']), 'baseline-invalid')
        require(self.capture() == bindings, 'snapshot-admission-drift')
        self.j = dict(schema=1, phase='prepared', bindings=bindings,
                      snapshots={n: snapshot(p, n == 'env') for n, p in self.files().items()},
                      baseline=baseline, baseline_hash=digest(packed(baseline)),
                      container_env=self.inspect(OLD)['Config']['Env'],
                      archive=PREVIOUS + '-archived-' + uuid.uuid4().hex)
        self.save('prepared')  # durable before the first runtime mutation
        self.save('env-intent')
        atomic(ENV_FILE, self.expected_env(), self.j['snapshots']['env'])
        self.save('env-written')
        self.save('collision-intent')
        with self.commands():
            self.c.command('docker', 'rename', bindings['collision'], self.j['archive'])
            self.save('collision-archived')
            self.save('swap-intent')
            # Native auto-rollback also crosses this guarded command boundary.
            try:
                self.c.swap(REV, IMAGE, OLD, baseline, STATE)
            except Exception:
                raise Refused('native-swap-failed; recover-before-retry') from None
        self.save('native-swapped')
        exporter(REV, DIGEST)
        self.save('exporter-accepted')
        self.save('unit-intent')
        atomic(UNIT_FILE, self.expected_unit(), self.j['snapshots']['unit'])
        self.save('unit-written')
        self.save('reload-intent')
        self.c.command('systemctl', 'daemon-reload')
        self.save('reloaded')
        self.save('restart-intent')
        self.c.command('systemctl', 'restart', SERVICE)
        self.save('restarted')
        self.accept()
        self.save('accepted')
        self.save('committed')

    @contextmanager
    def commands(self):
        previous = self.c.command
        self.c.command = self.guarded_command
        try:
            yield
        finally:
            self.c.command = previous

    def accept(self):
        require(ENV_FILE.read_bytes() == self.expected_env() and UNIT_FILE.read_bytes() == self.expected_unit(), 'written-binding-drift')
        require(sandbox(self.c) == self.j['bindings']['sandbox'], 'sandbox-drift')
        self.candidate(self.inspect(NAME))
        require(self.inspect(OLD)['Name'] == '/' + PREVIOUS, 'rollback-container-drift')
        require(not (STATE / 'transaction.json').exists(), 'pending-native-transaction')
        require((STATE / 'deployed').read_bytes() == REV.encode(), 'native-deployed-drift')
        require(self.c.continuity(self.j['baseline'], self.c.probe()), 'continuity-acceptance')
        viewer_binding(self.c, REV, DIGEST)
        availability = surfaces(REV, DIGEST)
        self.j['availability'] = availability
        require(proxy_policy(self.origin, REV, DIGEST, availability) == self.j['bindings']['proxy_policy'], 'proxy-policy-drift')

    def load(self, bindings):
        saved = snapshot(self.path, True)
        require(saved is not None, 'outer-journal-missing')
        self.j = json.loads(contents(saved))
        require(self.j['schema'] == 1 and self.j['bindings'] == bindings, 'outer-journal-binding')
        old = self.inspect(OLD)
        self.old_binding(old)
        require(self.j['container_env'] == old['Config']['Env'], 'journal-container-env-binding')
        require(digest(packed(self.j['baseline'])) == self.j['baseline_hash'], 'baseline-integrity')
        require(set(self.j['snapshots']) == set(self.files()), 'snapshot-set')
        for n, value in self.j['snapshots'].items():
            if value is not None:
                contents(value)
            expected = {k: v for k, v in value.items() if k != 'body'} if value else None
            require(expected == bindings['files'][n], 'snapshot-binding')
        require(re.fullmatch(re.escape(PREVIOUS) + '-archived-[0-9a-f]{32}', self.j['archive']) is not None, 'archive-binding')

    def recover(self):
        require(self.j['phase'] != 'committed', 'already-committed; inspect-before-explicit-rollback')
        if self.j['phase'] == 'recovered':
            self.verify_restored()
            return
        # Reconcile real identities first, including an already-native-recovered state.
        old = self.inspect(OLD)
        require(old['Image'] == IMAGE and old['Name'] in ('/' + NAME, '/' + PREVIOUS), 'old-container-identity-drift')
        self.old_binding(old)
        current = self.inspect(NAME, optional=True)
        if current is not None and current['Id'] != OLD:
            require(self.j['phase'] in ('swap-intent', 'native-swapped', 'exporter-accepted', 'unit-intent', 'unit-written',
                    'reload-intent', 'reloaded', 'restart-intent', 'restarted', 'accepted', 'recovering'), 'unexpected-candidate')
            self.candidate(current)
        collision = self.inspect(self.j['bindings']['collision'])
        require(not collision['State']['Running'] and collision['Name'] in ('/' + PREVIOUS, '/' + self.j['archive']), 'collision-recovery-drift')
        for name, path, changed in [('env', ENV_FILE, self.expected_env()), ('unit', UNIT_FILE, self.expected_unit())]:
            value = snapshot(path)
            saved = self.j['snapshots'][name]
            require(value is not None and contents(value) in (contents(saved), changed), 'recovery-file-drift')
            require({k: v for k, v in value.items() if k not in ('body', 'sha256')}
                    == {k: v for k, v in saved.items() if k not in ('body', 'sha256')}, 'recovery-file-metadata-drift')
        native_result = json.dumps(dict(sha=REV, home=200, admin_protected=True,
                                       registrations_preserved=len(self.j['baseline']['registrations']),
                                       connected_preserved=len(self.j['baseline']['connected']))).encode()
        for name, permitted in [('deployed', [REV.encode(), self.native_journal()['previous'].encode()]),
                                ('failed', [REV.encode()]), ('result.json', [native_result])]:
            original = self.j['snapshots'][name]
            permitted.append(contents(original) if original else None)
            now = snapshot(STATE / name, True)
            require((contents(now) if now else None) in permitted, 'recovery-record-drift')
        if (STATE / 'transaction.json').exists():
            native = json.loads(contents(snapshot(STATE / 'transaction.json', True)))
            require(native == self.native_journal(), 'native-journal-drift')
        self.save('recovering')
        restore(ENV_FILE, self.j['snapshots']['env'])
        restore(UNIT_FILE, self.j['snapshots']['unit'])
        with self.commands():
            self.c.rollback(self.native_journal(), STATE)
            # If interrupted after native rollback, canonical OLD makes another call safe.
            collision = self.inspect(self.j['bindings']['collision'])
            if collision['Name'] != '/' + PREVIOUS:
                self.c.command('docker', 'rename', collision['Id'], PREVIOUS)
        self.c.command('systemctl', 'daemon-reload')
        self.c.command('systemctl', 'restart', SERVICE)
        require(sandbox(self.c) == self.j['bindings']['sandbox'], 'recovered-sandbox-drift')
        viewer_binding(self.c, OLD_REV, self.j['bindings']['old_digest'])
        availability = surfaces(OLD_REV, self.j['bindings']['old_digest'], exporter_digest=DIGEST)
        require(proxy_policy(self.origin, OLD_REV, self.j['bindings']['old_digest'], availability) == self.j['bindings']['proxy_policy'], 'recovered-proxy-drift')
        for name in ('deployed', 'result.json', 'failed'):
            restore(STATE / name, self.j['snapshots'][name])
        self.verify_restored()
        self.save('recovered')

    def native_journal(self):
        return dict(sha=REV, old=OLD, baseline=self.j['baseline'],
                    previous=contents(self.j['snapshots']['deployed']).decode().strip())

    def verify_restored(self):
        require(self.inspect(NAME)['Id'] == OLD and self.inspect(OLD)['State']['Running'], 'recovery-runtime-incomplete')
        require(self.inspect(PREVIOUS)['Id'] == self.j['bindings']['collision'], 'recovery-collision-incomplete')
        for name, path in self.files().items():
            require(snapshot(path) == self.j['snapshots'][name], 'recovery-snapshot-incomplete')
        require(not (STATE / 'transaction.json').exists() and self.c.continuity(self.j['baseline'], self.c.probe()), 'recovery-continuity-incomplete')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('capture', 'apply', 'recover', 'status'))
    parser.add_argument('--bindings', type=Path, required=True)
    parser.add_argument('--proxy-origin', help='existing Q-owned HTTPS origin; capture only')
    args = parser.parse_args(argv)
    old_umask = None
    try:
        bindings = None
        if args.mode != 'capture':
            try:
                value = snapshot(args.bindings, True)
                require(value is not None, 'bindings-unavailable')
                bindings = json.loads(contents(value))
            except (OSError, ValueError):
                raise Refused('bindings-unavailable') from None
        old_umask = os.umask(0o077)
        with native_lock():
            controller = load_controller()
            transaction = Transaction(controller, args.proxy_origin if bindings is None else bindings['proxy_origin'])
            if args.mode == 'capture':
                require(not args.bindings.exists(), 'bindings-already-exist')
                atomic(args.bindings, packed(transaction.capture()))
                print('rebind: current-bindings-captured; no-runtime-mutation')
            elif args.mode == 'apply':
                transaction.apply(bindings)
                print('rebind: committed; availability=' + transaction.j['availability'])
            else:
                transaction.load(bindings)
                if args.mode == 'recover':
                    transaction.recover()
                    print('rebind: recovered-current-snapshots; continuity-checked')
                else:
                    if transaction.j['phase'] == 'committed':
                        require(STAGE.is_dir() and not STAGE.is_symlink(), 'committed-stage-missing')
                        stage_source()
                        transaction.accept()
                    elif transaction.j['phase'] == 'recovered':
                        transaction.verify_restored()
                    else:
                        raise Refused('pending-outer-transaction; recover-before-retry')
                    print('rebind: ' + transaction.j['phase'])
        return 0
    except Refused as error:
        print('rebind: ' + str(error))
        return 1
    except Exception:
        print('rebind: operation-failed; inspect-private-journal; recover-before-retry')
        return 1
    finally:
        if old_umask is not None:
            os.umask(old_umask)


if __name__ == '__main__':
    raise SystemExit(main())
