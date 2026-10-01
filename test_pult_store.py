"""Tests for the channel-neutral model. No channel integration is called."""

from datetime import timedelta
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from pult_core import iso_utc, utc_now
from pult_store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "pult.sqlite3"

    def material(self, name="content"):
        path = self.root / (name + ".json")
        path.write_text(json.dumps({"platforms": {"telegram": {"content": "Telegram text"},
                                                  "vk": {"content": "VK text"}}}), encoding="utf-8")
        return path

    def test_channels_have_independent_artifacts_schedule_and_results(self):
        store = Store(self.db_path)
        item_id = store.create("post", "Тема", "brief", channels=("telegram", "max"))
        first = iso_utc(utc_now() + timedelta(hours=1))
        second = iso_utc(utc_now() + timedelta(hours=2))
        store.set_schedule(item_id, "telegram", first)
        store.set_schedule(item_id, "max", second)
        content = self.material()
        channel_materials = {
            "telegram": {"text": "Telegram text", "media_path": self.root / "tg.png", "text_sha256": hashlib.sha256(b"Telegram text").hexdigest(), "media_sha256": "tg-hash"},
            "max": {"text": "MAX text", "media_path": self.root / "max.png", "text_sha256": hashlib.sha256(b"MAX text").hexdigest(), "media_sha256": "max-hash"},
        }
        store.add_version(item_id, content, hashlib.sha256(content.read_bytes()).hexdigest(), "Тема", channel_materials)
        v1 = store.version(item_id)
        self.assertEqual(v1["channels"]["telegram"]["text"], "Telegram text")
        self.assertEqual(v1["channels"]["max"]["text"], "MAX text")
        self.assertNotEqual(v1["channels"]["telegram"]["media_path"], v1["channels"]["max"]["media_path"])
        store.approve(item_id, 1, "telegram")
        store.approve(item_id, 1, "max")
        due = store.due(utc_now() + timedelta(hours=1, seconds=2))
        self.assertEqual(due, [(item_id, 1, "telegram")])
        self.assertTrue(store.claim_publish(item_id, 1, "telegram", scheduled=True, now=utc_now() + timedelta(hours=1, seconds=2)))
        store.publication_result(item_id, "telegram", "published", "456", "https://example.test/456")
        item = store.get(item_id)
        self.assertEqual(item["channels"]["telegram"]["published_version"], 1)
        self.assertEqual(item["channels"]["telegram"]["external_id"], "456")
        self.assertEqual(item["channels"]["max"]["status"], "approved")
        store.start_edit(item_id, 1)
        self.assertEqual(store.get(item_id)["channels"]["telegram"]["status"], "published")
        self.assertIsNone(store.get(item_id)["channels"]["max"]["approved_version"])
        store.add_version(item_id, content, hashlib.sha256(content.read_bytes()).hexdigest(), "Тема v2", channel_materials)
        item = store.get(item_id)
        self.assertEqual(item["current_version"], 2)
        self.assertEqual(item["channels"]["telegram"]["published_version"], 1)
        self.assertEqual(item["channels"]["max"]["status"], "ready")
        with closing(sqlite3.connect(self.db_path)) as db:
            item_columns = {r[1] for r in db.execute("PRAGMA table_info(items)")}
            version_columns = {r[1] for r in db.execute("PRAGMA table_info(versions)")}
        self.assertNotIn("scheduled_at", item_columns)
        self.assertNotIn("caption_sha256", version_columns)

    def test_schedule_collision_is_per_channel(self):
        store = Store(self.db_path)
        a = store.create("post", "A", "", channels=("telegram", "max"))
        b = store.create("post", "B", "", channels=("telegram", "max"))
        at = iso_utc(utc_now() + timedelta(days=1))
        store.set_schedule(a, "telegram", at)
        store.set_schedule(b, "max", at)
        with self.assertRaises(Exception):
            store.set_schedule(b, "telegram", at)

    def test_in_flight_publication_becomes_uncertain_after_restart(self):
        store = Store(self.db_path)
        item_id = store.create("post", "Тема", "brief", channels=("telegram",))
        due = utc_now() + timedelta(hours=1)
        store.set_schedule(item_id, "telegram", iso_utc(due))
        content = self.material()
        store.add_version(item_id, content, hashlib.sha256(content.read_bytes()).hexdigest(), "Тема", {
            "telegram": {"text": "Telegram text", "media_path": None,
                         "text_sha256": hashlib.sha256(b"Telegram text").hexdigest(), "media_sha256": None}
        })
        store.approve(item_id, 1, "telegram")
        self.assertTrue(store.claim_publish(item_id, 1, "telegram", scheduled=True,
                                            now=due + timedelta(seconds=1)))
        reopened = Store(self.db_path)
        self.assertEqual(len(reopened.recover_publications()), 1)
        self.assertEqual(reopened.get(item_id)["channels"]["telegram"]["status"], "uncertain")
        self.assertEqual(reopened.due(due + timedelta(minutes=1)), [])
        self.assertFalse(reopened.claim_publish(item_id, 1, "telegram"))

    def test_queued_jobs_survive_restart(self):
        store = Store(self.db_path)
        first = store.create("post", "A", "", channels=("telegram",))
        second = store.create("post", "B", "", channels=("telegram",))
        store.enqueue(first, "prepare A", {"text": "A"})
        store.enqueue(second, "prepare B", {"text": "B"})
        self.assertEqual(store.claim_job()["item_id"], first)
        reopened = Store(self.db_path)
        self.assertEqual(reopened.recover_jobs(), 1)
        self.assertEqual(reopened.claim_job()["item_id"], second)

    def test_legacy_database_is_migrated_with_channel_history(self):
        content = self.material("old")
        image = self.root / "old.png"
        image.write_bytes(b"PNG bytes")
        with closing(sqlite3.connect(self.db_path)) as db:
            db.executescript("""
                CREATE TABLE items(id INTEGER PRIMARY KEY, kind TEXT, title TEXT, brief TEXT,
                    status TEXT, current_version INTEGER, approved_version INTEGER,
                    scheduled_at TEXT, published_at TEXT, message_id INTEGER,
                    public_url TEXT, created_at TEXT);
                CREATE TABLE versions(item_id INTEGER, version INTEGER, content_path TEXT,
                    image_path TEXT, caption_sha256 TEXT, image_sha256 TEXT, created_at TEXT);
                CREATE TABLE jobs(id INTEGER PRIMARY KEY, item_id INTEGER, instruction TEXT,
                    input_json TEXT, status TEXT, error TEXT, created_at TEXT);
                CREATE TABLE kv(key TEXT PRIMARY KEY, value TEXT);
            """)
            db.execute("INSERT INTO items VALUES(1,'post','Old','brief','approved',1,1,?,NULL,NULL,NULL,?)",
                       (iso_utc(utc_now() + timedelta(hours=1)), iso_utc(utc_now())))
            db.execute("INSERT INTO items VALUES(2,'post','Uncertain','brief','uncertain',1,NULL,NULL,NULL,NULL,NULL,?)",
                       (iso_utc(utc_now()),))
            db.execute("INSERT INTO versions VALUES(1,1,?,?,?,?,?)",
                       (str(content), str(image), "old-caption-hash", "old-image-hash", iso_utc(utc_now())))
            db.execute("INSERT INTO versions VALUES(2,1,?,?,?,?,?)",
                       (str(content), str(image), "old-caption-hash", "old-image-hash", iso_utc(utc_now())))
            db.commit()
        store = Store(self.db_path)
        item = store.get(1)
        self.assertEqual(item["channels"]["telegram"]["status"], "approved")
        self.assertEqual(item["channels"]["telegram"]["approved_version"], 1)
        self.assertEqual(store.version(1)["channels"]["telegram"]["text"], "Telegram text")
        self.assertEqual(store.get(2)["channels"]["telegram"]["status"], "uncertain")
        self.assertEqual(store.get(2)["channels"]["telegram"]["attempt_version"], 1)
        self.assertEqual(store.create("idea", "Next", "brief", channels=("telegram",)), 3)
        self.assertTrue(self.db_path.with_suffix(".sqlite3.before-channels.bak").is_file())


if __name__ == "__main__":
    unittest.main()
