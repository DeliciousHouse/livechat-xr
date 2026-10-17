"""Online, sanitized listings snapshots and archives. Stdlib; no relay imports.

Raw listings.db is never restore authority. Restore requires a stopped relay.
"""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time

DURABLE = {
    "listings": {"id", "discord_id", "opted_in", "review_status", "dm_closed", "removed_at"},
    "supplied_channels": {"listing_id", "platform", "handle"},
}
EPHEMERAL = {
    "channels": {"id", "listing_id", "platform", "platform_id", "handle", "observed_at", "verified"},
    "live_streams": {"channel_id", "stream_id", "title", "viewers", "game", "seen_at", "live"},
}


@contextlib.contextmanager
def connect(path, *, readonly=False):
    db = sqlite3.connect(Path(path).resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True, timeout=10)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        yield db
    finally:
        db.close()


def schema(db, expected, version=1):
    if db.execute("PRAGMA user_version").fetchone()[0] != version:
        raise ValueError("unsupported listings schema")
    objects = db.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    if any(kind not in ("table", "index") for kind, _ in objects):
        raise ValueError("unexpected database object")
    if {name for kind, name in objects if kind == "table"} != set(expected):
        raise ValueError("unexpected snapshot tables")
    for table, columns in expected.items():
        if {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')} != columns:
            raise ValueError("unexpected snapshot columns")


def integrity(db):
    if [tuple(r) for r in db.execute("PRAGMA integrity_check")] != [("ok",)]:
        raise ValueError("snapshot integrity failed")
    if db.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("snapshot foreign keys failed")


def expire_removed(db, now):
    # Legacy removals with no reliable timestamp are discarded, never assigned a fresh deadline.
    where = "removed_at<=? OR (review_status='removed' AND removed_at IS NULL)"
    db.execute("DELETE FROM supplied_channels WHERE listing_id IN (SELECT id FROM listings WHERE " + where + ")", (now - 7 * 86400,))
    db.execute("DELETE FROM listings WHERE " + where, (now - 7 * 86400,))


def sanitize(db, now):
    """Allowlist seam: storage changes must update this and the archive/restore tests."""
    schema(db, DURABLE | EPHEMERAL)
    integrity(db)
    with db:
        db.execute("DROP TABLE live_streams")
        db.execute("DROP TABLE channels")
        expire_removed(db, now)
        db.execute("""DELETE FROM listings WHERE opted_in=0 AND review_status='pending'
            AND removed_at IS NULL AND id NOT IN (SELECT listing_id FROM supplied_channels)""")
    db.execute("PRAGMA journal_mode=DELETE")
    db.execute("VACUUM")  # Deleted titles, platform IDs and free pages must not survive as bytes.
    schema(db, DURABLE)
    integrity(db)


def check_snapshot(path):
    with connect(path, readonly=True) as db:
        schema(db, DURABLE)
        integrity(db)


@contextlib.contextmanager
def snapshot_lock(data):
    lock = Path(data) / ".listings-backup-lock"
    lock.mkdir()  # A stale lock fails closed; operators investigate before removing it.
    try:
        yield
    finally:
        lock.rmdir()


def snapshot(data, *, progress=None):
    """Caller holds snapshot_lock. Return False for chat-only, never a stale snapshot."""
    data = Path(data)
    source, output = data / "listings.db", data / "listings.snapshot.db"
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(output) + suffix).unlink(missing_ok=True)
    if not source.exists(): return False
    fd, name = tempfile.mkstemp(prefix=".listings-snapshot-", suffix=".db", dir=data)
    os.close(fd)
    temporary = Path(name)
    deadline = time.monotonic() + 60
    def step(status, remaining, total):
        if time.monotonic() > deadline: raise TimeoutError("snapshot deadline")
        if progress: progress(status, remaining, total)
    try:
        with connect(source, readonly=True) as src, connect(temporary) as dst:
            src.backup(dst, pages=64, progress=step, sleep=.01)
            sanitize(dst, time.time())
        check_snapshot(temporary)
        with temporary.open("rb+") as f: os.fsync(f.fileno())
        os.replace(temporary, output)
        return True
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(temporary) + suffix).unlink(missing_ok=True)


def excluded(name):
    # Never archive raw DB, journals, old snapshots, partial outputs or a stale lock.
    leaf = Path(name).name.lower()
    return (leaf.startswith(("listings", ".listings")) or "snapshot" in leaf
            or leaf.endswith((".db", ".sqlite", ".sqlite3", "-wal", "-shm", "-journal", ".tar", ".tar.gz", ".tgz"))
            or leaf == "network-backup.json")


def archive(data, output):
    data = Path(data)
    with snapshot_lock(data):
        has_snapshot = snapshot(data)
        with tarfile.open(fileobj=output, mode="w|") as tar:
            for path in sorted(data.rglob("*")):
                relative = path.relative_to(data)
                if any(excluded(part) for part in relative.parts): continue
                if path.is_symlink(): raise ValueError("symlink in data volume")
                if path.is_file():
                    with path.open("rb") as f:
                        if f.read(16) == b"SQLite format 3\0": continue
                    tar.add(path, arcname="data/" + relative.as_posix(), recursive=False)
            if has_snapshot: tar.add(data / "listings.snapshot.db", arcname="data/listings.snapshot.db", recursive=False)
            metadata = json.dumps({"snapshot": has_snapshot, "schema": 1}).encode()
            info = tarfile.TarInfo("data/network-backup.json")
            info.size, info.mode = len(metadata), 0o600
            tar.addfile(info, io.BytesIO(metadata))


def check_archive(path):
    with tarfile.open(path, "r:*") as tar:
        members = tar.getmembers()
        names = [m.name for m in members]
        if len(names) != len(set(names)) or "data/registrations.json" not in names:
            raise ValueError("invalid archive membership")
        for member in members:
            if not member.isfile() or not member.name.startswith("data/") or ".." in Path(member.name).parts:
                raise ValueError("unsafe archive member")
            if any(excluded(part) for part in Path(member.name).parts[1:]) and member.name not in ("data/listings.snapshot.db", "data/network-backup.json"):
                raise ValueError("raw or old listings database in archive")
        with tar.extractfile("data/network-backup.json") as f: metadata = json.load(f)
        if metadata not in ({"snapshot": True, "schema": 1}, {"snapshot": False, "schema": 1}):
            raise ValueError("invalid backup manifest")
        present = "data/listings.snapshot.db" in names
        if present != metadata["snapshot"]: raise ValueError("missing or unexpected snapshot")
        if present:
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "snapshot.db"
                with tar.extractfile("data/listings.snapshot.db") as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                check_snapshot(target)
        return present


def restore(data, *, relay_stopped=False):
    if not relay_stopped: raise ValueError("stop the relay before restoring")
    data = Path(data)
    with snapshot_lock(data):
        source, target = data / "listings.snapshot.db", data / "listings.db"
        check_snapshot(source)
        fd, name = tempfile.mkstemp(prefix=".listings-restore-", suffix=".db", dir=data)
        os.close(fd)
        temporary = Path(name)
        try:
            shutil.copyfile(source, temporary)
            with connect(temporary) as db:
                with db: expire_removed(db, time.time())
                db.execute("VACUUM")
                integrity(db)
            # Never replay journals belonging to the old live DB against the snapshot.
            for suffix in ("-wal", "-shm", "-journal"):
                Path(str(target) + suffix).unlink(missing_ok=True)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("snapshot", "archive", "check-archive", "restore"))
    parser.add_argument("path")
    parser.add_argument("--relay-stopped", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "snapshot":
            with snapshot_lock(args.path): present = snapshot(args.path)
            print("snapshot ready" if present else "chat-only: no listings database")
        elif args.command == "archive": archive(args.path, sys.stdout.buffer)
        elif args.command == "check-archive":
            print("snapshot integrity ok" if check_archive(args.path) else "chat-only archive ok (no snapshot)")
        else: restore(args.path, relay_stopped=args.relay_stopped)
    except Exception as exc:
        print("backup failed: " + type(exc).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
