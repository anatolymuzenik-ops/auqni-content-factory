import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from pult_core import PultError
from pult_satire import SatireStream, read_bank, validate_candidate, reuse_visual, visual_file
from pult_store import Store
from telegram_pult import Pult


ROOT = Path(__file__).resolve().parent


def sample_visual(root, name="sample.png"):
    target = Path(root) / "images/smk_satire" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    scan = (b"\x00" + b"\xff\xff\xff" * 1254) * 1254
    target.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1254, 1254, 8, 2, 0, 0, 0))
                       + chunk(b"IDAT", zlib.compress(scan)) + chunk(b"IEND", b""))
    path, digest = visual_file(target, root)
    return {"image_path": path, "image_sha256": digest, "visual_role": "тестовый персонаж",
            "visual_prompt": "тестовая сцена"}


def prepare_bank_visual(stream, post_id, root):
    job, _ = stream.claim_visual()
    assert job["post_id"] == post_id
    stream.finish_visual(post_id, job["version"], sample_visual(root, post_id + ".png"))


class SatireTests(unittest.TestCase):
    def test_two_owner_edits_use_the_same_pending_input_without_crossing_posts(self):
        class API:
            def __init__(self):
                self.calls = []

            def call(self, method, payload, files=None):
                self.calls.append((method, payload))
                return {"message_id": len(self.calls)}

            def download(self, file_id, target, limit):
                Path(target).parent.mkdir(parents=True, exist_ok=True)
                Path(target).write_bytes(b"voice")

            def transcribe(self, path, model):
                return "Голосовая правка для второго поста"

        class Writer:
            def __init__(self):
                self.instructions = []

            def run(self, idea, previous=None, instruction=None, mix_type=None):
                self.instructions.append((idea, previous, instruction))
                return {"text": f"После правки {idea} рабочая ситуация стала яснее. Теперь команда видит проблему и её последствия.",
                        "genre": "новая сатира", "topic": f"новая тема {idea}",
                        "mix_type": mix_type, "product_context": None}

        class Generator:
            def __init__(self):
                self.calls = 0

            def run(self, candidate, role):
                self.calls += 1
                return sample_visual(root, f"edited-{self.calls}.png")

        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"AUQNI_OWNER_USER_ID": "123"}):
            root = Path(temp)
            (root / "content").mkdir()
            (root / "content/smk_satire_bank.json").write_bytes((ROOT / "content/smk_satire_bank.json").read_bytes())
            api, writer, generator = API(), Writer(), Generator()
            pult = Pult(root, json.loads((ROOT / "pult_config.json").read_text()), api,
                        satire_writer=writer, satire_image_generator=generator)
            before = datetime(2099, 10, 7, 4, tzinfo=timezone.utc)
            first, second = [post_id for post_id, _, _ in pult.satire.plan(before)[:2]]
            for post_id in (first, second):
                prepare_bank_visual(pult.satire, post_id, root)
                pult.satire.approve(post_id, before, version=1)
            originals = {post_id: pult.satire.version(post_id, 1) for post_id in (first, second)}

            def click(post_id, version):
                pult.handle_callback({"id": f"edit-{post_id}", "from": {"id": 123},
                                      "message": {"chat": {"id": 123}},
                                      "data": f"satire:edit:{post_id}:{version}"})

            def send(**content):
                pult.handle_message({"from": {"id": 123}, "chat": {"id": 123, "type": "private"},
                                     **content})

            click(first, 1)
            self.assertEqual(pult.store.get_pending(), {"kind": "edit_input", "post_id": first, "version": 1})
            self.assertEqual(pult.satire.get(first)["status"], "editing")
            self.assertNotIn(first, [row["id"] for row in
                                     pult.satire.due(datetime(2099, 10, 10, tzinfo=timezone.utc))])
            with self.assertRaises(PultError):
                click(second, 1)
            self.assertEqual(pult.store.get_pending()["post_id"], first)
            self.assertEqual(pult.satire.get(second)["status"], "approved")
            send(text="Текстовая правка для первого поста")
            self.assertEqual(pult.satire.get(first)["status"], "editing")
            self.assertEqual(pult.satire.get(second)["status"], "approved")
            self.assertTrue(pult.process_one_satire_submission())
            self.assertEqual(pult.satire.get(first)["current_version"], 2)
            self.assertEqual(pult.satire.get(second)["current_version"], 1)

            click(second, 1)
            send(voice={"file_id": "second-voice", "file_size": 100})
            self.assertEqual(pult.satire.get(second)["status"], "editing")
            self.assertTrue(pult.process_one_satire_submission())
            self.assertEqual([instruction for _, _, instruction in writer.instructions],
                             ["Текстовая правка для первого поста", "Голосовая правка для второго поста"])
            self.assertEqual(generator.calls, 2)
            self.assertIsNone(pult.store.get_pending())
            for post_id in (first, second):
                post = pult.satire.get(post_id)
                version = pult.satire.version(post_id, 2)
                self.assertEqual(post["current_version"], 2)
                self.assertEqual(post["status"], "scheduled")
                self.assertNotEqual(version["image_path"], originals[post_id]["image_path"])
                self.assertEqual(pult.satire.version(post_id, 1)["image_path"], originals[post_id]["image_path"])
                with self.assertRaises(PultError):
                    pult.satire.approve(post_id, before, version=1)
                pult.satire.approve(post_id, before, version=2)
            reviews = [payload for method, payload in api.calls if method == "sendPhoto"]
            self.assertEqual(len(reviews), 2)
            for post_id, payload in zip((first, second), reviews):
                buttons = json.loads(payload["reply_markup"])["inline_keyboard"][0]
                self.assertEqual([button["text"] for button in buttons],
                                 ["Принято", "Изменить", "Отклонить"])
                self.assertTrue(buttons[0]["callback_data"].startswith(f"satire:approve:{post_id}:2:"))

    def test_scheduled_bank_joke_is_expanded_before_visual_review(self):
        class API:
            def __init__(self):
                self.calls = []

            def call(self, method, payload, files=None):
                self.calls.append((method, payload))
                return {"message_id": 1}

        class Writer:
            def run(self, idea, **kwargs):
                self.assertions.append((idea, kwargs))
                return {"text": idea + "\n\nЗа шуткой — задержка согласований, которая тормозит работу команды.",
                        "genre": "СМК-News", "topic": "Согласования", "mix_type": kwargs["mix_type"],
                        "product_context": None}

            def __init__(self):
                self.assertions = []

        class Generator:
            def run(self, candidate, role):
                return sample_visual(root)

        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"AUQNI_OWNER_USER_ID": "123"}):
            root = Path(temp)
            (root / "content").mkdir()
            (root / "content/smk_satire_bank.json").write_bytes((ROOT / "content/smk_satire_bank.json").read_bytes())
            api, writer = API(), Writer()
            pult = Pult(root, json.loads((ROOT / "pult_config.json").read_text()), api,
                        satire_writer=writer, satire_image_generator=Generator())
            post_id = pult.satire.plan(datetime(2099, 10, 7, 4, tzinfo=timezone.utc))[0][0]
            original = pult.satire.get(post_id)["text"]
            self.assertTrue(pult.process_one_satire_visual())
            post = pult.satire.get(post_id)
            self.assertEqual(post["current_version"], 2)
            self.assertEqual(post["status"], "scheduled")
            self.assertEqual(pult.satire.version(post_id, 2)["image_sha256"],
                             pult.satire.version(post_id, 1)["image_sha256"])
            self.assertTrue(post["text"].startswith(original + "\n\n"))
            self.assertTrue(writer.assertions[0][1]["preserve_opening"])
            reviews = [payload for method, payload in api.calls if method == "sendPhoto"]
            self.assertEqual(len(reviews), 1)
            buttons = json.loads(reviews[0]["reply_markup"])["inline_keyboard"][0]
            approval = next(b["callback_data"] for b in buttons if b["text"] == "Принято")
            self.assertIn(f"satire:approve:{post_id}:2:", approval)
            self.assertEqual(pult.store.get_kv(f"satire_editorial_review:{post_id}:2"), "sent")
            with self.assertRaises(PultError):
                pult.satire.approve(post_id, version=1)
            self.assertFalse(pult.revise_scheduled_satire(post_id))

    def test_ready_bank_visual_is_expanded_once_and_bad_opening_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            stream = SatireStream(Store(Path(temp) / "pult.sqlite3"),
                                  ROOT / "content/smk_satire_bank.json", {"time": "08:30"}, temp)
            stream.seed()
            post_id = stream.plan(datetime(2099, 10, 7, 4, tzinfo=timezone.utc))[0][0]
            prepare_bank_visual(stream, post_id, temp)
            self.assertEqual([row["id"] for row in stream.scheduled_editorial_candidates()], [post_id])
            with self.assertRaises(PultError):
                stream.revise_scheduled_text(post_id, 1, "Другая шутка.\n\nПрофессиональная мысль.")
            self.assertEqual(stream.get(post_id)["current_version"], 1)
            expanded = stream.get(post_id)["text"] + "\n\nЗа шуткой стоит потеря времени на согласовании."
            self.assertEqual(stream.revise_scheduled_text(post_id, 1, expanded), 2)
            self.assertEqual(stream.scheduled_editorial_candidates(), [])
            self.assertEqual(stream.pending_editorial_reviews()[0]["id"], post_id)

    def test_abandoned_edit_recovery_keeps_the_active_pending_post(self):
        with tempfile.TemporaryDirectory() as temp:
            stream = SatireStream(Store(Path(temp) / "pult.sqlite3"),
                                  ROOT / "content/smk_satire_bank.json", {"time": "08:30"}, temp)
            stream.seed()
            first, second = [post_id for post_id, _, _ in
                             stream.plan(datetime(2099, 10, 7, 4, tzinfo=timezone.utc))[:2]]
            stream.begin_edit(first, 1)
            stream.begin_edit(second, 1)
            self.assertEqual(stream.recover_abandoned_edits(first), [second])
            self.assertEqual(stream.get(first)["status"], "editing")
            self.assertEqual(stream.get(second)["status"], "scheduled")
            self.assertEqual(stream.recover_abandoned_edits(), [first])

    def test_visual_is_required_and_old_approval_is_revoked(self):
        with tempfile.TemporaryDirectory() as temp:
            stream = SatireStream(Store(Path(temp) / "pult.sqlite3"),
                                  ROOT / "content/smk_satire_bank.json", {"time": "08:30"}, temp)
            stream.seed()
            before = datetime(2026, 10, 9, 4, 0, tzinfo=timezone.utc)
            post_id = stream.plan(before)[0][0]
            with self.assertRaises(PultError):
                stream.approve(post_id, before)
            with stream.store.connect() as db:
                db.execute("UPDATE satire_posts SET status='approved' WHERE id=?", (post_id,))
            self.assertEqual(stream.recover_visuals(), [post_id])
            self.assertEqual(stream.get(post_id)["status"], "scheduled")
            self.assertFalse(stream.due(datetime(2026, 10, 9, 5, 31, tzinfo=timezone.utc)))
            prepare_bank_visual(stream, post_id, temp)
            stream.approve(post_id, before)
            self.assertTrue(stream.due(datetime(2026, 10, 9, 5, 31, tzinfo=timezone.utc)))

    def test_visual_reuse_only_for_small_same_topic_edits(self):
        old = {"text": "Показатель достигнут. Теперь предстоит выяснить, что он показывал.",
               "genre": "СМК-News", "topic": "Показатели", "image_path": "/some/image.png"}
        self.assertTrue(reuse_visual(old, {"text": old["text"] + "!", "genre": "СМК-News", "topic": "Показатели"}))
        self.assertFalse(reuse_visual(old, {"text": "На аудите нашли новый процесс, которого раньше никто не видел.",
                                            "genre": "СМК-News", "topic": "Аудит"}))

    def test_reject_image_card_prevents_publication(self):
        class API:
            def __init__(self):
                self.calls = []

            def call(self, method, payload, files=None):
                self.calls.append((method, payload))
                return {"message_id": 1}

        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"AUQNI_OWNER_USER_ID": "123"}):
            root = Path(temp)
            (root / "content").mkdir()
            (root / "content/smk_satire_bank.json").write_bytes((ROOT / "content/smk_satire_bank.json").read_bytes())
            api = API()
            pult = Pult(root, json.loads((ROOT / "pult_config.json").read_text()), api)
            before = datetime(2026, 10, 9, 4, 0, tzinfo=timezone.utc)
            post_id = pult.satire.plan(before)[0][0]
            job, _ = pult.satire.claim_visual()
            pult.satire.finish_visual(post_id, job["version"], sample_visual(root))
            pult.show_satire_review(post_id)
            buttons = json.loads(api.calls[-1][1]["reply_markup"])["inline_keyboard"][0]
            reject = next(b["callback_data"] for b in buttons if b["text"] == "Отклонить")
            pult.handle_callback({"id": "reject", "from": {"id": 123}, "message": {"chat": {"id": 123}},
                                  "data": reject})
            self.assertEqual(pult.satire.get(post_id)["status"], "rejected")
            pult.tick_satire(datetime(2026, 12, 1, tzinfo=timezone.utc))
            self.assertFalse(any(payload.get("chat_id") == "@auqni_qms" for _, payload in api.calls))

    def test_bank_edit_keeps_versions_and_revokes_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            stream = SatireStream(Store(Path(temp) / "pult.sqlite3"),
                                  ROOT / "content/smk_satire_bank.json",
                                  {"time": "08:30", "weekdays": [0, 1, 2, 3, 4], "horizon_days": 1})
            stream.seed()
            stream.root = Path(temp)
            before = datetime(2026, 10, 9, 4, 0, tzinfo=timezone.utc)
            post_id = stream.plan(before)[0][0]
            original = stream.get(post_id)["text"]
            prepare_bank_visual(stream, post_id, temp)
            stream.approve(post_id, before, version=1)
            stream.begin_edit(post_id, 1)
            submission_id = stream.queue_edit(post_id, 1, "Сделай финал о четвёртом согласовании")
            self.assertEqual(stream.get(post_id)["status"], "editing")
            self.assertEqual(stream.due(datetime(2026, 10, 9, 5, 31, tzinfo=timezone.utc)), [])
            with stream.store.connect() as db:
                submission = dict(db.execute("SELECT * FROM satire_submissions WHERE id=?", (submission_id,)).fetchone())
            stream.claim_submission()
            candidate = {"text": "Согласовали сокращение маршрута документа. Теперь четыре подписи ставят в одной комнате.",
                         "genre": "СМК-News", "topic": "Согласование", "mix_type": stream.get(post_id)["mix_type"],
                         "product_context": None}
            stream.finish_submission(submission, candidate, sample_visual(temp, "edit-2.png"))
            self.assertEqual(stream.version(post_id, 1)["text"], original)
            self.assertEqual(stream.version(post_id, 2)["text"], candidate["text"])
            self.assertEqual(stream.get(post_id)["current_version"], 2)
            self.assertEqual(stream.get(post_id)["status"], "scheduled")
            with self.assertRaises(PultError):
                stream.approve(post_id, before, version=1)
            stream.approve(post_id, before, version=2)
            self.assertEqual(stream.get(post_id)["status"], "approved")
            stream.begin_edit(post_id, 2)
            second_id = stream.queue_edit(post_id, 2, "Укороти первую фразу")
            with stream.store.connect() as db:
                second = dict(db.execute("SELECT * FROM satire_submissions WHERE id=?", (second_id,)).fetchone())
            stream.claim_submission()
            next_candidate = {**candidate, "text": "Маршрут согласования сократили. Подписи остались те же — теперь все четыре ставят в одной комнате."}
            stream.finish_submission(second, next_candidate, sample_visual(temp, "edit-3.png"))
            self.assertEqual(stream.get(post_id)["current_version"], 3)
            self.assertEqual(stream.version(post_id, 2)["text"], candidate["text"])
            self.assertEqual(stream.version(post_id, 3)["text"], next_candidate["text"])

    def test_owner_idea_goes_to_review_then_free_weekday_slot(self):
        class API:
            def __init__(self):
                self.calls = []

            def call(self, method, payload, files=None):
                self.calls.append((method, payload))
                return {"message_id": 1}

        class Writer:
            def run(self, idea, previous=None, instruction=None, mix_type=None):
                assert idea == "Хаос — это такой порядок"
                return {"text": "В отделе качества хаос назвали порядком. Теперь его нужно согласовать и внести в реестр процессов.",
                        "genre": "управленческий парадокс", "topic": "процессы", "mix_type": "soft",
                        "product_context": "сбор болей и продуктовых идей"}

        class Generator:
            def __init__(self, root):
                self.root = root
                self.calls = 0

            def run(self, candidate, role):
                self.calls += 1
                return sample_visual(self.root, f"idea-{self.calls}.png")

        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"AUQNI_OWNER_USER_ID": "123"}):
            root = Path(temp)
            (root / "content").mkdir()
            (root / "content/smk_satire_bank.json").write_bytes((ROOT / "content/smk_satire_bank.json").read_bytes())
            config = json.loads((ROOT / "pult_config.json").read_text())
            api = API()
            pult = Pult(root, config, api, satire_writer=Writer(), satire_image_generator=Generator(root))
            pult.handle_message({"from": {"id": 123}, "chat": {"id": 123, "type": "private"}, "text": "Создать пост"})
            pult.handle_message({"from": {"id": 123}, "chat": {"id": 123, "type": "private"},
                                 "text": "Хаос — это такой порядок"})
            confirm = api.calls[-1][1]["reply_markup"]["inline_keyboard"][0]
            self.assertIn("confirm:satire", [button["callback_data"] for button in confirm])
            pult.handle_callback({"id": "cb", "from": {"id": 123}, "message": {"chat": {"id": 123}},
                                  "data": "confirm:satire"})
            self.assertTrue(pult.process_one_satire_submission())
            with pult.store.connect() as db:
                submission = dict(db.execute("SELECT * FROM satire_submissions LIMIT 1").fetchone())
            post = pult.satire.get(submission["result_post_id"])
            self.assertEqual(post["status"], "draft")
            self.assertEqual(post["origin"], "user")
            self.assertIsNone(post["scheduled_at"])
            self.assertFalse(any(payload.get("chat_id") == "@auqni_qms" for _, payload in api.calls))
            self.assertEqual(pult.satire.version(post["id"], 1)["source"], "user")
            self.assertEqual(api.calls[-1][0], "sendPhoto")
            buttons = json.loads(api.calls[-1][1]["reply_markup"])["inline_keyboard"][0]
            self.assertEqual([b["text"] for b in buttons], ["Принято", "Изменить", "Отклонить"])
            approve_callback = buttons[0]["callback_data"]
            with self.assertRaises(PultError):
                pult.handle_callback({"id": "old", "from": {"id": 123}, "message": {"chat": {"id": 123}},
                                      "data": f"satire:approve:{post['id']}:1"})
            pult.handle_callback({"id": "cb2", "from": {"id": 123}, "message": {"chat": {"id": 123}},
                                  "data": approve_callback})
            approved = pult.satire.get(post["id"])
            self.assertEqual(approved["status"], "approved")
            self.assertIsNotNone(approved["scheduled_at"])
            with pult.store.connect() as db:
                self.assertEqual(db.execute("SELECT status FROM satire_submissions WHERE id=?",
                                            (submission["id"],)).fetchone()[0], "APPROVED")
            from pult_core import moscow_zone
            local_slot = datetime.fromisoformat(approved["scheduled_at"]).astimezone(moscow_zone())
            self.assertLess(local_slot.weekday(), 5)
            self.assertEqual(local_slot.strftime("%H:%M"), "08:30")
            self.assertFalse(any(payload.get("chat_id") == "@auqni_qms" for _, payload in api.calls))
            pult.handle_callback({"id": "cb3", "from": {"id": 123}, "message": {"chat": {"id": 123}},
                                  "data": f"satire:edit:{post['id']}:1"})
            self.assertEqual(pult.store.get_pending(),
                             {"kind": "edit_input", "post_id": post["id"], "version": 1})
            self.assertEqual(pult.satire.get(post["id"])["status"], "editing")

    def test_substantive_edit_creates_new_text_and_image_version(self):
        class API:
            def call(self, method, payload, files=None):
                return {"message_id": 1}

        class Writer:
            def run(self, *args, **kwargs):
                return {"text": "На аудите обнаружили процесс, о существовании которого сотрудники узнали из отчёта.",
                        "genre": "СМК-News", "topic": "Аудит", "mix_type": "pure", "product_context": None}

        class Generator:
            def __init__(self, root):
                self.root = root
                self.calls = 0

            def run(self, candidate, role):
                self.calls += 1
                return sample_visual(self.root, f"version-{self.calls}.png")

        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"AUQNI_OWNER_USER_ID": "123"}):
            root = Path(temp)
            (root / "content").mkdir()
            (root / "content/smk_satire_bank.json").write_bytes((ROOT / "content/smk_satire_bank.json").read_bytes())
            generator = Generator(root)
            pult = Pult(root, json.loads((ROOT / "pult_config.json").read_text()), API(),
                        satire_writer=Writer(), satire_image_generator=generator)
            post_id = pult.satire.plan(datetime(2026, 10, 9, 4, 0, tzinfo=timezone.utc))[0][0]
            self.assertTrue(pult.process_one_satire_visual())
            old_image = pult.satire.version(post_id, 1)["image_path"]
            pult.satire.begin_edit(post_id, 1)
            pult.satire.queue_edit(post_id, 1, "Сделай новую сцену про аудит")
            self.assertTrue(pult.process_one_satire_submission())
            new = pult.satire.version(post_id, 2)
            self.assertEqual(generator.calls, 2)
            self.assertNotEqual(new["image_path"], old_image)
            self.assertEqual(new["visual_source_version"], 2)
            self.assertEqual(pult.satire.version(post_id, 1)["image_path"], old_image)
            self.assertEqual(pult.satire.get(post_id)["status"], "scheduled")

    def test_generated_candidate_must_be_short_and_typed(self):
        with self.assertRaises(PultError):
            validate_candidate({"text": "Слишком коротко", "genre": "новость", "topic": "СМК", "mix_type": "pure"})

    def test_failed_edit_remains_unapproved_and_can_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            stream = SatireStream(Store(Path(temp) / "pult.sqlite3"),
                                  ROOT / "content/smk_satire_bank.json",
                                  {"time": "08:30", "horizon_days": 1})
            stream.seed()
            stream.root = Path(temp)
            post_id = stream.plan(datetime(2026, 10, 9, 4, 0, tzinfo=timezone.utc))[0][0]
            stream.begin_edit(post_id, 1)
            submission_id = stream.queue_edit(post_id, 1, "Сделай короче")
            submission = stream.claim_submission()
            stream.fail_submission(submission, PultError("writer failed"))
            self.assertEqual(stream.get(post_id)["status"], "scheduled")
            self.assertFalse(stream.due(datetime(2026, 12, 1, tzinfo=timezone.utc)))
            stream.retry_submission(submission_id)
            self.assertEqual(stream.get(post_id)["status"], "editing")
            self.assertEqual(stream.claim_submission()["id"], submission_id)

    def test_bank_and_weekday_slots_keep_mix(self):
        bank = read_bank(ROOT / "content/smk_satire_bank.json")
        self.assertEqual(len(bank), 60)
        self.assertEqual(sum(x["selected"] for x in bank), 40)
        with tempfile.TemporaryDirectory() as temp:
            stream = SatireStream(Store(Path(temp) / "pult.sqlite3"),
                                  ROOT / "content/smk_satire_bank.json",
                                  {"time": "08:30", "horizon_days": 13})
            stream.seed()
            stream.root = Path(temp)
            now = datetime(2026, 10, 9, 4, 0, tzinfo=timezone.utc)
            planned = stream.plan(now)
            self.assertEqual(len(planned), 10)
            self.assertEqual(stream.plan(now), [])
            from pult_core import moscow_zone
            self.assertTrue(all(datetime.fromisoformat(slot).astimezone(moscow_zone()).weekday() < 5
                                and datetime.fromisoformat(slot).astimezone(moscow_zone()).strftime("%H:%M") == "08:30"
                                for _, _, slot in planned))
            self.assertEqual([stream.get(post_id)["mix_type"] for post_id, _, _ in planned],
                             ["pure", "problem", "pure", "soft", "pure", "problem", "pure", "soft", "pure", "problem"])
            self.assertFalse(stream.due(now))
            prepare_bank_visual(stream, planned[0][0], temp)
            stream.approve(planned[0][0], now)
            self.assertEqual(len(stream.due(datetime(2026, 10, 9, 5, 31, tzinfo=timezone.utc))), 1)
            self.assertTrue(stream.claim(planned[0][0], datetime(2026, 10, 9, 5, 31, tzinfo=timezone.utc)))
            stream.result(planned[0][0])
            self.assertEqual(stream.get(planned[0][0])["status"], "uncertain")
            self.assertFalse(stream.claim(planned[0][0], datetime(2026, 10, 9, 5, 31, tzinfo=timezone.utc)))

    def test_existing_schedule_is_unchanged_and_satire_on(self):
        config = json.loads((ROOT / "pult_config.json").read_text())
        self.assertEqual(config["channels"]["telegram"]["schedule"],
                         {"0": "19:00", "1": "18:00", "2": "13:00", "3": "18:00"})
        self.assertTrue(config["smk_satire"]["enabled"])
        self.assertEqual(config["smk_satire"]["time"], "08:30")

    def test_launch_queue_then_mix_catches_up(self):
        config = json.loads((ROOT / "pult_config.json").read_text())
        with tempfile.TemporaryDirectory() as temp:
            stream = SatireStream(Store(Path(temp) / "pult.sqlite3"),
                                  ROOT / "content/smk_satire_bank.json",
                                  {**config["smk_satire"], "horizon_days": 29})
            stream.seed()
            stream.root = Path(temp)
            planned = stream.plan(datetime(2026, 10, 9, 4, 0, tzinfo=timezone.utc))
            self.assertGreaterEqual(len(planned), 20)
            planned = planned[:20]
            self.assertEqual([p[0] for p in planned[:10]], config["smk_satire"]["launch_queue"])
            from collections import Counter
            self.assertEqual(Counter(stream.get(p[0])["mix_type"] for p in planned),
                             {"pure": 10, "problem": 6, "soft": 4})
            self.assertEqual(len({p[0] for p in planned}), 20)

    def test_reaction_count_snapshot_is_observed_and_scoped(self):
        with tempfile.TemporaryDirectory() as temp:
            stream = SatireStream(Store(Path(temp) / "pult.sqlite3"),
                                  ROOT / "content/smk_satire_bank.json",
                                  {"time": "08:30", "horizon_days": 1})
            stream.seed()
            stream.root = Path(temp)
            before = datetime(2026, 10, 9, 4, 0, tzinfo=timezone.utc)
            post_id = stream.plan(before)[0][0]
            prepare_bank_visual(stream, post_id, temp)
            stream.approve(post_id, before)
            self.assertTrue(stream.claim(post_id, datetime(2026, 10, 9, 5, 31, tzinfo=timezone.utc)))
            stream.result(post_id, 42)
            update = {"chat": {"username": "auqni_qms"}, "message_id": 42,
                      "date": 1791522000,
                      "reactions": [{"type": {"type": "emoji", "emoji": "👍"}, "total_count": 3}]}
            self.assertTrue(stream.record_reaction_count(update))
            saved = json.loads(stream.get(post_id)["reactions_json"])
            self.assertEqual(saved["total"], 3)
            self.assertEqual(saved["source"], "telegram_bot_api_message_reaction_count")
            self.assertFalse(stream.record_reaction_count({**update, "chat": {"username": "other"}}))
            self.assertFalse(stream.record_reaction_count({**update, "message_id": 43}))
            self.assertEqual(json.loads(stream.get(post_id)["reactions_json"]), saved)

    def test_pult_sends_text_and_image_only_after_owner_approval(self):
        class API:
            def __init__(self):
                self.calls = []

            def call(self, method, payload, files=None):
                self.calls.append((method, payload))
                if payload.get("chat_id") == "@auqni_qms":
                    return {"message_id": 42, "chat": {"username": "auqni_qms"}}
                return {"message_id": 1}

        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"AUQNI_OWNER_USER_ID": "123"}):
            root = Path(temp)
            (root / "content").mkdir()
            (root / "content/smk_satire_bank.json").write_bytes((ROOT / "content/smk_satire_bank.json").read_bytes())
            config = json.loads((ROOT / "pult_config.json").read_text())
            config["smk_satire"]["enabled"] = True
            config["smk_satire"]["horizon_days"] = 1
            api = API()
            pult = Pult(root, config, api)
            before = datetime(2026, 10, 9, 4, 0, tzinfo=timezone.utc)
            due = datetime(2026, 10, 9, 5, 31, tzinfo=timezone.utc)
            pult.tick(before)
            post_id = "SMK-007"
            pult.satire_image_generator = type("Generator", (), {"run": lambda self, row, role: sample_visual(root, "bank.png")})()
            self.assertTrue(pult.process_one_satire_visual())
            self.assertFalse(any(payload.get("chat_id") == "@auqni_qms" for _, payload in api.calls))
            pult.satire.approve(post_id, before)
            pult.tick(due)
            sent = [(method, payload) for method, payload in api.calls if payload.get("chat_id") == "@auqni_qms"]
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0][0], "sendPhoto")
            self.assertEqual(pult.satire.get(post_id)["status"], "published")
            pult.tick(due)
            self.assertEqual(sum(payload.get("chat_id") == "@auqni_qms" for _, payload in api.calls), 1)


if __name__ == "__main__":
    unittest.main()
