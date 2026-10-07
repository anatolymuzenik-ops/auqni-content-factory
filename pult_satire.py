"""Optional SMK satire stream inside the existing Pult store, owner UI and tick."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from pult_core import PultError, iso_utc, local_label, moscow_zone, utc_now


MIX = ("pure", "problem", "pure", "soft", "pure",
       "problem", "pure", "soft", "pure", "problem")
MAKEUP = ("pure", "soft", "pure", "pure", "soft",
          "pure", "soft", "pure", "pure", "soft")


def read_bank(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema_version") != "smk-satire-bank/v1":
        raise PultError("Неверная версия банка SMK_SATIRE")
    rows = data.get("candidates")
    if not isinstance(rows, list) or len(rows) < 60:
        raise PultError("Банк SMK_SATIRE неполон")
    ids = set()
    for row in rows:
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k].strip()
            for k in ("id", "text", "genre", "topic")):
            raise PultError("Некорректная запись SMK_SATIRE")
        if row["id"] in ids or row.get("mix_type") not in MIX or not isinstance(row.get("selected"), bool):
            raise PultError("Дубли или некорректная категория SMK_SATIRE")
        ids.add(row["id"])
    selected = [row for row in rows if row["selected"]]
    if len(selected) < 40 or [sum(r["mix_type"] == kind for r in selected) for kind in ("pure", "problem", "soft")] != [20, 12, 8]:
        raise PultError("Нарушена пропорция отбора SMK_SATIRE")
    return rows


class SatireStream:
    def __init__(self, store, bank_path, settings):
        self.store = store
        self.bank = read_bank(bank_path)
        self.settings = settings

    def seed(self):
        with self.store.connect() as db:
            for row in self.bank:
                db.execute("""INSERT OR IGNORE INTO satire_posts
                    (id,text,genre,topic,mix_type,product_context,selected,status,reactions_json)
                    VALUES(?,?,?,?,?,?,?,'unused',?)""",
                    (row["id"], row["text"], row["genre"], row["topic"],
                     row["mix_type"], row.get("product_context"), int(row["selected"]), None))

    def plan(self, now=None):
        """Reserve morning slots; never approve a candidate or publish it."""
        now = (now or utc_now()).astimezone(moscow_zone())
        horizon = int(self.settings.get("horizon_days", 10))
        clock = self.settings.get("time", "08:30")
        hour, minute = map(int, clock.split(":"))
        added = []
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            launch = self.settings.get("launch_queue", [])
            # The owner-approved first ten are intentionally 4/6/0. The next ten
            # restore 50/30/20 across the first twenty planned posts.
            launch_pending = [post_id for post_id in launch if db.execute(
                "SELECT 1 FROM satire_posts WHERE id=? AND status='unused' AND selected=1",
                (post_id,)).fetchone()]
            nonlaunch_count = db.execute(
                "SELECT count(*) FROM satire_posts WHERE status!='unused' AND id NOT IN (" +
                ",".join("?" for _ in launch) + ")", launch).fetchone()[0] if launch else 0
            count = db.execute("SELECT count(*) FROM satire_posts WHERE status NOT IN ('unused','rejected')").fetchone()[0]
            for offset in range(horizon + 1):
                day = (now + timedelta(days=offset)).date()
                if day.weekday() >= 5:
                    continue
                slot = datetime(day.year, day.month, day.day, hour, minute, tzinfo=moscow_zone())
                if slot <= now:
                    continue
                stamp = iso_utc(slot)
                if db.execute("SELECT 1 FROM satire_posts WHERE scheduled_at=?", (stamp,)).fetchone():
                    continue
                if launch_pending:
                    post_id = launch_pending.pop(0)
                    row = db.execute("SELECT id,text FROM satire_posts WHERE id=?", (post_id,)).fetchone()
                else:
                    kind = (MAKEUP[nonlaunch_count] if launch and nonlaunch_count < len(MAKEUP)
                            else MIX[(nonlaunch_count - len(MAKEUP)) % len(MIX)] if launch
                            else MIX[count % len(MIX)])
                    row = db.execute("""SELECT id,text FROM satire_posts
                        WHERE selected=1 AND status='unused' AND mix_type=? ORDER BY id LIMIT 1""", (kind,)).fetchone()
                    nonlaunch_count += 1
                if not row:
                    break
                db.execute("UPDATE satire_posts SET status='scheduled', scheduled_at=? WHERE id=?", (stamp, row["id"]))
                added.append((row["id"], row["text"], stamp))
                count += 1
        return added

    def get(self, post_id):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM satire_posts WHERE id=?", (post_id,)).fetchone()
            return dict(row) if row else None

    def approve(self, post_id, now=None):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,scheduled_at FROM satire_posts WHERE id=?", (post_id,)).fetchone()
            if not row or row["status"] != "scheduled" or row["scheduled_at"] <= iso_utc(now or utc_now()):
                raise PultError("Шутка уже недоступна для принятия; проверьте дату")
            db.execute("UPDATE satire_posts SET status='approved' WHERE id=?", (post_id,))

    def reject(self, post_id):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM satire_posts WHERE id=?", (post_id,)).fetchone()
            if not row or row["status"] not in ("scheduled", "approved"):
                raise PultError("Шутку нельзя отклонить")
            db.execute("UPDATE satire_posts SET status='rejected', scheduled_at=NULL WHERE id=?", (post_id,))

    def due(self, now=None):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute("""SELECT * FROM satire_posts
                WHERE status='approved' AND scheduled_at<=? ORDER BY scheduled_at""", (iso_utc(now or utc_now()),))]

    def claim(self, post_id, now=None):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute("""UPDATE satire_posts SET status='publishing'
                WHERE id=? AND status='approved' AND scheduled_at<=?""", (post_id, iso_utc(now or utc_now())))
            return result.rowcount == 1

    def result(self, post_id, message_id=None):
        with self.store.connect() as db:
            if message_id is None:
                db.execute("UPDATE satire_posts SET status='uncertain' WHERE id=? AND status='publishing'", (post_id,))
            else:
                db.execute("""UPDATE satire_posts SET status='published', published_at=?, external_id=?, public_url=?
                    WHERE id=? AND status='publishing'""", (iso_utc(utc_now()), str(message_id),
                    f"https://t.me/auqni_qms/{message_id}", post_id))

    def recover(self):
        with self.store.connect() as db:
            rows = [r[0] for r in db.execute("SELECT id FROM satire_posts WHERE status='publishing'")]
            db.execute("UPDATE satire_posts SET status='uncertain' WHERE status='publishing'")
            return rows

    def record_reaction_count(self, update):
        """Persist an observed Bot API count snapshot for our published post only."""
        chat = update.get("chat", {})
        message_id = update.get("message_id")
        timestamp = update.get("date")
        reactions = update.get("reactions")
        if (not isinstance(chat, dict) or chat.get("username") != "auqni_qms"
                or type(message_id) is not int or type(timestamp) is not int
                or not isinstance(reactions, list)):
            return False
        counts = []
        for item in reactions:
            if not isinstance(item, dict) or not isinstance(item.get("type"), dict) or type(item.get("total_count")) is not int:
                return False
            counts.append({"type": item["type"], "count": item["total_count"]})
        try:
            observed_at = iso_utc(datetime.fromtimestamp(timestamp, tz=timezone.utc))
        except (OverflowError, OSError, ValueError):
            return False
        snapshot = json.dumps({"observed_at": observed_at, "counts": counts,
                               "total": sum(entry["count"] for entry in counts),
                               "source": "telegram_bot_api_message_reaction_count"}, ensure_ascii=False)
        with self.store.connect() as db:
            row = db.execute("SELECT id,reactions_json FROM satire_posts WHERE external_id=? AND status='published'",
                             (str(message_id),)).fetchone()
            if not row:
                return False
            old = json.loads(row["reactions_json"]) if row["reactions_json"] else None
            if old and old["observed_at"] > observed_at:
                return False
            db.execute("UPDATE satire_posts SET reactions_json=? WHERE id=?", (snapshot, row["id"]))
            return True


def review_label(post_id, text, stamp):
    return f"SMK_SATIRE {post_id} · {local_label(stamp)}\n\n{text}"
