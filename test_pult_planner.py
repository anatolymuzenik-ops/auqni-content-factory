import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

from pult_planner import upcoming_slots, validate_candidates
from test_telegram_pult import FakeAPI, FakePipeline, FakePublisher, SCHEDULE
from telegram_pult import Pult
from pult_store import Store


NOW = datetime(2026, 9, 30, 8, tzinfo=timezone.utc)


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
            {"topic": "Тема о качестве данных", "problem": "Проблема в работе процесса", "angle": "Практический подход к проверке", "stream": "external_environment", "evidence_ids": ["fake"]},
            {"topic": "Тема о качестве данных", "problem": "Проблема в работе процесса", "angle": "Практический подход к проверке", "stream": "external_environment", "evidence_ids": ["pubmed:123"]},
            {"topic": "Тема о качестве данных", "problem": "Проблема в работе процесса", "angle": "Практический подход к проверке", "stream": "external_environment", "evidence_ids": ["pubmed:123"]}]}
        self.assertEqual(len(validate_candidates(data, evidence, [], 4)), 1)

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


if __name__ == "__main__":
    unittest.main()
