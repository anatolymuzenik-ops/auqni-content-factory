import json
import os
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pult_core import next_slot, parse_local_time, utc_now
from pult_store import Store
from telegram_pult import Pult


PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000b49444154789c636000020000050001a5f645400000000049454e44ae426082")
SCHEDULE = {"0": "19:00", "1": "18:00", "2": "13:00", "3": "18:00"}


class FakeAPI:
    def __init__(self):
        self.calls = []

    def call(self, method, fields=None, files=None, timeout=35):
        self.calls.append((method, fields, files))
        return {"message_id": len(self.calls)}

    def download(self, file_id, target, limit):
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_bytes(b"voice")
        return Path(target)

    def transcribe(self, path, model):
        return "Идея из голосового"


class FakePipeline:
    def __init__(self, root):
        self.root = root
        self.calls = []

    def run(self, item, instruction, inputs, previous=None):
        self.calls.append((item["id"], instruction, inputs, previous))
        version = item["current_version"] + 1
        content_path = self.root / f"generated-{item['id']}-{version}.json"
        image_path = self.root / f"generated-{item['id']}-{version}.png"
        caption = "СМК работает каждый день. Версия " + str(version) + "."
        obj = {"schema_version": "auqni-content/v1", "publication_status": "draft",
               "source": {}, "editorial_brief": {"topic": "СМК работает каждый день"},
               "claims": [], "image_prompt": "СМК работает каждый день", "media": {}, "review": {},
               "platforms": {name: {"content": caption, "claim_ids": []}
                             for name in ("telegram", "website", "vk", "instagram")}}
        content_path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        image_path.write_bytes(PNG)
        return content_path, image_path


class FakePublisher:
    def __init__(self):
        self.dry_runs = []
        self.sent = []
        self.fail = False

    def prepare(self, content, image):
        return {"text": content["platforms"]["telegram"]["content"], "media_path": image}

    def validate(self, content, text, image, root):
        from telegram_channel import validate_telegram_material
        import json
        assert text == json.loads(Path(content).read_text(encoding="utf-8"))["platforms"]["telegram"]["content"]
        return validate_telegram_material(content, image, root)

    def dry_run(self, content, image):
        self.dry_runs.append((content, image))
        return {"validation": "ok", "network_requests": 0}

    def publish(self, content, image):
        self.sent.append((content, image))
        if self.fail:
            raise RuntimeError("transport failure")
        return 812, "https://t.me/auqni_qms/812"


class PultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.api = FakeAPI()
        self.pipeline = FakePipeline(self.root)
        self.publisher = FakePublisher()
        self.owner = 12345
        env = patch.dict(os.environ, {"AUQNI_OWNER_USER_ID": str(self.owner)})
        env.start(); self.addCleanup(env.stop)
        self.store = Store(self.root / "pult_data" / "pult.sqlite3")
        self.pult = Pult(self.root, {"channels": {"telegram": {"enabled": True, "schedule": SCHEDULE}}, "data_dir": "pult_data"},
                         self.api, self.store, self.pipeline, self.publisher)

    def message(self, text=None, user=None, **extra):
        payload = {"from": {"id": user or self.owner},
                   "chat": {"id": user or self.owner, "type": "private"}}
        if text is not None:
            payload["text"] = text
        payload.update(extra)
        self.pult.handle_message(payload)

    def callback(self, data, user=None):
        self.pult.handle_callback({"id": "callback", "from": {"id": user or self.owner},
                                   "message": {"chat": {"id": user or self.owner}}, "data": data})

    def make_post(self):
        self.message("Создать пост")
        self.message("СМК в медицинской организации")
        self.callback("confirm:create")
        self.assertTrue(self.pult.process_one_job())
        item = self.store.get(1)
        self.assertEqual(item["current_version"], 1)
        return item

    def test_create_review_approve_and_edit_revokes_approval(self):
        self.make_post()
        self.assertTrue(any(method == "sendPhoto" for method, _, _ in self.api.calls))
        self.assertTrue(self.publisher.dry_runs)
        self.callback("approve:1:1")
        self.assertEqual(self.store.get(1)["channels"]["telegram"]["approved_version"], 1)
        self.callback("edit:1:1")
        self.message("Текст оставь, картинку сделай Light Tech")
        self.assertIsNone(self.store.get(1)["channels"]["telegram"]["approved_version"])
        self.assertTrue(self.pult.process_one_job())
        self.assertEqual(self.store.get(1)["current_version"], 2)
        self.assertIsNone(self.store.get(1)["channels"]["telegram"]["approved_version"])
        with self.assertRaises(Exception):
            self.callback("approve:1:1")

    def test_scheduled_publication_requires_approval(self):
        from datetime import timedelta
        from pult_core import iso_utc
        item = self.make_post()
        due = utc_now() + timedelta(hours=1)
        self.store.set_schedule(1, "telegram", iso_utc(due))
        self.pult.tick()
        self.assertFalse(self.publisher.sent)
        self.callback("approve:1:1")
        self.pult.tick(due + timedelta(seconds=1))
        self.assertEqual(len(self.publisher.sent), 1)
        published = self.store.get(1)
        self.assertEqual(published["channels"]["telegram"]["status"], "published")
        self.assertEqual(published["channels"]["telegram"]["external_id"], "812")
        self.assertEqual(published["channels"]["telegram"]["public_url"], "https://t.me/auqni_qms/812")
        notice = self.api.calls[-1][1]["text"]
        self.assertIn("№1 v1", notice)
        self.assertIn("Статус: опубликовано", notice)
        self.assertIn("Дата:", notice)
        self.assertIn("https://t.me/auqni_qms/812", notice)
        self.pult.tick()
        self.assertEqual(len(self.publisher.sent), 1)
        restarted = Pult(self.root, self.pult.config, self.api,
                         Store(self.store.path), self.pipeline, self.publisher)
        restarted.tick(due + timedelta(minutes=1))
        self.assertEqual(len(self.publisher.sent), 1)
        self.assertEqual(restarted.store.get(1)["channels"]["telegram"]["status"], "published")

    def test_approved_schedule_survives_store_restart(self):
        from datetime import timedelta
        from pult_core import iso_utc
        self.make_post()
        due = utc_now() + timedelta(hours=1)
        self.store.set_schedule(1, "telegram", iso_utc(due))
        self.callback("approve:1:1")
        reopened = Store(self.store.path)
        self.assertEqual(reopened.due(due + timedelta(seconds=1)), [(1, 1, "telegram")])
        self.assertEqual(reopened.get(1)["channels"]["telegram"]["approved_version"], 1)

    def test_uncertain_result_is_not_retried(self):
        self.make_post()
        self.publisher.fail = True
        self.callback("publish:1:1")
        self.assertEqual(self.store.get(1)["channels"]["telegram"]["status"], "uncertain")
        self.assertEqual(len(self.publisher.sent), 1)
        self.pult.tick()
        self.assertEqual(len(self.publisher.sent), 1)

    def test_private_access_ideas_and_plan_controls(self):
        self.message("Создать пост", user=999)
        self.assertIsNone(self.store.get_pending())
        self.callback("idea:new", user=999)
        self.assertIsNone(self.store.get_pending())
        self.callback("idea:new")
        self.message(voice={"file_id": "voice-id", "file_size": 100})
        self.assertEqual(self.store.get(1)["kind"], "idea")
        self.callback("make:1")
        self.assertTrue(self.pult.process_one_job())
        future_date = (utc_now() + timedelta(days=7)).strftime("%d.%m.%Y")
        self.message(f"Перенеси №1 на {future_date} 19:00")
        self.assertEqual(self.store.get(1)["status"], "ready")
        self.message("Убери №1")
        self.assertIsNone(self.store.get(1)["channels"]["telegram"]["scheduled_at"])
        self.message("Контент-план")
        self.assertIn("№1", self.api.calls[-1][1]["text"])

    def test_edit_in_progress_cannot_be_reapproved_or_published(self):
        self.make_post()
        self.callback("approve:1:1")
        self.callback("edit:1:1")
        self.message("Измени изображение")
        self.assertEqual(self.store.get(1)["status"], "preparing")
        self.assertFalse(self.store.claim_publish(1, 1, "telegram", scheduled=True))
        with self.assertRaises(Exception):
            self.store.to_plan(1, "telegram")
        self.assertFalse(self.publisher.sent)

    def test_photo_video_document_inputs_are_downloaded(self):
        self.message("Создать пост")
        self.message(caption="Сделай пост по материалам",
                     photo=[{"file_id": "photo", "file_size": 50}],
                     video={"file_id": "video", "file_size": 50},
                     document={"file_id": "doc", "file_size": 50, "file_name": "report.pdf"})
        pending = self.store.get_pending()
        self.assertEqual({a["type"] for a in pending["inputs"]["attachments"]},
                         {"photo", "video", "document"})
        self.callback("confirm:create")
        self.assertTrue(self.pult.process_one_job())

    def test_scheduled_idea_is_prepared_ahead_but_not_published(self):
        self.callback("idea:new")
        self.message("Как сохранять результаты аудита")
        from datetime import timedelta
        from pult_core import iso_utc
        self.store.set_schedule(1, "telegram", iso_utc(utc_now() + timedelta(hours=24)))
        self.pult.tick()
        self.assertTrue(self.pult.process_one_job())
        self.assertEqual(self.store.get(1)["status"], "ready")
        self.assertIsNone(self.store.get(1)["channels"]["telegram"]["approved_version"])
        self.assertFalse(self.publisher.sent)

    def test_immutable_snapshot_blocks_tampering(self):
        self.make_post()
        version = self.store.version(1)
        Path(version["channels"]["telegram"]["media_path"]).write_bytes(PNG + b"changed")
        with self.assertRaises(Exception):
            self.pult.publish_item(1, 1, scheduled=False)
        self.assertFalse(self.publisher.sent)

    def test_schedule_configuration(self):
        slot = next_slot(SCHEDULE, [], utc_now())
        self.assertIn("T", slot)
        future_date = (utc_now() + timedelta(days=7)).strftime("%d.%m.%Y")
        moved = parse_local_time(f"{future_date} 19:00")
        self.assertTrue(moved.endswith("+00:00"))


if __name__ == "__main__":
    unittest.main()
