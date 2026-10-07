import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

from pult_planner import upcoming_slots, validate_candidates
from pult_core import iso_utc, moscow_zone
from test_telegram_pult import FakeAPI, FakePipeline, FakePublisher, SCHEDULE
from telegram_pult import Pult
from pult_store import Store


NOW = datetime(2026, 9, 30, 11, tzinfo=timezone.utc)
TODAY = datetime(2026, 10, 7, 10, tzinfo=moscow_zone())


class FakePlanner:
    def __init__(self):
        self.calls = 0

    def propose(self, slots, history):
        self.calls += 1
        candidates = [{"topic": f"Практическая тема {n} для службы качества", "problem": "Разрозненные записи мешают увидеть решение",
                       "angle": "Показать путь от события до проверки результата", "stream": "smk_practice", "evidence_ids": []}
                      for n in range(len(slots))]
        return candidates, [], []


class PlannerTests(unittest.TestCase):
    def test_slots_respect_lead_and_existing_plan(self):
        slots = upcoming_slots(SCHEDULE, [], NOW, 48, 10)
        self.assertEqual(len(slots), 4)
        self.assertTrue(slots[0].startswith("2026-10-05"))
        self.assertEqual(len(upcoming_slots(SCHEDULE, [slots[0]], NOW, 48, 10)), 3)

    def test_evidence_and_duplicate_guard(self):
        evidence = [{"id": "pubmed:123", "text": "read abstract"}]
        data = {"candidates": [
            {"topic": "Тема о качестве данных", "problem": "Проблема в работе процесса", "angle": "Практический подход к проверке", "stream": "external_environment", "evidence_ids": ["fake"], "quality_score": 8, "quality_reason": "Есть ясная проблема и источник"},
            {"topic": "Тема о качестве данных", "problem": "Проблема в работе процесса", "angle": "Практический подход к проверке", "stream": "external_environment", "evidence_ids": ["pubmed:123"], "quality_score": 8, "quality_reason": "Есть ясная проблема и источник"},
            {"topic": "Тема о качестве данных", "problem": "Проблема в работе процесса", "angle": "Практический подход к проверке", "stream": "external_environment", "evidence_ids": ["pubmed:123"], "quality_score": 8, "quality_reason": "Есть ясная проблема и источник"}]}
        self.assertEqual(len(validate_candidates(data, evidence, [], 4)), 1)
        data["candidates"][1]["quality_score"] = 7
        data["candidates"][2]["quality_score"] = 7
        self.assertEqual(validate_candidates(data, evidence, [], 4), [])

    def test_plan_enqueues_once_and_never_approves(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"AUQNI_OWNER_USER_ID": "123"}):
            root = Path(temp)
            store = Store(root / "pult_data" / "pult.sqlite3")
            planner = FakePlanner()
            config = {"channels": {"telegram": {"enabled": True, "schedule": SCHEDULE}},
                      "autonomous_planning": {"enabled": True, "run_after_hour": 7,
                                              "min_lead_hours": 48, "horizon_days": 10,
                                              "max_new_per_run": 4}}
            pult = Pult(root, config, FakeAPI(), store, FakePipeline(root), FakePublisher(), planner)
            self.assertEqual(pult.plan_autonomously(NOW), 4)
            self.assertEqual(pult.plan_autonomously(NOW), 0)
            self.assertEqual(planner.calls, 1)
            for item in store.list_plan():
                self.assertEqual(item["current_version"], 0)
                self.assertIsNone(item["channels"]["telegram"]["approved_version"])
            self.assertEqual(len(store.occupied_slots("telegram")), 4)
            self.assertTrue(pult.process_one_job())
            self.assertEqual(store.get(1)["current_version"], 1)
            self.assertIsNone(store.get(1)["channels"]["telegram"]["approved_version"])
            restarted = Pult(root, config, FakeAPI(), Store(store.path), FakePipeline(root), FakePublisher(), planner)
            self.assertEqual(restarted.plan_autonomously(NOW), 0)


class TodaySlotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        owner = patch.dict(os.environ, {"AUQNI_OWNER_USER_ID": "123"})
        owner.start(); self.addCleanup(owner.stop)
        self.store = Store(self.root / "pult_data/pult.sqlite3")
        self.api = FakeAPI()
        self.pipeline = FakePipeline(self.root)
        self.publisher = FakePublisher()
        self.planner = FakePlanner()
        self.config = {"channels": {"telegram": {"enabled": True, "schedule": SCHEDULE}},
                       "data_dir": "pult_data", "autonomous_planning": {"enabled": True,
                       "run_after_hour": 7, "min_lead_hours": 48, "horizon_days": 10,
                       "max_new_per_run": 4}, "smk_satire": {"enabled": False}}
        self.pult = Pult(self.root, self.config, self.api, self.store,
                         self.pipeline, self.publisher, self.planner)

    def slot(self):
        return iso_utc(datetime(2026, 10, 7, 13, tzinfo=moscow_zone()))

    def callback(self, action, item_id):
        with patch("telegram_pult.utc_now", return_value=TODAY):
            self.pult.handle_callback({"id": "callback", "from": {"id": 123},
                                       "message": {"chat": {"id": 123}},
                                       "data": f"{action}:{item_id}:1"})

    def three_candidates(self):
        self.planner.propose = lambda slots, history: ([
            {"topic": f"Сильная тема {n} для службы качества", "problem": "Есть проблема",
             "angle": "Есть проверяемый подход", "stream": "smk_practice", "evidence_ids": []}
            for n in (1, 2, 3)], [], [])

    def test_reject_retries_next_candidate_then_reports_exhaustion(self):
        self.three_candidates()
        self.store.set_kv("autoplan_last_attempt", "2026-10-07")
        self.assertEqual(self.pult.plan_today(TODAY), 1)
        self.assertIn("Вариант 1", [fields["caption"] for method, fields, _ in self.api.calls
                                     if method == "sendPhoto"][-1])
        for rejected_id, next_id in ((1, 2), (2, 3)):
            self.callback("reject", rejected_id)
            self.assertIn(f"Вариант {rejected_id} отклонён", self.api.calls[-1][1]["text"])
            self.assertEqual(self.store.get(rejected_id)["status"], "rejected")
            self.assertIsNone(self.store.get(rejected_id)["channels"]["telegram"]["scheduled_at"])
            self.assertEqual(self.pult.plan_autonomously(TODAY), 1)
            self.assertEqual(self.store.get(next_id)["channels"]["telegram"]["scheduled_at"], self.slot())
            self.assertIn(f"Вариант {next_id}", [fields["caption"] for method, fields, _ in self.api.calls
                                              if method == "sendPhoto"][-1])
        self.callback("reject", 3)
        self.assertEqual(self.pult.plan_autonomously(TODAY), 0)
        self.assertEqual(json.loads(self.store.get_kv(f"autoplan_today_rejected:{self.slot()}")), [1, 2, 3])
        self.assertEqual(len(self.store.topic_history()), 3)
        notices = [fields["text"] for method, fields, _ in self.api.calls if method == "sendMessage"]
        self.assertEqual(len([notice for notice in notices if "остался свободным" in notice]), 1)
        self.assertEqual(self.pult.plan_autonomously(TODAY), 0)
        self.assertFalse(self.publisher.sent)

    def test_approval_stops_retry(self):
        self.three_candidates()
        self.assertEqual(self.pult.plan_today(TODAY), 1)
        self.callback("reject", 1)
        self.assertEqual(self.pult.plan_today(TODAY), 1)
        with patch("pult_store.utc_now", return_value=TODAY):
            self.callback("approve", 2)
        self.assertIn("Вариант 2 принят. Слот", self.api.calls[-1][1]["text"])
        self.assertEqual(self.pult.plan_today(TODAY), 0)
        self.assertEqual(len(self.store.topic_history()), 2)
        self.assertEqual(self.store.get(2)["channels"]["telegram"]["status"], "approved")
        self.pult.tick(TODAY)
        self.assertFalse(self.publisher.sent)

    def test_revision_changes_version_without_changing_variant(self):
        self.three_candidates()
        for item_id in (1, 2, 3):
            self.assertEqual(self.pult.plan_today(TODAY), 1)
            if item_id < 3:
                self.callback("reject", item_id)
        self.store.start_edit(3, 1)
        self.store.enqueue(3, "Уточни текст", {"text": "Уточни текст"})
        self.assertTrue(self.pult.process_one_job())
        caption = [fields["caption"] for method, fields, _ in self.api.calls if method == "sendPhoto"][-1]
        self.assertIn("Вариант 3 · версия 2", caption)
        self.assertNotIn("v1", caption)
        self.assertEqual(self.store.get(3)["current_version"], 2)

    def test_reject_limit_bounds_retry(self):
        self.three_candidates()
        self.store.set_kv(f"autoplan_today_rejected:{self.slot()}", json.dumps(list(range(10, 16))))
        self.assertEqual(self.pult.plan_today(TODAY), 0)
        self.assertEqual(self.planner.calls, 0)
        self.assertEqual(self.pult.plan_today(TODAY), 0)
        notices = [fields["text"] for method, fields, _ in self.api.calls if method == "sendMessage"]
        self.assertEqual(len([notice for notice in notices if "остался свободным" in notice]), 1)

    def test_off_grid_reject_does_not_restart_today(self):
        item_id = self.store.create("post", "Другой слот", "{}", ("telegram",))
        self.store.enqueue(item_id, "Подготовь", {"text": "Тема"})
        self.assertTrue(self.pult.process_one_job())
        self.store.set_schedule(item_id, "telegram", iso_utc(datetime(2026, 10, 8, 18, tzinfo=moscow_zone())))
        self.callback("reject", item_id)
        self.assertIsNone(self.store.get_kv(f"autoplan_today_rejected:{self.slot()}"))
        self.assertEqual(self.store.get(item_id)["status"], "rejected")

    def test_reject_uses_next_ready_material_before_planner(self):
        for number in (1, 2):
            item_id = self.store.create("post", f"Готовый материал {number}", "{}", ("telegram",))
            self.store.enqueue(item_id, "Подготовь", {"text": f"Тема {number}"})
            self.assertTrue(self.pult.process_one_job())
        self.assertEqual(self.pult.plan_today(TODAY), 1)
        self.callback("reject", 1)
        self.assertEqual(self.pult.plan_today(TODAY), 1)
        self.assertEqual(self.store.get(2)["channels"]["telegram"]["scheduled_at"], self.slot())
        self.assertEqual(self.planner.calls, 0)

    def test_ready_unplanned_material_is_offered_first(self):
        item_id = self.store.create("post", "Готовый материал", "{}", ("telegram",))
        self.store.enqueue(item_id, "Подготовь", {"text": "Тема"})
        self.assertTrue(self.pult.process_one_job())
        self.api.calls.clear()
        self.assertEqual(self.pult.plan_today(TODAY), 1)
        row = self.store.get(item_id)
        self.assertEqual(row["channels"]["telegram"]["scheduled_at"], self.slot())
        self.assertIsNone(row["channels"]["telegram"]["approved_version"])
        self.assertEqual(self.planner.calls, 0)
        self.assertTrue(any(method == "sendPhoto" for method, _, _ in self.api.calls))
        self.assertFalse(self.publisher.sent)

    def test_strong_theme_uses_existing_pipeline_and_waits_for_approval(self):
        self.assertEqual(self.pult.plan_today(TODAY), 1)
        self.assertEqual(self.planner.calls, 1)
        item = self.store.get(1)
        self.assertEqual(item["current_version"], 1)
        self.assertEqual(item["channels"]["telegram"]["scheduled_at"], self.slot())
        self.assertIsNone(item["channels"]["telegram"]["approved_version"])
        self.assertTrue(any(method == "sendPhoto" for method, _, _ in self.api.calls))
        self.pult.tick(TODAY)
        self.assertFalse(self.publisher.sent)

    def test_no_quality_candidate_reports_only_after_search(self):
        self.planner.propose = lambda slots, history: ([], [], [])
        self.assertEqual(self.pult.plan_today(TODAY), 0)
        notices = [fields["text"] for method, fields, _ in self.api.calls if method == "sendMessage"]
        self.assertEqual(len([notice for notice in notices if "остался свободным" in notice]), 1)
        self.assertIn("рассмотрено 0", notices[-1])
        self.assertEqual(self.pult.plan_today(TODAY), 0)
        self.assertEqual(len([fields for method, fields, _ in self.api.calls
                              if method == "sendMessage" and "остался свободным" in fields["text"]]), 1)

    def test_failed_first_theme_tries_second_before_reporting_empty(self):
        class FailsOnce(FakePipeline):
            def run(self, item, instruction, inputs, previous=None):
                if not self.calls:
                    self.calls.append((item["id"], instruction, inputs, previous))
                    raise RuntimeError("test preparation failure")
                return super().run(item, instruction, inputs, previous)

        self.pipeline = FailsOnce(self.root)
        self.pult.pipeline = self.pipeline
        self.planner.propose = lambda slots, history: ([
            {"topic": f"Сильная тема {n} для службы качества", "problem": "Есть проблема",
             "angle": "Есть проверяемый подход", "stream": "smk_practice", "evidence_ids": []}
            for n in (1, 2)], [], [])
        self.assertEqual(self.pult.plan_today(TODAY), 1)
        self.assertEqual(len(self.pipeline.calls), 2)
        self.assertIsNone(self.store.get(1)["channels"]["telegram"]["scheduled_at"])
        self.assertEqual(self.store.get(2)["current_version"], 1)
        self.assertEqual(self.store.get(2)["channels"]["telegram"]["scheduled_at"], self.slot())
        self.assertFalse(any("остался свободным" in fields["text"] for method, fields, _ in self.api.calls
                             if method == "sendMessage"))

    def test_occupied_slot_skips_extra_generation(self):
        item_id = self.store.create("post", "Уже в плане", "{}", ("telegram",))
        self.store.set_schedule(item_id, "telegram", self.slot())
        self.assertEqual(self.pult.plan_today(TODAY), 0)
        self.assertEqual(self.planner.calls, 0)
        self.assertEqual(self.pipeline.calls, [])

    def test_prepared_archive_is_offered_without_new_generation(self):
        (self.root / "posts").mkdir()
        (self.root / "images").mkdir()
        source, image = self.pipeline.run({"id": 9, "current_version": 0}, "Тема", {"text": "Тема"})
        (self.root / "posts/2026-10-07-ready-content.json").write_bytes(source.read_bytes())
        (self.root / "images/2026-10-07-ready.png").write_bytes(image.read_bytes())
        self.pipeline.calls.clear()
        self.assertEqual(self.pult.plan_today(TODAY), 1)
        self.assertEqual(self.planner.calls, 0)
        self.assertEqual(self.pipeline.calls, [])
        self.assertEqual(self.store.get(1)["current_version"], 1)
        self.assertIsNone(self.store.get(1)["channels"]["telegram"]["approved_version"])

    def test_experimental_hours_are_unchanged(self):
        import json
        config = json.loads((Path(__file__).resolve().parent / "pult_config.json").read_text())
        self.assertEqual(config["channels"]["telegram"]["schedule"], SCHEDULE)
        self.assertFalse(config["smk_satire"]["enabled"])


if __name__ == "__main__":
    unittest.main()
