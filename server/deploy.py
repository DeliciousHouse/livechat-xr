"""Dora-only pull deploy. Public GitHub reads; credentials stay in the host env file.

Install this controller from a reviewed commit, not from each fetched candidate.
No migrations or automatic volume restores: rollback retains writes made after swap.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import time
import urllib.request

REPO = "DeliciousHouse/livechat-xr"
NAME = "livechat-xr-relay"
NETWORK = "livechat-xr"
VOLUME = "livechat-xr-data"
BIND = "192.168.1.240:13300:13300"
ENV_FILE = Path.home() / ".livechat-xr.env"
PROBE = Path(__file__).with_name("deploy_probe.py")


def command(*args, input=None):
    result = subprocess.run(args, input=input, text=True, capture_output=True, timeout=900)
    if result.returncode:
        # Never emit raw Docker output, runtime env or GitHub error bodies.
        raise RuntimeError(f"command failed: {args[0]} {args[1]}")
    return result.stdout.strip()


def github(path):
    request = urllib.request.Request("https://api.github.com/repos/" + REPO + "/" + path,
                                     headers={"Accept": "application/vnd.github+json", "User-Agent": "livechat-xr-deploy"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def ci_passed(sha):
    checks = github(f"commits/{sha}/check-runs?per_page=100")["check_runs"]
    matching = [c for c in checks if c.get("name") == "full-suite" and c.get("head_sha") == sha
                and c.get("app", {}).get("slug") == "github-actions"]
    # The newest matching run must pass; an older success cannot hide a failed rerun.
    latest = max(matching, key=lambda c: c.get("id", 0), default={})
    return latest.get("status") == "completed" and latest.get("conclusion") == "success"


def target(state):
    repo = state / "repo"
    if not repo.exists():
        command("git", "clone", "--bare", "https://github.com/" + REPO + ".git", str(repo))
    command("git", "--git-dir", str(repo), "fetch", "origin", "main:refs/heads/main")
    sha = command("git", "--git-dir", str(repo), "rev-parse", "main")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RuntimeError("invalid target SHA")
    return sha


def runtime_changed(paths):
    return any(p.startswith("server/") or p == "app/chat.py" for p in paths)


def continuity(before, after):
    return (set(before["registrations"]) <= set(after["registrations"])
            and set(before["connected"]) <= set(after["connected"]))


def probe():
    return json.loads(command("docker", "exec", "-i", NAME, "python", "-", input=PROBE.read_text()))


def wait_healthy(before):
    deadline = time.monotonic() + 240  # startup staggers registrations; TikTok can take time to reconnect
    while True:
        try:
            if continuity(before, probe()):
                return
        except (RuntimeError, ValueError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("relay continuity health check failed")
        time.sleep(10)


def validate_container(info):
    host = info["HostConfig"]
    if (host["NetworkMode"] != NETWORK or host["RestartPolicy"]["Name"] != "unless-stopped"
            or host["PortBindings"] != {"13300/tcp": [{"HostIp": "192.168.1.240", "HostPort": "13300"}]}
            or len(info["Mounts"]) != 1 or info["Mounts"][0].get("Name") != VOLUME
            or info["Mounts"][0].get("Destination") != "/data" or not info["Mounts"][0].get("RW")):
        raise RuntimeError("container configuration drift; Dora must reconcile before deployment")


def stamp(state, name, text):
    tmp = state / (name + ".tmp")
    with tmp.open("w") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, state / name)
    if os.name == "posix":
        directory = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def rollback(journal, state):
    old = journal["old"]
    info = json.loads(command("docker", "inspect", old))[0]
    if info["State"].get("Paused"):
        command("docker", "unpause", old)
    # If rename failed, the original may still have the canonical name.
    if info["Name"].lstrip("/") != NAME:
        try:
            candidate = json.loads(command("docker", "inspect", NAME))[0]
        except RuntimeError:
            candidate = None
        if candidate is not None:
            if candidate["Id"] == old:
                raise RuntimeError("unexpected rollback identity")
            command("docker", "rm", "-f", candidate["Id"])
        command("docker", "rename", old, NAME)
    command("docker", "start", old)
    wait_healthy(journal["baseline"])
    stamp(state, "deployed", journal["previous"])
    stamp(state, "failed", journal["sha"])
    (state / "transaction.json").unlink(missing_ok=True)


def swap(sha, image, old, baseline, state):
    previous = (state / "deployed").read_text().strip() if (state / "deployed").exists() else ""
    journal = {"sha": sha, "old": old, "baseline": baseline, "previous": previous}
    stamp(state, "transaction.json", json.dumps(journal))
    command("docker", "stop", "--time", "30", old)
    try:
        command("docker", "rename", old, NAME + "-previous-" + sha[:12])
        command("docker", "run", "-d", "--name", NAME, "--restart", "unless-stopped", "--network", NETWORK,
                "-p", BIND, "-v", VOLUME + ":/data", "--env-file", str(ENV_FILE), image)
        wait_healthy(baseline)
    except Exception:
        rollback(journal, state)
        raise RuntimeError("deployment failed; rolled back and continuity checked") from None
    stamp(state, "deployed", sha)
    (state / "transaction.json").unlink()
    stamp(state, "result.json", json.dumps({"sha": sha, "home": 200, "admin_protected": True,
                                          "registrations_preserved": len(baseline["registrations"]),
                                          "connected_preserved": len(baseline["connected"])}))


def poll(state):
    if (state / "transaction.json").exists():
        rollback(json.loads((state / "transaction.json").read_text()), state)
        print("Recovered interrupted swap; previous container healthy")
        return
    sha = target(state)
    previous = (state / "deployed").read_text().strip() if (state / "deployed").exists() else ""
    failed = (state / "failed").read_text().strip() if (state / "failed").exists() else ""
    if sha in (previous, failed):
        return
    if not re.fullmatch(r"[0-9a-f]{40}", previous):
        raise RuntimeError("Dora must adopt the verified running revision before enabling the timer")
    if not ci_passed(sha):
        print("Waiting for exact-head full-suite")
        return
    paths = command("git", "--git-dir", str(state / "repo"), "diff", "--name-only", previous, sha).splitlines()
    if not runtime_changed(paths):
        stamp(state, "deployed", sha)
        return
    if not ENV_FILE.is_file() or ENV_FILE.stat().st_mode & 0o077:
        raise RuntimeError("host env file must exist with owner-only permissions")
    info = json.loads(command("docker", "inspect", NAME))[0]
    validate_container(info)
    if not info["State"]["Running"]:
        raise RuntimeError("current relay is not running")
    baseline = probe()
    with tempfile.TemporaryDirectory(dir=state) as tmp:
        archive = Path(tmp) / "source.tar"
        command("git", "--git-dir", str(state / "repo"), "archive", "--format=tar", "--output=" + str(archive), sha)
        with tarfile.open(archive) as tar:
            tar.extractall(tmp, filter="data")
        tag = NAME + ":" + sha
        command("docker", "build", "--network", "host", "--label", "org.opencontainers.image.revision=" + sha,
                "-f", str(Path(tmp) / "server/Dockerfile"), "-t", tag, tmp)
        image = command("docker", "image", "inspect", "--format", "{{.Id}}", tag)
    backups = state / "backups"
    backups.mkdir(mode=0o700, exist_ok=True)
    backup = sha + "-" + str(time.time_ns()) + ".tar.gz"
    stamp(state, "transaction.json", json.dumps({"sha": sha, "old": info["Id"], "baseline": baseline, "previous": previous}))
    command("docker", "pause", info["Id"])
    try:
        command("docker", "run", "--rm", "--network", "none", "--user", "0", "--entrypoint", "tar",
                "-v", VOLUME + ":/data:ro", "-v", str(backups) + ":/backup", image,
                "czf", "/backup/" + backup, "-C", "/data", ".")
        with tarfile.open(backups / backup) as tar:
            if not any(m.name.removeprefix("./") == "registrations.json" for m in tar.getmembers()):
                raise RuntimeError("backup is missing registrations")
            # Reading every byte also verifies the gzip checksum, not just the tar header.
            for member in tar.getmembers():
                if member.isfile():
                    stream = tar.extractfile(member)
                    assert stream is not None
                    with stream:
                        while stream.read(1024 * 1024):
                            pass
    finally:
        command("docker", "unpause", info["Id"])
    swap(sha, image, info["Id"], baseline, state)
    print("Deployed " + sha + "; backup verified; home/admin/registration continuity healthy")


def main():
    import fcntl  # host controller is Linux-only; pure guards are tested on Windows too
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path.home() / ".local/share/livechat-xr-deploy")
    parser.add_argument("--status", action="store_true", help="check exact main revision and live health; never deploy")
    args = parser.parse_args()
    os.umask(0o077)
    args.state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (args.state / "lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.status:
            if (args.state / "transaction.json").exists():
                raise RuntimeError("unfinished deployment transaction")
            sha = target(args.state)
            if (args.state / "deployed").read_text().strip() != sha:
                raise RuntimeError("relay has not caught up to main")
            current = probe()
            print("Revision " + sha + "; home 200; admin protected; registrations "
                  + str(len(current["registrations"])) + "; connected " + str(len(current["connected"])))
        else:
            poll(args.state)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Some exceptions include URLs or runtime details. Emit only controlled errors.
        print(str(error) if type(error) is RuntimeError else "deployment controller failed; Dora must investigate locally")
        raise SystemExit(1) from None
