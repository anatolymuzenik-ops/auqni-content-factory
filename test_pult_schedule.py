import json
import os
from datetime import datetime, timedelta
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from pult_core import PultError, iso_utc, moscow_zone, utc_now
from pult_schedule import schedule_messages
from pult_store import Store
from telegram_pult import Pult
from test_telegram_pult import FakeAPI, FakePipeline, FakePublisher, SCHEDULE
from test_pult_satire import sample_visual


NOW = datetime(2026, 10, 7, 10, 0, tzinfo=moscow_zone())


def stamp(day, hour, minute=0):
    return iso_utc(datetime(2026, 10, day, hour, minute, tzinfo=moscow_zone()))


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "pult_data/pult.sqlite3")
        self.api = FakeAPI()
        owner = patch.dict(os.environ, {"AUQNI_OWNER_USER_ID": "12345"})
        owner.start()
        self.addCleanup(owner.stop)
        self.config = {"channels": {"telegram": {"enabled": True, "schedule": SCHEDULE}},
                       "data_dir": "pult_data", "smk_satire": {"enabled": False, "time": "08:30",
                       "weekdays": [0, 1, 2, 3, 4]}}
        self.pult = Pult(self.root, self.config, self.api, self.store,
                         FakePipeline(self.root), FakePublisher())

    def add_post(self, title, when, status="ready"):
        item_id = self.store.create("post", title, "{}", channels=("telegram",))
        self.store.set_schedule(item_id, "telegram", when)
        with self.store.connect() as db:
            db.execute("UPDATE item_channels SET status=? WHERE item_id=?", (status, item_id))
        return item_id

    def texts(self):
        return [fields["text"] for method, fields, _ in self.api.calls if method == "sendMessage"]

    def callback(self, data):
        self.pult.handle_callback({"id": "schedule-test", "from": {"id": 12345},
                                   "message": {"chat": {"id": 12345}}, "data": data})

    def message(self, text):
        self.pult.handle_message({"from": {"id": 12345},
                                  "chat": {"id": 12345, "type": "private"}, "text": text})

    def action(self, prefix):
        for method, fields, _ in reversed(self.api.calls):
            if method != "sendMessage":
                continue
            for row in fields.get("reply_markup", {}).get("inline_keyboard", []):
                for button in row:
                    if button.get("callback_data", "").startswith(prefix):
                        return button["callback_data"]
        self.fail(f"Missing schedule action {prefix}")

    @staticmethod
    def future_slot(kind="main"):
        now = utc_now().astimezone(moscow_zone())
        for offset in range(1, 10):
            day = (now + timedelta(days=offset)).date()
            clock = (SCHEDULE.get(str(day.weekday())) if kind == "main" else
                     "08:30" if day.weekday() < 5 else None)
            if clock:
                hour, minute = map(int, clock.split(":"))
                return iso_utc(datetime(day.year, day.month, day.day, hour, minute,
                                        tzinfo=moscow_zone()))
        raise AssertionError("No future configured slot")

    def enable_satire(self, writer=None, generator=None):
        (self.root / "content").mkdir(exist_ok=True)
        shutil.copyfile(Path(__file__).resolve().parent / "content/smk_satire_bank.json",
                        self.root / "content/smk_satire_bank.json")
        self.config["smk_satire"]["enabled"] = True
        self.pult = Pult(self.root, self.config, self.api, self.store,
                         FakePipeline(self.root), FakePublisher(),
                         satire_writer=writer, satire_image_generator=generator)

    def test_menu_and_callback_ranges_keep_existing_buttons(self):
        keyboard = self.pult.permanent_keyboard()["keyboard"]
        self.assertEqual([item["text"] for row in keyboard for item in row],
                         ["Создать пост", "Контент-план", "Расписание"])
        self.pult.handle_message({"from": {"id": 12345}, "chat": {"id": 12345, "type": "private"},
                                  "text": "Расписание"})
        self.assertIn("7 дней", self.texts()[-1])
        buttons = self.api.calls[-1][1]["reply_markup"]["inline_keyboard"]
        labels = [button["text"] for row in buttons for button in row]
        self.assertTrue(any(label.startswith("Добавить пост") for label in labels))
        self.assertEqual(labels[-3:], ["7 дней", "14 дней", "Только свободные"])
        self.pult.handle_callback({"id": "cb", "from": {"id": 12345},
                                   "message": {"chat": {"id": 12345}}, "data": "schedule:14:all"})
        self.assertIn("14 дней", self.texts()[-1])
        self.pult.handle_callback({"id": "cb", "from": {"id": 12345},
                                   "message": {"chat": {"id": 12345}}, "data": "schedule:14:free"})
        self.assertIn("СВОБОДНЫЕ СЛОТЫ", self.texts()[-1])

    def test_open_main_post_and_return_to_schedule(self):
        slot = self.future_slot()
        item_id = self.store.create("post", "Проверка аудита", "{}", channels=["telegram"])
        self.store.set_schedule(item_id, "telegram", slot)
        self.store.enqueue(item_id, "Подготовь материал", {"text": "Проверка аудита", "attachments": []})
        self.assertTrue(self.pult.process_one_job())
        self.api.calls.clear()
        self.pult.show_schedule()
        self.callback(self.action(f"schedule:open:main:{item_id}:7:all"))
        self.assertTrue(any(method == "sendPhoto" for method, _, _ in self.api.calls))
        card_buttons = self.api.calls[-1][1]["reply_markup"]["inline_keyboard"]
        self.assertIn("Принято", [button["text"] for row in card_buttons for button in row])
        self.assertEqual(card_buttons[-1][0]["text"], "Назад в расписание")
        self.callback(card_buttons[-1][0]["callback_data"])
        self.assertIn("РАСПИСАНИЕ AUQNI", self.texts()[-1])

    def test_open_smk_post_and_return_to_schedule(self):
        self.enable_satire()
        slot = self.future_slot("smk")
        with self.store.connect() as db:
            db.execute("UPDATE satire_posts SET status='scheduled',scheduled_at=? WHERE id='SMK-007'", (slot,))
        self.pult.show_schedule()
        self.callback(self.action("schedule:open:smk:SMK-007:7:all"))
        self.assertIn("Изображение ещё не готово", self.texts()[-1])
        self.assertNotIn("Принято", [button["text"] for row in
                         self.api.calls[-1][1]["reply_markup"]["inline_keyboard"] for button in row])
        job, _ = self.pult.satire.claim_visual()
        self.pult.satire.finish_visual("SMK-007", job["version"], sample_visual(self.root))
        self.api.calls.clear()
        self.pult.show_schedule()
        self.callback(self.action("schedule:open:smk:SMK-007:7:all"))
        review = next(fields for method, fields, _ in reversed(self.api.calls) if method == "sendPhoto")
        buttons = json.loads(review["reply_markup"])["inline_keyboard"]
        self.assertEqual([button["text"] for button in buttons[0]],
                         ["Принято", "Изменить", "Отклонить"])
        self.assertEqual(buttons[-1][0]["text"], "Назад в расписание")
        self.callback(buttons[-1][0]["callback_data"])
        self.assertIn("РАСПИСАНИЕ AUQNI", self.texts()[-1])

    def test_add_main_post_to_selected_free_slot_and_recheck_occupancy(self):
        slot = self.future_slot()
        epoch = int(datetime.fromisoformat(slot).timestamp())
        self.pult.show_schedule()
        add = self.action(f"schedule:add:main:{epoch}:7:all")
        self.callback(add)
        self.assertEqual(self.store.get_pending()["slot"], slot)
        self.message("Тема для выбранного слота")
        self.assertEqual(self.store.get_pending()["slot_kind"], "main")
        self.callback("confirm:create")
        item = self.store.get(1)
        self.assertEqual(item["channels"]["telegram"]["scheduled_at"], slot)
        self.assertTrue(self.pult.process_one_job())
        self.callback("approve:1:1")
        self.assertEqual(self.store.get(1)["channels"]["telegram"]["scheduled_at"], slot)

    def test_occupied_slot_does_not_create_orphan_post(self):
        slot = self.future_slot()
        epoch = int(datetime.fromisoformat(slot).timestamp())
        self.pult.show_schedule()
        self.callback(self.action(f"schedule:add:main:{epoch}:7:all"))
        self.message("Материал для слота")
        competing = self.store.create("post", "Конкурирующий", "{}", channels=["telegram"])
        self.store.set_schedule(competing, "telegram", slot)
        with self.assertRaises(PultError):
            self.callback("confirm:create")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM items").fetchone()[0], 1)
        self.assertEqual(self.store.get_pending()["kind"], "create_confirm")
        with self.assertRaises(PultError):
            self.callback(f"schedule:add:main:{epoch}:7:all")

    def test_add_smk_post_to_selected_free_slot(self):
        class Writer:
            def run(self, idea, previous=None, instruction=None, mix_type=None):
                return {"text": "На аудите нашли ещё один порядок согласования. Теперь его согласуют отдельно от процесса.",
                        "genre": "сатира", "topic": "аудит", "mix_type": "pure",
                        "product_context": None}

        class Generator:
            def run(self, candidate, role):
                return sample_visual(root, "scheduled-user.png")

        root = self.root
        self.enable_satire(Writer(), Generator())
        slot = self.future_slot("smk")
        epoch = int(datetime.fromisoformat(slot).timestamp())
        self.pult.show_schedule()
        self.callback(self.action(f"schedule:add:smk:{epoch}:7:all"))
        self.message("Шутка про аудит")
        self.assertEqual(self.store.get_pending()["slot_kind"], "smk")
        self.callback("confirm:satire")
        self.assertTrue(self.pult.process_one_satire_submission())
        with self.store.connect() as db:
            post_id = db.execute("SELECT result_post_id FROM satire_submissions ORDER BY id DESC LIMIT 1").fetchone()[0]
        post = self.pult.satire.get(post_id)
        self.assertEqual(post["scheduled_at"], slot)
        self.assertEqual(post["status"], "scheduled")
        self.pult.satire.approve(post_id, utc_now(), version=1)
        self.assertEqual(self.pult.satire.get(post_id)["scheduled_at"], slot)

    def test_smk_slot_taken_during_preparation_is_not_reassigned(self):
        class Writer:
            def run(self, idea, previous=None, instruction=None, mix_type=None):
                return {"text": "На аудите нашли порядок для новой проверки. Теперь проверяют, кто проверит проверку.",
                        "genre": "сатира", "topic": "аудит", "mix_type": "pure",
                        "product_context": None}

        class Generator:
            def run(self, candidate, role):
                return sample_visual(root, "occupied-user.png")

        root = self.root
        self.enable_satire(Writer(), Generator())
        slot = self.future_slot("smk")
        epoch = int(datetime.fromisoformat(slot).timestamp())
        self.pult.show_schedule()
        self.callback(self.action(f"schedule:add:smk:{epoch}:7:all"))
        self.message("Шутка для слота")
        self.callback("confirm:satire")
        with self.store.connect() as db:
            db.execute("UPDATE satire_posts SET status='scheduled',scheduled_at=? WHERE id='SMK-007'", (slot,))
        self.assertTrue(self.pult.process_one_satire_submission())
        with self.store.connect() as db:
            submission = db.execute("SELECT result_post_id,last_error FROM satire_submissions LIMIT 1").fetchone()
            self.assertIsNone(submission["result_post_id"])
            self.assertIn("слот", submission["last_error"])
            self.assertEqual(db.execute("SELECT count(*) FROM satire_posts WHERE origin='user'").fetchone()[0], 0)

    def test_real_slots_statuses_offgrid_and_disabled_satire(self):
        self.add_post("План аудита", stamp(7, 13), "approved")
        self.add_post("Итоги проверки", stamp(8, 18), "published")
        self.add_post("Вне графика", stamp(9, 18), "ready")
        full = "\n".join(schedule_messages(self.store, SCHEDULE, self.config["smk_satire"], now=NOW))
        free = "\n".join(schedule_messages(self.store, SCHEDULE, self.config["smk_satire"],
                                           now=NOW, free_only=True))
        self.assertIn("занято 2 из 4, свободно 2", full)
        self.assertIn("«План аудита»\nСогласовано", full)
        self.assertIn("«Итоги проверки»\nОпубликовано", full)
        self.assertIn("Вне графика", full)
        self.assertIn("вне регулярного слота", full)
        self.assertIn("Поток выключен", full)
        self.assertIn("Основной контент: занято 2 из 4, свободно 2", free)
        self.assertIn("Всего свободно: 2", free)
        self.assertNotIn("Пт 09.10", free)
        self.assertNotIn("08:30 | SMK_SATIRE", free)
        self.assertNotIn("Сб 10.10", free)
        self.assertNotIn("Вс 11.10", free)

    def test_fourteen_days_and_missing_topic(self):
        self.add_post("", stamp(7, 13), "idea")
        full = "\n".join(schedule_messages(self.store, SCHEDULE, self.config["smk_satire"],
                                           days=14, now=NOW))
        self.assertIn("занято 1 из 8, свободно 7", full)
        self.assertIn("«Без темы»\nИдея", full)
        self.assertIn("Ср 14.10", full)
        self.assertNotIn("Ср 21.10", full)

    def test_slot_variant_and_search_status(self):
        slot = stamp(7, 13)
        self.store.set_kv(f"autoplan_today_rejected:{slot}", json.dumps([8, 9]))
        self.store.set_kv(f"autoplan_today_state:{slot}", "searching")
        full = "\n".join(schedule_messages(self.store, SCHEDULE, self.config["smk_satire"], now=NOW))
        self.assertIn("13:00 | Основной контент\nИДЁТ ПОДБОР", full)
        self.store.set_kv(f"autoplan_today_state:{slot}", "not_found")
        full = "\n".join(schedule_messages(self.store, SCHEDULE, self.config["smk_satire"], now=NOW))
        self.assertIn("МАТЕРИАЛ НЕ НАЙДЕН", full)
        item_id = self.add_post("Проверка результата", slot, "ready")
        full = "\n".join(schedule_messages(self.store, SCHEDULE, self.config["smk_satire"], now=NOW))
        self.assertIn("Вариант 3\n«Проверка результата»\nОжидает согласования", full)
        self.assertNotIn("МАТЕРИАЛ НЕ НАЙДЕН", full)
        with self.store.connect() as db:
            db.execute("UPDATE items SET current_version=2 WHERE id=?", (item_id,))
            db.execute("UPDATE item_channels SET status='approved' WHERE item_id=?", (item_id,))
        full = "\n".join(schedule_messages(self.store, SCHEDULE, self.config["smk_satire"], now=NOW))
        self.assertIn("Вариант 3 · версия 2", full)
        self.assertIn("Согласовано", full)
        self.assertEqual(self.store.get(item_id)["id"], item_id)

    def test_enabled_satire_uses_existing_table_and_status(self):
        (self.root / "content").mkdir()
        shutil.copyfile(Path(__file__).resolve().parent / "content/smk_satire_bank.json",
                        self.root / "content/smk_satire_bank.json")
        self.config["smk_satire"]["enabled"] = True
        self.pult = Pult(self.root, self.config, self.api, self.store,
                         FakePipeline(self.root), FakePublisher())
        with self.store.connect() as db:
            db.execute("UPDATE satire_posts SET scheduled_at=?,status='scheduled' WHERE id='SMK-007'",
                       (stamp(8, 8, 30),))
        full = "\n".join(schedule_messages(self.store, SCHEDULE, self.config["smk_satire"], now=NOW))
        free = "\n".join(schedule_messages(self.store, SCHEDULE, self.config["smk_satire"],
                                           now=NOW, free_only=True))
        self.assertIn("SMK_SATIRE: занято 1 из 5, свободно 3, прошло 1", full)
        self.assertIn("SMK_SATIRE · Пост 1", full)
        self.assertNotIn("SMK-007", full)
        self.assertIn("Запланировано", full)
        self.assertIn("Всего свободно: 7", free)

    def test_cross_stream_occupancy_is_not_offered_as_free(self):
        self.enable_satire()
        slot = self.future_slot("smk")
        self.add_post("Основной пост вне сетки", slot)
        pages = schedule_messages(self.store, SCHEDULE, self.config["smk_satire"],
                                  now=utc_now(), with_actions=True)
        self.assertFalse(any(action[1].startswith("schedule:add:smk:") and
                             action[1].split(":")[3] == str(int(datetime.fromisoformat(slot).timestamp()))
                             for _, actions in pages for action in actions))
        self.assertFalse(self.store.slot_is_free(slot))
        another = iso_utc(datetime.fromisoformat(slot) + timedelta(days=7))
        with self.store.connect() as db:
            db.execute("UPDATE satire_posts SET status='scheduled',scheduled_at=? WHERE id='SMK-007'", (another,))
        other_post = self.store.create("post", "Не должен занять SMK", "{}", channels=["telegram"])
        with self.assertRaises(PultError):
            self.store.set_schedule(other_post, "telegram", another)

    def test_empty_schedule_and_long_response(self):
        empty = schedule_messages(self.store, {}, {"enabled": False, "weekdays": []},
                                  free_only=True, now=NOW)
        self.assertIn("Свободных слотов нет", empty[0])
        for index in range(90):
            self.add_post("Длинная тема " + "А" * 120 + str(index), stamp(9, index // 60, index % 60))
        pages = schedule_messages(self.store, SCHEDULE, self.config["smk_satire"], days=14, now=NOW)
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(len(page) <= 3500 for page in pages))


if __name__ == "__main__":
    unittest.main()
