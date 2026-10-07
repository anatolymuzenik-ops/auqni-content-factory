"""Channel-neutral SQLite state for the AUQNI Content Factory control panel."""

from __future__ import annotations

from contextlib import closing, contextmanager
import json
from pathlib import Path
import shutil
import sqlite3

from pult_core import PultError, iso_utc, utc_now


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL CHECK(kind IN ('idea','post')),
        title TEXT NOT NULL,
        brief TEXT NOT NULL,
        status TEXT NOT NULL,
        current_version INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS item_channels (
        item_id INTEGER NOT NULL,
        channel TEXT NOT NULL,
        selected INTEGER NOT NULL DEFAULT 1,
        scheduled_at TEXT,
        approved_version INTEGER,
        status TEXT NOT NULL,
        attempt_version INTEGER,
        published_version INTEGER,
        published_at TEXT,
        external_id TEXT,
        public_url TEXT,
        PRIMARY KEY(item_id, channel),
        FOREIGN KEY(item_id) REFERENCES items(id)
    )""",
    """CREATE TABLE IF NOT EXISTS versions (
        item_id INTEGER NOT NULL,
        version INTEGER NOT NULL,
        content_path TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(item_id, version),
        FOREIGN KEY(item_id) REFERENCES items(id)
    )""",
    """CREATE TABLE IF NOT EXISTS version_channels (
        item_id INTEGER NOT NULL,
        version INTEGER NOT NULL,
        channel TEXT NOT NULL,
        text TEXT NOT NULL,
        media_path TEXT,
        text_sha256 TEXT NOT NULL,
        media_sha256 TEXT,
        PRIMARY KEY(item_id, version, channel),
        FOREIGN KEY(item_id, version) REFERENCES versions(item_id, version)
    )""",
    """CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER NOT NULL,
        instruction TEXT NOT NULL,
        input_json TEXT NOT NULL,
        status TEXT NOT NULL,
        error TEXT,
        created_at TEXT NOT NULL
    )""",
    "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_channel_due ON item_channels(status, scheduled_at)",
    """CREATE TABLE IF NOT EXISTS satire_posts (
        id TEXT PRIMARY KEY, text TEXT NOT NULL, genre TEXT NOT NULL, topic TEXT NOT NULL,
        mix_type TEXT NOT NULL, product_context TEXT, selected INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'unused', scheduled_at TEXT UNIQUE,
        published_at TEXT, external_id TEXT, public_url TEXT, reactions_json TEXT,
        CHECK(mix_type IN ('pure','problem','soft'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_satire_due ON satire_posts(status, scheduled_at)",
    """CREATE TABLE IF NOT EXISTS satire_versions (
        post_id TEXT NOT NULL, version INTEGER NOT NULL, text TEXT NOT NULL,
        genre TEXT NOT NULL, topic TEXT NOT NULL, mix_type TEXT NOT NULL,
        product_context TEXT, source TEXT NOT NULL, instruction TEXT,
        created_at TEXT NOT NULL, PRIMARY KEY(post_id, version),
        FOREIGN KEY(post_id) REFERENCES satire_posts(id)
    )""",
    """CREATE TABLE IF NOT EXISTS satire_submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
        text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'USER_SUBMISSION',
        edited_text TEXT, submitted_at TEXT NOT NULL, reviewed_at TEXT,
        CHECK(kind IN ('text','story','dialogue','joke')),
        CHECK(status IN ('USER_SUBMISSION','MODERATION','APPROVED','REJECTED','PUBLISHED'))
    )""",
)


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self._legacy_schema():
            self._backup_and_migrate()
        with self.connect() as db:
            for statement in SCHEMA:
                db.execute(statement)
            # The satire stream predates text versions and owner idea processing.
            # Add only nullable/defaulted columns to the existing production tables.
            for table, additions in {
                "satire_posts": {"current_version": "INTEGER NOT NULL DEFAULT 1",
                                 "origin": "TEXT NOT NULL DEFAULT 'bank'",
                                 "source_submission_id": "INTEGER"},
                "satire_submissions": {"target_post_id": "TEXT", "base_version": "INTEGER",
                                       "result_post_id": "TEXT", "last_error": "TEXT",
                                       "requested_slot": "TEXT"},
                "satire_versions": {"image_path": "TEXT", "image_sha256": "TEXT",
                                    "visual_role": "TEXT", "visual_prompt": "TEXT",
                                    "visual_source_version": "INTEGER",
                                    "visual_state": "TEXT", "visual_attempts": "INTEGER NOT NULL DEFAULT 0",
                                    "visual_last_attempt": "TEXT"},
            }.items():
                existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                for name, definition in additions.items():
                    if name not in existing:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _legacy_schema(self):
        with closing(sqlite3.connect(self.path)) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(items)")}
            return "scheduled_at" in columns and "approved_version" in columns

    def _backup_and_migrate(self):
        backup = self.path.with_suffix(self.path.suffix + ".before-channels.bak")
        if backup.exists():
            raise PultError("Legacy database backup already exists; migration stopped")
        with closing(sqlite3.connect(self.path)) as src, closing(sqlite3.connect(backup)) as dst:
            src.backup(dst)
        try:
            self._migrate_legacy()
        except Exception:
            # A failed migration must not be retried against a partially changed DB.
            shutil.copyfile(backup, self.path)
            raise

    def _migrate_legacy(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            old_items = [dict(row) for row in db.execute("SELECT * FROM items")]
            old_versions = [dict(row) for row in db.execute("SELECT * FROM versions")]
            # Parse all snapshots before modifying the database.
            materials = []
            for row in old_versions:
                content_path = Path(row["content_path"])
                image_path = Path(row["image_path"])
                content = content_path.read_bytes()
                image = image_path.read_bytes()
                obj = json.loads(content.decode("utf-8-sig"))
                materials.append((row, content, image, obj))
            import hashlib
            db.execute("BEGIN IMMEDIATE")
            db.execute("ALTER TABLE items RENAME TO legacy_items")
            db.execute("ALTER TABLE versions RENAME TO legacy_versions")
            for statement in SCHEMA:
                db.execute(statement)
            for row in old_items:
                base_status = "rejected" if row["status"] == "rejected" else ("idea" if row["current_version"] == 0 else "ready")
                db.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?)",
                           (row["id"], row["kind"], row["title"], row["brief"], base_status,
                            row["current_version"], row["created_at"]))
                channel_status = row["status"] if row["status"] in ("published", "publishing", "uncertain", "rejected", "approved") else ("idea" if row["current_version"] == 0 else "ready")
                db.execute("INSERT INTO item_channels VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (row["id"], "telegram", 1, row["scheduled_at"], row["approved_version"],
                            channel_status,
                            row["current_version"] if channel_status in ("publishing", "uncertain") else None,
                            row["current_version"] if channel_status == "published" else None,
                            row["published_at"], str(row["message_id"]) if row["message_id"] is not None else None,
                            row["public_url"]))
            for row, content, image, obj in materials:
                item_id, version = row["item_id"], row["version"]
                db.execute("INSERT INTO versions VALUES(?,?,?,?,?)",
                           (item_id, version, row["content_path"], hashlib.sha256(content).hexdigest(), row["created_at"]))
                for channel, platform in obj.get("platforms", {}).items():
                    text = platform.get("content", "")
                    media_path = row["image_path"] if channel == "telegram" else None
                    db.execute("INSERT INTO version_channels VALUES(?,?,?,?,?,?,?)",
                               (item_id, version, channel, text, media_path,
                                hashlib.sha256(text.encode()).hexdigest(),
                                hashlib.sha256(image).hexdigest() if media_path else None))
            db.execute("DROP TABLE legacy_versions")
            db.execute("DROP TABLE legacy_items")
            db.commit()

    @staticmethod
    def _channels(db, item_id):
        return {row["channel"]: dict(row) for row in db.execute(
            "SELECT * FROM item_channels WHERE item_id=?", (item_id,))}

    @staticmethod
    def _insert_item(db, kind, title, brief, channels):
        cur = db.execute("INSERT INTO items(kind,title,brief,status,created_at) VALUES(?,?,?,?,?)",
                         (kind, title, brief, "idea" if kind == "idea" else "preparing", iso_utc(utc_now())))
        item_id = cur.lastrowid
        for channel in dict.fromkeys(channels):
            db.execute("INSERT INTO item_channels(item_id,channel,status) VALUES(?,?,?)",
                       (item_id, channel, "idea" if kind == "idea" else "preparing"))
        return item_id

    @staticmethod
    def _slot_free(db, when):
        return (not db.execute("""SELECT 1 FROM item_channels WHERE channel='telegram' AND selected=1
            AND scheduled_at=? AND status!='rejected'""", (when,)).fetchone()
            and not db.execute("""SELECT 1 FROM satire_posts WHERE scheduled_at=?
            AND status!='rejected'""", (when,)).fetchone())

    def slot_is_free(self, when):
        with self.connect() as db:
            return self._slot_free(db, when)

    def create(self, kind, title, brief, channels=()):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._insert_item(db, kind, title, brief, channels)

    def create_scheduled(self, title, brief, when, channel="telegram"):
        """Reuse item creation and reserve a selected slot in one transaction."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if when <= iso_utc(utc_now()) or not self._slot_free(db, when):
                raise PultError("Выбранный слот уже занят или его время прошло")
            item_id = self._insert_item(db, "post", title, brief, (channel,))
            db.execute("UPDATE item_channels SET scheduled_at=? WHERE item_id=? AND channel=?",
                       (when, item_id, channel))
            return item_id

    def get(self, item_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if not row:
                return None
            item = dict(row)
            item["channels"] = self._channels(db, item_id)
            return item

    def list_plan(self):
        with self.connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM items WHERE status != 'rejected'")]
            for row in rows:
                row["channels"] = self._channels(db, row["id"])
        rows = [row for row in rows if not row["channels"] or any(
            channel["selected"] and channel["status"] not in ("published", "rejected")
            for channel in row["channels"].values())]
        def sort_key(row):
            dates = [c["scheduled_at"] for c in row["channels"].values() if c["selected"] and c["scheduled_at"]]
            return (0, min(dates), row["id"]) if dates else (1, "", row["id"])
        return sorted(rows, key=sort_key)

    def selected_channels(self, item_id):
        item = self.get(item_id)
        return [name for name, state in item["channels"].items() if state["selected"]] if item else []

    def set_channels(self, item_id, channels):
        """Change selection without erasing channel history or published results."""
        selected = set(channels)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            item = db.execute("SELECT status,current_version FROM items WHERE id=?", (item_id,)).fetchone()
            if not item or item["status"] == "rejected":
                raise PultError("Материал недоступен")
            current = self._channels(db, item_id)
            for channel, state in current.items():
                if channel not in selected and state["status"] in ("publishing", "uncertain"):
                    raise PultError("Канал с неопределённой отправкой нельзя убрать")
                if channel not in selected:
                    db.execute("UPDATE item_channels SET selected=0, approved_version=NULL WHERE item_id=? AND channel=?", (item_id, channel))
            for channel in selected:
                if channel in current:
                    db.execute("UPDATE item_channels SET selected=1 WHERE item_id=? AND channel=?", (item_id, channel))
                else:
                    has_material = item["current_version"] and db.execute(
                        "SELECT 1 FROM version_channels WHERE item_id=? AND version=? AND channel=?",
                        (item_id, item["current_version"], channel)).fetchone()
                    status = "ready" if has_material else ("needs_material" if item["current_version"] else "idea")
                    db.execute("INSERT INTO item_channels(item_id,channel,status) VALUES(?,?,?)", (item_id, channel, status))

    def occupied_slots(self, channel, exclude_id=None):
        with self.connect() as db:
            return [row[0] for row in db.execute("""
                SELECT scheduled_at FROM item_channels
                WHERE channel=? AND selected=1 AND scheduled_at IS NOT NULL
                  AND status!='rejected' AND item_id != ?
            """, (channel, exclude_id or -1))]

    def set_schedule(self, item_id, channel, when):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            item = db.execute("SELECT status,current_version FROM items WHERE id=?", (item_id,)).fetchone()
            state = db.execute("SELECT * FROM item_channels WHERE item_id=? AND channel=? AND selected=1", (item_id, channel)).fetchone()
            if not item or not state or item["status"] == "rejected" or state["status"] in ("published", "publishing", "uncertain", "rejected"):
                raise PultError("Канал материала недоступен для переноса")
            if when and db.execute("""SELECT 1 FROM item_channels WHERE channel=? AND item_id != ?
                AND selected=1 AND scheduled_at=? AND status!='rejected'""",
                (channel, item_id, when)).fetchone():
                raise PultError("Это время уже занято другим материалом в этом канале")
            if when and channel == "telegram" and db.execute("""SELECT 1 FROM satire_posts
                WHERE scheduled_at=? AND status!='rejected'""", (when,)).fetchone():
                raise PultError("Это время уже занято SMK_SATIRE")
            if item["status"] == "preparing":
                status = "preparing"
            elif item["current_version"]:
                has_material = db.execute("SELECT 1 FROM version_channels WHERE item_id=? AND version=? AND channel=?",
                                          (item_id, item["current_version"], channel)).fetchone()
                status = "ready" if has_material else "needs_material"
            else:
                status = "idea"
            db.execute("UPDATE item_channels SET scheduled_at=?, approved_version=NULL, status=? WHERE item_id=? AND channel=?",
                       (when, status, item_id, channel))

    def add_version(self, item_id, content_path, content_sha, title, channel_materials):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            item = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if not item or item["status"] == "rejected":
                raise PultError("Нельзя изменить отклонённый материал")
            if db.execute("SELECT 1 FROM item_channels WHERE item_id=? AND selected=1 AND status IN ('publishing','uncertain')", (item_id,)).fetchone():
                raise PultError("Нельзя изменить материал во время неопределённой отправки")
            version = item["current_version"] + 1
            db.execute("INSERT INTO versions VALUES(?,?,?,?,?)",
                       (item_id, version, str(content_path), content_sha, iso_utc(utc_now())))
            for channel, material in channel_materials.items():
                db.execute("INSERT INTO version_channels VALUES(?,?,?,?,?,?,?)",
                           (item_id, version, channel, material["text"],
                            str(material["media_path"]) if material.get("media_path") else None,
                            material["text_sha256"], material.get("media_sha256")))
            db.execute("UPDATE items SET kind='post', title=?, status='ready', current_version=? WHERE id=?", (title, version, item_id))
            for state in list(db.execute("SELECT channel,status FROM item_channels WHERE item_id=?", (item_id,))):
                if state["status"] in ("published", "rejected"):
                    continue
                db.execute("UPDATE item_channels SET approved_version=NULL, status=? WHERE item_id=? AND channel=?",
                           ("ready" if state["channel"] in channel_materials else "needs_material", item_id, state["channel"]))
            return version

    def version(self, item_id, version=None):
        item = self.get(item_id)
        if not item:
            return None
        with self.connect() as db:
            row = db.execute("SELECT * FROM versions WHERE item_id=? AND version=?",
                             (item_id, version or item["current_version"])).fetchone()
            if not row:
                return None
            result = dict(row)
            result["channels"] = {r["channel"]: dict(r) for r in db.execute(
                "SELECT * FROM version_channels WHERE item_id=? AND version=?", (item_id, row["version"]))}
            return result

    def approve(self, item_id, version, channel):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            item = db.execute("SELECT status,current_version FROM items WHERE id=?", (item_id,)).fetchone()
            state = db.execute("SELECT * FROM item_channels WHERE item_id=? AND channel=? AND selected=1", (item_id, channel)).fetchone()
            if not item or not state or item["status"] != "ready" or item["current_version"] != version or state["status"] not in ("ready", "approved"):
                raise PultError("Эта версия недоступна для принятия в этом канале")
            if not state["scheduled_at"]:
                raise PultError("Сначала назначьте время публикации")
            if state["scheduled_at"] <= iso_utc(utc_now()):
                raise PultError("Плановое время прошло; сначала перенесите публикацию")
            if channel == "telegram" and db.execute("""SELECT 1 FROM satire_posts
                WHERE scheduled_at=? AND status!='rejected'""", (state["scheduled_at"],)).fetchone():
                raise PultError("Слот уже занят SMK_SATIRE")
            if not db.execute("SELECT 1 FROM version_channels WHERE item_id=? AND version=? AND channel=?", (item_id, version, channel)).fetchone():
                raise PultError("У версии нет материала для этого канала")
            db.execute("UPDATE item_channels SET approved_version=?, status='approved' WHERE item_id=? AND channel=?", (version, item_id, channel))

    def start_edit(self, item_id, version):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            item = db.execute("SELECT status,current_version FROM items WHERE id=?", (item_id,)).fetchone()
            if not item or item["current_version"] != version or item["status"] not in ("ready", "preparing"):
                raise PultError("Эта версия недоступна для изменения")
            if db.execute("SELECT 1 FROM item_channels WHERE item_id=? AND selected=1 AND status IN ('publishing','uncertain')", (item_id,)).fetchone():
                raise PultError("Сначала разберитесь с неопределённой отправкой")
            db.execute("UPDATE items SET status='preparing' WHERE id=?", (item_id,))
            db.execute("""UPDATE item_channels SET approved_version=NULL, status='preparing'
                WHERE item_id=? AND status NOT IN ('published','rejected')""", (item_id,))

    def to_plan(self, item_id, channel):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            item = db.execute("SELECT status,current_version FROM items WHERE id=?", (item_id,)).fetchone()
            state = db.execute("SELECT status FROM item_channels WHERE item_id=? AND channel=? AND selected=1", (item_id, channel)).fetchone()
            if not item or not state or item["status"] == "preparing" or state["status"] in ("publishing", "published", "uncertain", "rejected"):
                raise PultError("Материал нельзя переместить в план")
            has_material = item["current_version"] and db.execute(
                "SELECT 1 FROM version_channels WHERE item_id=? AND version=? AND channel=?",
                (item_id, item["current_version"], channel)).fetchone()
            db.execute("UPDATE item_channels SET approved_version=NULL, status=? WHERE item_id=? AND channel=?",
                       ("ready" if has_material else ("needs_material" if item["current_version"] else "idea"), item_id, channel))

    def reject(self, item_id, channel, retry_slot=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = db.execute("SELECT status,scheduled_at FROM item_channels WHERE item_id=? AND channel=? AND selected=1", (item_id, channel)).fetchone()
            if not state or state["status"] in ("publishing", "published", "uncertain"):
                raise PultError("Материал уже нельзя отклонить в этом канале")
            if retry_slot and state["scheduled_at"] != retry_slot:
                raise PultError("Слот публикации изменился; обновите карточку")
            db.execute("UPDATE item_channels SET status='rejected', approved_version=NULL, scheduled_at=NULL WHERE item_id=? AND channel=?", (item_id, channel))
            if not db.execute("SELECT 1 FROM item_channels WHERE item_id=? AND selected=1 AND status != 'rejected'", (item_id,)).fetchone():
                db.execute("UPDATE items SET status='rejected' WHERE id=?", (item_id,))
            if retry_slot:
                key = f"autoplan_today_rejected:{retry_slot}"
                row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
                rejected = json.loads(row[0]) if row else []
                if item_id not in rejected:
                    rejected.append(item_id)
                db.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (key, json.dumps(rejected)))
                db.execute("DELETE FROM kv WHERE key=?", (f"autoplan_today_attempt:{retry_slot[:10]}",))

    def claim_publish(self, item_id, version, channel, scheduled=False, now=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            item = db.execute("SELECT status,current_version FROM items WHERE id=?", (item_id,)).fetchone()
            state = db.execute("SELECT * FROM item_channels WHERE item_id=? AND channel=? AND selected=1", (item_id, channel)).fetchone()
            if not item or not state or item["status"] != "ready" or item["current_version"] != version:
                return False
            if scheduled:
                if state["status"] != "approved" or state["approved_version"] != version or not state["scheduled_at"] or state["scheduled_at"] > iso_utc(now or utc_now()):
                    return False
            elif state["status"] not in ("ready", "approved"):
                return False
            db.execute("UPDATE item_channels SET status='publishing', attempt_version=? WHERE item_id=? AND channel=?", (version, item_id, channel))
            return True

    def due(self, now=None, channels=None):
        selected = set(channels) if channels is not None else None
        with self.connect() as db:
            rows = db.execute("""SELECT c.item_id, i.current_version, c.channel FROM item_channels c
                JOIN items i ON i.id=c.item_id WHERE c.selected=1 AND i.status='ready'
                AND c.status='approved' AND c.approved_version=i.current_version
                AND c.scheduled_at <= ? ORDER BY c.scheduled_at,c.item_id""", (iso_utc(now or utc_now()),))
            return [(r[0], r[1], r[2]) for r in rows if selected is None or r[2] in selected]

    def ideas_to_prepare(self, horizon):
        with self.connect() as db:
            rows = db.execute("""SELECT DISTINCT i.* FROM items i JOIN item_channels c ON c.item_id=i.id
                WHERE i.kind='idea' AND i.current_version=0 AND i.status='idea'
                AND c.selected=1 AND c.status='idea' AND c.scheduled_at IS NOT NULL
                AND c.scheduled_at <= ?
                AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.item_id=i.id)
                ORDER BY i.id""", (iso_utc(horizon),))
            return [dict(r) for r in rows]

    def publication_result(self, item_id, channel, status, external_id=None, url=None):
        if status not in ("published", "uncertain"):
            raise ValueError(status)
        with self.connect() as db:
            db.execute("""UPDATE item_channels SET status=?, approved_version=NULL, published_at=?, published_version=?,
                external_id=?, public_url=? WHERE item_id=? AND channel=? AND status='publishing'""",
                (status, iso_utc(utc_now()) if status == "published" else None,
                 None if status == "uncertain" else db.execute("SELECT attempt_version FROM item_channels WHERE item_id=? AND channel=?", (item_id, channel)).fetchone()[0],
                 str(external_id) if external_id is not None else None, url, item_id, channel))

    def recover_publications(self):
        """A process restart makes an in-flight network result uncertain, never due again."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = [dict(row) for row in db.execute("""SELECT item_id, channel, attempt_version
                FROM item_channels WHERE status='publishing'""")]
            db.execute("""UPDATE item_channels SET status='uncertain', approved_version=NULL
                WHERE status='publishing'""")
            return rows

    def enqueue(self, item_id, instruction, inputs):
        with self.connect() as db:
            item = db.execute("SELECT status FROM items WHERE id=?", (item_id,)).fetchone()
            if not item or item["status"] == "rejected":
                raise PultError("Материал недоступен для подготовки")
            if db.execute("SELECT 1 FROM item_channels WHERE item_id=? AND selected=1 AND status IN ('publishing','uncertain')", (item_id,)).fetchone():
                raise PultError("Материал находится в неопределённой публикации")
            if db.execute("SELECT 1 FROM jobs WHERE item_id=? AND status IN ('queued','running')", (item_id,)).fetchone():
                raise PultError("Для этого материала уже выполняется подготовка")
            cur = db.execute("INSERT INTO jobs(item_id,instruction,input_json,status,created_at) VALUES(?,?,?,?,?)",
                             (item_id, instruction, json.dumps(inputs, ensure_ascii=False), "queued", iso_utc(utc_now())))
            return cur.lastrowid

    def claim_job(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE jobs SET status='running' WHERE id=?", (row["id"],))
            return dict(row) if row else None

    def finish_job(self, job_id, error=None):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status=?, error=? WHERE id=?", ("failed" if error else "done", error, job_id))

    def recover_jobs(self):
        with self.connect() as db:
            cur = db.execute("UPDATE jobs SET status='failed', error='Interrupted by process restart' WHERE status='running'")
            return cur.rowcount

    def set_pending(self, value):
        with self.connect() as db:
            db.execute("INSERT INTO kv(key,value) VALUES('pending',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(value, ensure_ascii=False),))

    def get_pending(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM kv WHERE key='pending'").fetchone()
            return json.loads(row[0]) if row else None

    def clear_pending(self):
        with self.connect() as db:
            db.execute("DELETE FROM kv WHERE key='pending'")

    def get_offset(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM kv WHERE key='offset'").fetchone()
            return int(row[0]) if row else 0

    def set_offset(self, value):
        with self.connect() as db:
            db.execute("INSERT INTO kv(key,value) VALUES('offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(value),))

    def get_kv(self, key):
        with self.connect() as db:
            row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def set_kv(self, key, value):
        with self.connect() as db:
            db.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def topic_history(self):
        """Published and planned themes, including rejected ideas, for duplicate avoidance."""
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT id,title,brief,status,created_at FROM items ORDER BY id DESC LIMIT 100")]
