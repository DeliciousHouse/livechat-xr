"""E2/DR8: archive bytes, concurrent writes, unbound restore and nightly shell."""
import asyncio
import contextlib
import io
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.test_network import network, row, config, server
from nacl.signing import SigningKey
import backup


class Snapshots(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.store = network.Store(self.data / "listings.db")
        self.store.initialize()
        self.now = time.time()
        self.store.upsert_presence("twitch", [dict(row(), title="PRIVATE_PLATFORM_TITLE_SENTINEL", platform_id="PRIVATE_PLATFORM_ID_SENTINEL")], self.now)
        self.lid = self.store.channels()[0]["listing_id"]
        self.store.record_supplied_channel(self.lid, "twitch", "streamer")
        self.store.set_listing(self.lid, opted_in=True, review_status="reviewed")
        (self.data / "registrations.json").write_text('{"fixture":true}')
        (self.data / "stats.json").write_text('{}')

    async def test_concurrent_online_snapshot_no_platform_bytes_or_shells(self):
        self.store.upsert_presence("youtube", [dict(row(), platform_id="UNCLAIMED_ID", handle="UNCLAIMED_HANDLE")], self.now)
        started, written = threading.Event(), threading.Event()
        errors = []
        def writer():
            try:
                self.assertTrue(started.wait(3))
                self.store.set_listing(self.lid, opted_in=False, now=self.now)
                written.set()
            except Exception as exc: errors.append(exc)
        thread = threading.Thread(target=writer)
        thread.start()
        def progress(*_):
            started.set()
            self.assertTrue(written.wait(3))  # Actual commit while sqlite backup() is still active.
        try:
            with backup.snapshot_lock(self.data): self.assertTrue(backup.snapshot(self.data, progress=progress))
        finally:
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        # The concurrent snapshot can linearize before the opt-out. The next snapshot must include it.
        with backup.snapshot_lock(self.data): backup.snapshot(self.data)
        target = self.data / "listings.snapshot.db"
        backup.check_snapshot(target)
        data = target.read_bytes()
        for private in (b"PRIVATE_PLATFORM_TITLE_SENTINEL", b"PRIVATE_PLATFORM_ID_SENTINEL", b"UNCLAIMED_ID", b"UNCLAIMED_HANDLE"):
            self.assertNotIn(private, data)
        with backup.connect(target, readonly=True) as db:
            self.assertEqual(db.execute("SELECT id,review_status FROM listings").fetchall(), [(self.lid, "removed")])
            self.assertEqual(db.execute("SELECT handle FROM supplied_channels").fetchall(), [("streamer",)])
            self.assertEqual({r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}, set(backup.DURABLE))

    async def test_restore_real_runtime_unbound_renamed_handle_and_expiry(self):
        # One removed, one active record; both must be unbound after restore.
        self.store.set_listing(self.lid, opted_in=False, now=self.now)
        self.store.upsert_presence("twitch", [dict(row("active"), platform_id="active-id")], self.now)
        active = self.store.channels()[0]["listing_id"]
        self.store.record_supplied_channel(active, "twitch", "active")
        self.store.set_listing(active, opted_in=True, review_status="reviewed")
        with backup.snapshot_lock(self.data): backup.snapshot(self.data)
        restored = self.data / "restored"
        restored.mkdir()
        shutil.copyfile(self.data / "listings.snapshot.db", restored / "listings.snapshot.db")
        for suffix in ("", "-wal", "-shm"): (restored / ("listings.db" + suffix)).write_bytes(b"obsolete")
        with self.assertRaises(ValueError): backup.restore(restored)
        backup.restore(restored, relay_stopped=True)
        self.assertFalse((restored / "listings.db-wal").exists())
        self.assertFalse((restored / "listings.db-shm").exists())
        cfg = config()
        cfg["DISCORD_PUBLIC_KEY"] = SigningKey.generate().verify_key.encode().hex()
        net = network.Network(restored, server.normalise_channel, cfg)
        net.collect_twitch = mock.Mock(return_value=[dict(row("streamer"), platform_id="reassigned-id"),
                                                       dict(row("renamed-active"), platform_id="active-id")])
        net.collect_youtube = mock.Mock(return_value=[])
        net.register_commands = mock.AsyncMock()
        await net.start()
        try:
            for _ in range(100):
                if net.snapshot()["sources"]["twitch"]["last_success"]: break
                await asyncio.sleep(.01)
            self.assertGreater(net.snapshot()["sources"]["twitch"]["last_success"], 0)
            self.assertEqual(net.store.discovery(), [])
            self.assertEqual(net.store.listing(self.lid)["review_status"], "removed")
            self.assertTrue(net.store.listing(active)["opted_in"])
            with self.assertRaises(ValueError):
                net.store.bind_verified_channel(self.lid, "twitch", "streamer", "reassigned-id")
            # Caller has freshly proved the supplied handle's ownership; no implicit handle match.
            net.store.bind_verified_channel(active, "twitch", "active", "active-id")
            self.assertEqual([r["listing_id"] for r in net.store.discovery()], [active])
            with self.assertRaises(ValueError):
                net.store.bind_verified_channel(active, "twitch", "not-supplied", "reassigned-id")
        finally:
            await net.stop()
        # Restore-time expiry prevents an old archive resurrecting overdue removals.
        with mock.patch.object(backup.time, "time", return_value=self.now + 7 * 86400):
            backup.restore(restored, relay_stopped=True)
        with backup.connect(restored / "listings.db", readonly=True) as db:
            self.assertIsNone(db.execute("SELECT id FROM listings WHERE id=?", (self.lid,)).fetchone())
        self.assertIsNone(network.Store(restored / "listings.db").peak("twitch", "active-id"))

    async def test_failure_discards_stale_snapshot_and_temp_corruption_rejected(self):
        with backup.snapshot_lock(self.data): backup.snapshot(self.data)
        target = self.data / "listings.snapshot.db"
        with mock.patch.object(backup, "sanitize", side_effect=ValueError("fail")):
            with self.assertRaises(ValueError), backup.snapshot_lock(self.data): backup.snapshot(self.data)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.data.glob(".listings-snapshot-*")), [])
        target.write_bytes(b"not sqlite")
        before = (self.data / "listings.db").read_bytes()
        with self.assertRaises(sqlite3.DatabaseError): backup.restore(self.data, relay_stopped=True)
        self.assertEqual((self.data / "listings.db").read_bytes(), before)
        with self.store.connect() as db: db.execute("ALTER TABLE listings ADD COLUMN unexpected_peak INTEGER")
        with self.assertRaises(ValueError), backup.snapshot_lock(self.data): backup.snapshot(self.data)
        self.assertFalse(target.exists())

    async def test_foreign_key_violation_fails_snapshot_before_publication(self):
        with backup.snapshot_lock(self.data): backup.snapshot(self.data)
        with contextlib.closing(sqlite3.connect(self.store.path)) as db:
            db.execute("INSERT INTO supplied_channels VALUES(999,'twitch','orphan')")
            db.commit()
        with self.assertRaises(ValueError), backup.snapshot_lock(self.data): backup.snapshot(self.data)
        self.assertFalse((self.data / "listings.snapshot.db").exists())

    async def test_archive_contents_integrity_chat_only_and_corruption(self):
        (self.data / "listings.old.db").write_bytes(b"OLD_PLATFORM_DATA")
        (self.data / ".listings-snapshot-crashed.db").write_bytes(b"OLD_PLATFORM_DATA")
        stream = io.BytesIO()
        backup.archive(self.data, stream)
        archive = self.data / "archive-fixture.tar"
        archive.write_bytes(stream.getvalue())
        self.addCleanup(archive.unlink)
        self.assertTrue(backup.check_archive(archive))
        with tarfile.open(fileobj=io.BytesIO(stream.getvalue())) as tar:
            self.assertEqual(set(tar.getnames()), {"data/registrations.json", "data/stats.json", "data/listings.snapshot.db", "data/network-backup.json"})
            self.assertEqual(tar.extractfile("data/registrations.json").read(), b'{"fixture":true}')
        for private in (b"PRIVATE_PLATFORM_ID_SENTINEL", b"PRIVATE_PLATFORM_TITLE_SENTINEL", b"OLD_PLATFORM_DATA"):
            self.assertNotIn(private, stream.getvalue())
        with tarfile.open(archive, "w") as tar:
            for name, data in (("data/registrations.json", b"{}"), ("data/network-backup.json", b'{"snapshot":true,"schema":1}'),
                               ("data/listings.snapshot.db", b"corrupt")):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        with self.assertRaises(sqlite3.DatabaseError): backup.check_archive(archive)
        chat = self.data / "chat-only"
        chat.mkdir()
        (chat / "registrations.json").write_text('{}')
        (chat / "listings.snapshot.db").write_bytes(b"stale")
        clean = io.BytesIO()
        backup.archive(chat, clean)
        archive.write_bytes(clean.getvalue())
        self.assertFalse(backup.check_archive(archive))
        self.assertFalse((chat / "listings.snapshot.db").exists())


@unittest.skipUnless(sys.platform == "linux", "nightly shell is exercised by Linux CI")
class NightlyShell(unittest.TestCase):
    def test_running_stopped_and_failure_keep_json_and_stamp_only_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            (data / "registrations.json").write_text('{}')
            store = network.Store(data / "listings.db")
            store.initialize()
            store.upsert_presence("twitch", [row()], time.time())
            store.record_supplied_channel(store.channels()[0]["listing_id"], "twitch", "streamer")
            helper = Path(backup.__file__).resolve()
            # Fake only Docker transport; execute the real helper, shell, tar/gzip and checks.
            docker = root / "docker"
            docker.write_text(f'''#!{sys.executable}
import os, subprocess, sys
from pathlib import Path
args=sys.argv[1:]
if args[0]=='ps':
    if os.environ.get('STOPPED')!='1': print('livechat-xr-relay')
elif 'archive' in args:
    if os.environ.get('FAIL')=='1': sys.exit(23)
    sys.exit(subprocess.call([sys.executable, {str(helper)!r}, 'archive', os.environ['FIXTURE_DATA']]))
elif 'sh' in args:
    Path(os.environ['FIXTURE_DATA'], 'last_backup').write_text('stamped')
else: sys.exit(2)
''')
            docker.chmod(0o755)
            env = dict(os.environ, HOME=str(root), PATH=str(root)+os.pathsep+os.environ['PATH'], FIXTURE_DATA=str(data))
            script = str(helper.with_suffix('.sh'))
            for stopped in ('0', '1'):
                result = subprocess.run(['sh', script], env=dict(env, STOPPED=stopped), capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('snapshot integrity ok', result.stdout)
            archive = next((root / 'backups/livechat-xr').glob('*.tar.gz'))
            self.assertTrue(backup.check_archive(archive))
            old = archive.read_bytes()
            (data / 'last_backup').unlink()
            failed = subprocess.run(['sh', script], env=dict(env, FAIL='1'), capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertFalse((data / 'last_backup').exists())
            self.assertEqual(archive.read_bytes(), old)
            self.assertTrue((archive.parent / 'last-result').read_text().startswith('FAILED'))
            self.assertEqual(list(archive.parent.glob('*.tmp')), [])
