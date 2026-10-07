import json
import os
from datetime import datetime
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from pult_core import iso_utc, moscow_zone
from pult_schedule import schedule_messages
from pult_store import Store
from telegram_pult import Pult
from test_telegram_pult import FakeAPI, FakePipeline, FakePublisher, SCHEDULE


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

    def test_menu_and_callback_ranges_keep_existing_buttons(self):
        keyboard = self.pult.permanent_keyboard()["keyboard"]
        self.assertEqual([item["text"] for row in keyboard for item in row],
                         ["Создать пост", "Контент-план", "Расписание"])
        self.pult.handle_message({"from": {"id": 12345}, "chat": {"id": 12345, "type": "private"},
                                  "text": "Расписание"})
        self.assertIn("7 дней", self.texts()[-1])
        buttons = self.api.calls[-1][1]["reply_markup"]["inline_keyboard"]
        self.assertEqual([button["text"] for row in buttons for button in row],
                         ["7 дней", "14 дней", "Только свободные"])
        self.pult.handle_callback({"id": "cb", "from": {"id": 12345},
                                   "message": {"chat": {"id": 12345}}, "data": "schedule:14:all"})
        self.assertIn("14 дней", self.texts()[-1])
        self.pult.handle_callback({"id": "cb", "from": {"id": 12345},
                                   "message": {"chat": {"id": 12345}}, "data": "schedule:14:free"})
        self.assertIn("СВОБОДНЫЕ СЛОТЫ", self.texts()[-1])

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
        self.assertIn("SMK_SATIRE · SMK-007", full)
        self.assertIn("Запланировано", full)
        self.assertIn("Всего свободно: 7", free)

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
