"""Bounded synthetic home capture source. Not an execution authorization.

Linux only at runtime. No imports of fixture/production code until inside the
reviewed namespace. See docs/qa-capture.md for the plan and acquisition boundary.
"""
import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import struct
import subprocess
import sys
import time
import zlib

RUNTIME = Path("/home/node/hermes-dora/qa/vrln-viewonly-5d402dde")
OUTPUT = Path("/home/node/hermes-dora/evidence/vrln-pr46-readiness-20261020/synthetic-captures")
TASK = "t_f663a921"
CONTRACT = "59dc444583847d9ab764270fb2016dd3b93c7200db26e8b598fb65cb64964b40"
SCENARIOS = ("normal", "delayed", "closed-dm")  # reviewed tools/qa_relay.py:SCENARIOS
PNG = b"\x89PNG\r\n\x1a\n"
PNG_LIMIT = 10 * 1024 * 1024
STREAM_LIMIT = RECEIPT_LIMIT = 64 * 1024
ENV = {"HOME": "/qa/home", "TMPDIR": "/qa/tmp", "PATH": str(RUNTIME / "venv/bin"),
       "LANG": "C.UTF-8", "FONTCONFIG_FILE": str(RUNTIME / "config/fonts.conf")}
SANDBOX = ["/usr/bin/bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
           "--uid", "1000", "--gid", "1000", "--cap-drop", "ALL", "--as-pid-1",
           "--tmpfs", "/", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/qa"]
TREES = ("python", "venv", "source", "browser")
FILES = ("launch-viewonly", "config/fonts.conf", "config/nsswitch.conf")


class Refused(RuntimeError):
    pass


def require(condition):
    if not condition:
        raise Refused("capture contract refused")


def scenario_valid(scenario):
    require(type(scenario) is str and scenario in SCENARIOS)


def browser_argv(port):
    require(type(port) is int and 1 <= port <= 65535)
    return [str(RUNTIME / "browser/chrome"), "--headless", "--user-data-dir=/qa/browser-profile",
            "--no-first-run", "--disable-background-networking", "--disable-sync",
            "--screenshot=/qa/capture/home.png", f"http://127.0.0.1:{port}/"]


def example_plan():
    """Schema example only: zero hashes are not approved provenance."""
    return dict(version=1, source_commit="0" * 40, source_receipt="PENDING",
                execution_disposition="PENDING", provenance_sha256="0" * 64,
                trees={name: "0" * 64 for name in TREES},
                files={name: "0" * 64 for name in FILES}, resources=[])


def digest_valid(value, length=64):
    return type(value) is str and re.fullmatch("[0-9a-f]{%d}" % length, value) is not None


def absolute(value):
    require(type(value) is str and value.startswith("/") and "\x00" not in value)
    path = Path(value)
    require(str(path) == value and ".." not in path.parts)
    return path


def validate_plan(plan):
    require(type(plan) is dict and set(plan) == set(example_plan()))
    require(type(plan["version"]) is int and plan["version"] == 1)
    require(digest_valid(plan["source_commit"], 40) and digest_valid(plan["provenance_sha256"]))
    for key in ("source_receipt", "execution_disposition"):
        require(type(plan[key]) is str and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,160}", plan[key]))
    for key, names in (("trees", TREES), ("files", FILES)):
        require(type(plan[key]) is dict and set(plan[key]) == set(names))
        require(all(digest_valid(v) for v in plan[key].values()))
    require(type(plan["resources"]) is list and len(plan["resources"]) <= 512)
    targets = set()
    for entry in plan["resources"]:
        require(type(entry) is dict and set(entry) == {"source", "target", "sha256"})
        source, target = absolute(entry["source"]), absolute(entry["target"])
        # Individual loader/library/font files only. Never a host directory,
        # resolver, cache, socket, profile or general /etc, /home, /run mount.
        require(any(source.is_relative_to(p) for p in ("/lib", "/lib64", "/usr/lib", "/usr/lib64", "/usr/share/fonts")))
        require(any(target.is_relative_to(p) for p in ("/lib", "/lib64", "/usr/lib", "/usr/lib64", "/usr/share/fonts")))
        require(source.name not in ("lib", "lib64", "fonts") and target.name not in ("lib", "lib64", "fonts"))
        require(target not in targets and digest_valid(entry["sha256"]))
        targets.add(target)


def checked_directory(path):
    path = Path(path)
    info = path.lstat()
    require(path.is_absolute() and path.resolve(strict=True) == path)
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid())
    return path


def read_regular(path, limit, *, owned=True):
    # O_NONBLOCK makes a substituted FIFO fail inspection rather than hang open.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_size <= limit)
        require(not owned or before.st_uid == os.getuid())
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as stream:
        body = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        require(len(body) <= limit and len(body) == before.st_size)
        require((before.st_ino, before.st_size, before.st_mtime_ns) ==
                (after.st_ino, after.st_size, after.st_mtime_ns))
        return body


def tree_digest(path):
    checked_directory(path)
    digest = hashlib.sha256()
    items = []
    for item in path.rglob("*"):
        require(len(items) < 100000)
        items.append(item)
    total = 0
    for item in sorted(items):
        info = item.lstat()
        require(info.st_uid == os.getuid())
        name = item.relative_to(path).as_posix()
        if stat.S_ISLNK(info.st_mode):
            resolved = item.resolve(strict=True)
            require(any(resolved.is_relative_to(RUNTIME / p) for p in ("python", "venv")))
            require(resolved.is_file())  # no directory links or traversal outside QA prefixes
            value = "link:" + os.readlink(item)
        elif stat.S_ISDIR(info.st_mode):
            value = "dir"
        else:
            body = read_regular(item, 512 * 1024 * 1024)
            total += len(body)
            require(total <= 2 * 1024 * 1024 * 1024)
            value = hashlib.sha256(body).hexdigest()
        digest.update(json.dumps([name, info.st_mode & 0o777, value], separators=(",", ":")).encode() + b"\n")
    return digest.hexdigest()


def read_plan(path):
    plan = json.loads(read_regular(path, RECEIPT_LIMIT))
    validate_plan(plan)
    return plan


def preflight(path, expected_sha256):
    require(sys.platform == "linux" and os.getuid() == 1000 and digest_valid(expected_sha256))
    require(Path(path).resolve(strict=True) == absolute(path))
    body = read_regular(path, RECEIPT_LIMIT)
    require(hashlib.sha256(body).hexdigest() == expected_sha256)
    plan = json.loads(body)
    validate_plan(plan)
    require(plan["execution_disposition"] != "PENDING" and plan["source_receipt"] != "PENDING")
    checked_directory(RUNTIME)
    for name, expected in plan["trees"].items():
        require(tree_digest(RUNTIME / name) == expected)
    for name, expected in plan["files"].items():
        item = RUNTIME / name
        require(item.resolve(strict=True) == item)
        require(hashlib.sha256(read_regular(item, 1024 * 1024)).hexdigest() == expected)
    require(read_regular(RUNTIME / "launch-viewonly", 1024 * 1024) ==
            read_regular(Path(__file__).resolve(), 1024 * 1024))
    for entry in plan["resources"]:
        source = Path(entry["source"])
        require(source.resolve(strict=True) == source)
        require(hashlib.sha256(read_regular(source, 512 * 1024 * 1024, owned=False)).hexdigest() == entry["sha256"])
    # No interpreter, shebang or symlink fallback to the host installation.
    cfg = dict(line.strip().split(" = ", 1) for line in
               read_regular(RUNTIME / "venv/pyvenv.cfg", 16384).decode().splitlines() if " = " in line)
    require(cfg.get("home") == str(RUNTIME / "python/bin"))
    require(cfg.get("executable") == str(RUNTIME / "python/bin/python3.12"))
    require(cfg.get("include-system-site-packages") == "false")
    executable = (RUNTIME / "venv/bin/python").resolve(strict=True)
    require(executable.is_relative_to(RUNTIME / "python") or executable.is_relative_to(RUNTIME / "venv"))
    for item in (RUNTIME / "venv/bin").iterdir():
        if item.is_file() and not item.is_symlink():
            with item.open("rb") as stream:
                first = stream.readline(4096)
            if first.startswith(b"#!"):
                require(first.rstrip().decode() == "#!" + str(RUNTIME / "venv/bin/python"))
    return plan


def sandbox_argv(plan, plan_path, scenario):
    validate_plan(plan)
    scenario_valid(scenario)
    args = list(SANDBOX)
    mounts = [(str(RUNTIME / name), str(RUNTIME / name)) for name in (*TREES, *FILES)]
    mounts += [(e["source"], e["target"]) for e in plan["resources"]]
    mounts += [(str(RUNTIME / "config/nsswitch.conf"), "/etc/nsswitch.conf"),
               (plan_path, "/qa/plan.json"), (str(OUTPUT / "creation.json"), "/qa/creation.json")]
    directories = {str(OUTPUT), *(str(p) for p in OUTPUT.parents if str(p) != "/")}
    for _, target in mounts:
        directories.update(str(p) for p in Path(target).parents if str(p) not in ("/", "/qa"))
    # Ancestors only; no broad host-directory binds.
    for directory in sorted(directories, key=lambda p: (len(Path(p).parts), p)):
        if directory not in ("/proc", "/dev", "/tmp"):
            args += ["--dir", directory]
    for source, target in mounts:
        args += ["--ro-bind", source, target]
    args += ["--bind", str(OUTPUT), str(OUTPUT), "--chdir", "/qa"]
    for key, value in ENV.items():
        args += ["--setenv", key, value]
    return args + ["--", str(RUNTIME / "venv/bin/python"), "-I", "-B",
                   str(RUNTIME / "launch-viewonly"), "--inside", scenario]


def create_output():
    checked_directory(OUTPUT.parent)
    # mkdir itself is exclusive: even a pre-existing empty or owned dir refuses.
    OUTPUT.mkdir(mode=0o700)
    checked_directory(OUTPUT)
    require(OUTPUT.stat().st_mode & 0o777 == 0o700)
    return OUTPUT


def exclusive_write(directory, name, body):
    require(len(body) <= PNG_LIMIT if name == "home.png" else len(body) <= RECEIPT_LIMIT)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and info.st_mode & 0o777 == 0o700)
        output = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with os.fdopen(output, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(fd)


def write_receipt(directory, receipt, name="receipt.json"):
    require(name in ("receipt.json", "parent.json", "creation.json"))
    body = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    require(len(body) <= RECEIPT_LIMIT)
    exclusive_write(directory, name, body)


def copy_png(source, directory):
    checked_directory(source.parent)
    body = read_regular(source, PNG_LIMIT)
    require(body.startswith(PNG))
    offset = len(PNG)
    dimensions = None
    data_seen = ended = False
    while offset < len(body):
        require(offset + 12 <= len(body) and not ended)
        size, kind = struct.unpack("!I4s", body[offset:offset + 8])
        end = offset + 12 + size
        require(end <= len(body))
        data = body[offset + 8:end - 4]
        require(zlib.crc32(kind + data) == struct.unpack("!I", body[end - 4:end])[0])
        if dimensions is None:
            require(kind == b"IHDR" and size == 13)
            width, height = struct.unpack("!II", data[:8])
            require(1 <= width <= 1920 and 1 <= height <= 1080)
            dimensions = [width, height]
        elif kind == b"IHDR":
            raise Refused("duplicate PNG header")
        if kind == b"IDAT":
            data_seen = True
        if kind == b"IEND":
            require(size == 0 and data_seen)
            ended = True
        offset = end
    require(ended)
    exclusive_write(directory, "home.png", body)
    return dict(path=str(directory / "home.png"), sha256=hashlib.sha256(body).hexdigest(),
                size=len(body), dimensions=dimensions)


def identity(pid):
    # /proc stat comm may contain spaces and parentheses; fields after its last ')'.
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return dict(pid=pid, start=fields[19])


def namespaces(pid):
    return {name: os.readlink(f"/proc/{pid}/ns/{name}") for name in ("pid", "mnt", "net", "user")}


@contextlib.contextmanager
def deadline(seconds):
    started = time.monotonic()
    def expired(*_):
        raise TimeoutError("bounded phase expired")
    old = signal.signal(signal.SIGALRM, expired)
    prior = signal.setitimer(signal.ITIMER_REAL, seconds)
    if prior[0]:
        signal.setitimer(signal.ITIMER_REAL, min(seconds, prior[0]))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
        if prior[0]:
            signal.setitimer(signal.ITIMER_REAL, max(0.000001, prior[0] - (time.monotonic() - started)), prior[1])


def children(pid):
    return [int(p) for p in Path(f"/proc/{pid}/task/{pid}/children").read_text().split()]


def remember_descendants(pid, owned):
    pending = [pid]
    visited = set()
    while pending:
        parent = pending.pop()
        if parent in visited:
            continue
        visited.add(parent)
        require(len(visited) <= 512)
        try:
            descendants = children(parent)
        except FileNotFoundError:
            continue
        for child in descendants:
            if child not in owned:
                require(len(owned) < 512)
                try:
                    current = identity(child)
                    handle = os.pidfd_open(child)
                    if identity(child) != current:
                        os.close(handle)
                        raise Refused("process identity changed")
                    owned[child] = (current, handle)
                except ProcessLookupError:
                    continue
            pending.append(child)


def signal_owned(owned, sig):
    for pid, (record, handle) in list(owned.items()):
        try:
            require(identity(pid) == record)
            signal.pidfd_send_signal(handle, sig)
        except (ProcessLookupError, FileNotFoundError):
            pass


def join_owned(process, owned):
    # The inner wrapper is namespace PID 1, so orphaned Chromium descendants
    # reparent here. The outer parent owns only bwrap's subtree, never global PIDs.
    complete = True
    try:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            end = time.monotonic() + 5
            while True:
                try:
                    remember_descendants(os.getpid() if os.getpid() == 1 else process.pid, owned)
                except (OSError, Refused):
                    complete = False  # Still stop/join the exact identities already owned.
                signal_owned(owned, sig)
                if process.poll() is None:
                    try:
                        process.wait(timeout=min(0.05, max(0.001, end - time.monotonic())))
                    except subprocess.TimeoutExpired:
                        pass
                for pid in list(owned):
                    if pid == process.pid:
                        continue
                    try:
                        os.waitpid(pid, os.WNOHANG)
                    except ChildProcessError:
                        pass
                live = []
                for pid, (record, _) in owned.items():
                    try:
                        require(identity(pid) == record)
                        live.append(pid)
                    except FileNotFoundError:
                        pass
                if process.poll() is not None and not live:
                    process.wait(timeout=0)
                    return complete
                require(time.monotonic() < end or sig != signal.SIGKILL)
                if time.monotonic() >= end:
                    break
                time.sleep(0.01)
        return False
    finally:
        for _, handle in owned.values():
            os.close(handle)


def drain(process, owned, timeout):
    end = time.monotonic() + timeout
    counts = [0, 0]
    with selectors.DefaultSelector() as selector:
        for i, stream in enumerate((process.stdout, process.stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, i)
        while selector.get_map() or process.poll() is None:
            if time.monotonic() >= end:
                raise TimeoutError("child deadline")
            remember_descendants(os.getpid() if os.getpid() == 1 else process.pid, owned)
            for key, _ in selector.select(timeout=min(0.05, end - time.monotonic())):
                data = os.read(key.fileobj.fileno(), 8192)
                counts[key.data] += len(data)
                require(counts[key.data] <= STREAM_LIMIT)
                if not data:
                    selector.unregister(key.fileobj)
            # Streams are discarded, never decoded, logged or retained.
        process.wait(timeout=max(0.001, end - time.monotonic()))
    return counts


def run_owned(argv, *, timeout, record=None):
    require(hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"))
    if record is None:
        record = {}
    record.update(joined=False, exit=None, timeout=False)
    process = None
    owned = {}
    try:
        with deadline(timeout):
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=dict(ENV), cwd="/qa" if os.getpid() == 1 else str(RUNTIME),
                                       shell=False, close_fds=True)
            owned_identity = identity(process.pid)
            handle = os.pidfd_open(process.pid)
            owned[process.pid] = (owned_identity, handle)
            require(identity(process.pid) == owned_identity)
            record["identity"] = dict(owned_identity, namespaces=namespaces(process.pid))
            counts = drain(process, owned, timeout)
            require(process.returncode == 0)
            record["stream_bytes"] = counts
            return record
    except TimeoutError:
        record["timeout"] = True
        raise
    finally:
        if process is not None:
            try:
                # Even setup failure owns the unreaped Popen child; never signal a
                # reused numeric PID. pidfd failure is fatal, not a sandbox fallback.
                if not owned:
                    handle = os.pidfd_open(process.pid)
                    owned[process.pid] = (identity(process.pid), handle)
                record["joined"] = join_owned(process, owned)
                record["descendants"] = [entry[0] for entry in owned.values()]
                record["exit"] = process.returncode
                require(record["joined"])
            finally:
                process.stdout.close()
                process.stderr.close()


def load_fixture():
    path = RUNTIME / "source/tools/qa_relay.py"
    spec = importlib.util.spec_from_file_location("qa_relay", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    require(module.SCENARIOS == SCENARIOS)
    return module.fixture


def prepare_internal():
    for name in ("home", "tmp", "browser-profile", "capture"):
        Path("/qa", name).mkdir(mode=0o700)


def verify_inside():
    require(dict(os.environ) == ENV and sys.flags.isolated and sys.flags.no_user_site and sys.dont_write_bytecode)
    creation = json.loads(read_regular("/qa/creation.json", RECEIPT_LIMIT))
    observed = namespaces(os.getpid())
    require(set(observed) == {"pid", "net", "mnt", "user"})
    require(all(observed[key] != creation["namespaces"][key] for key in observed))
    return observed


def inside(scenario):
    scenario_valid(scenario)
    require(sys.platform == "linux" and os.getpid() == 1)
    receipt = dict(task=TASK, contract_sha256=CONTRACT, synthetic=True, status="failed", scenario=scenario, started=time.time(),
                   fixture_joined=False, isolation={"browser_sandbox": "unverified", "quotas": "unestablished"})
    srv = None
    context = None
    try:
        with deadline(45):
            checked_directory(OUTPUT)
            observed = verify_inside()
            prepare_internal()
            plan = read_plan("/qa/plan.json")
            receipt.update(source_commit=plan["source_commit"], source_receipt=plan["source_receipt"],
                           provenance_sha256=plan["provenance_sha256"], execution_disposition=plan["execution_disposition"],
                           runtime=str(RUNTIME), output=str(OUTPUT), uid=os.getuid(),
                           wrapper=identity(os.getpid()), namespaces=observed,
                           environment_keys=sorted(ENV), sandbox_options=SANDBOX,
                           readonly_mounts=[str(RUNTIME / name) for name in (*TREES, *FILES)] + plan["resources"])
            try:
                with deadline(15):
                    context = load_fixture()(port=0, network_fixture=True, scenario=scenario)
                    srv = context.__enter__()
                require(srv.server_address[0] == "127.0.0.1")
                argv = browser_argv(srv.server_port)
                receipt["port"] = srv.server_port
                receipt["browser"] = {}
                run_owned(argv, timeout=20, record=receipt["browser"])
            finally:
                if context is not None:
                    with deadline(5):
                        context.__exit__(*sys.exc_info())
                if srv is not None:
                    receipt["fixture_joined"] = (not srv.fixture_http_thread.is_alive() and
                        not srv.fixture_loop_thread.is_alive() and srv.fileno() == -1)
            require(receipt["fixture_joined"])
            receipt["capture"] = copy_png(Path("/qa/capture/home.png"), OUTPUT)
            receipt["status"] = "captured"
    except BaseException as error:
        # Static classes only; arbitrary exceptions may contain private paths/body.
        receipt["error"] = "timeout" if isinstance(error, TimeoutError) else "capture-failed"
    finally:
        receipt["finished"] = time.time()
        receipt["wrapper_exit"] = 0 if receipt["status"] == "captured" else 1
        write_receipt(OUTPUT, receipt)
    return 0 if receipt["status"] == "captured" else 1


def outside(plan_path, expected_sha256, scenario):
    scenario_valid(scenario)
    plan = preflight(plan_path, expected_sha256)
    output = create_output()
    receipt = dict(task=TASK, contract_sha256=CONTRACT, synthetic=True, status="failed", started=time.time(), plan_sha256=expected_sha256,
                   source_commit=plan["source_commit"], scenario=scenario, runtime=str(RUNTIME), output=str(output),
                   isolation={"effective_loaded_policy": "unverified", "quotas": "unestablished",
                              "tmpfs_disappearance": "unverified"})
    try:
        with deadline(60):
            receipt.update(parent=identity(os.getpid()), namespaces=namespaces(os.getpid()))
            write_receipt(output, dict(receipt, uid=os.getuid(), output_mode="0700",
                                      sandbox_options=sandbox_argv(plan, plan_path, scenario),
                                      environment_keys=sorted(ENV), provenance_sha256=plan["provenance_sha256"],
                                      source_receipt=plan["source_receipt"],
                                      execution_disposition=plan["execution_disposition"]), "creation.json")
            receipt["sandbox"] = {}
            run_owned(sandbox_argv(plan, plan_path, scenario), timeout=45, record=receipt["sandbox"])
            child = json.loads(read_regular(output / "receipt.json", RECEIPT_LIMIT))
            require(child["status"] == "captured" and child["synthetic"] is True and child["fixture_joined"] is True)
            require(child["scenario"] == scenario and child["source_commit"] == plan["source_commit"])
            require(child["browser"]["exit"] == 0 and child["browser"]["joined"] is True)
            capture = read_regular(output / "home.png", PNG_LIMIT)
            require(hashlib.sha256(capture).hexdigest() == child["capture"]["sha256"])
            receipt["status"] = "captured"
    except BaseException as error:
        receipt["error"] = "timeout" if isinstance(error, TimeoutError) else "sandbox-failed"
    finally:
        receipt["finished"] = time.time()
        receipt["wrapper_exit"] = 0 if receipt["status"] == "captured" else 1
        write_receipt(output, receipt, "parent.json")
    return 0 if receipt["status"] == "captured" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inside", choices=SCENARIOS, help=argparse.SUPPRESS)
    parser.add_argument("--plan")
    parser.add_argument("--plan-sha256")
    parser.add_argument("--scenario", choices=SCENARIOS, default="normal")
    args = parser.parse_args()
    require(sys.platform == "linux")
    def interrupted(*_):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    if args.inside is not None:
        require(args.plan is None and args.plan_sha256 is None)
        return inside(args.inside)
    require(args.plan is not None and args.plan_sha256 is not None)
    with deadline(60):
        return outside(args.plan, args.plan_sha256, args.scenario)


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        # Never print exception text, stdout/stderr, body or profile state.
        code = 1
    sys.exit(code)
