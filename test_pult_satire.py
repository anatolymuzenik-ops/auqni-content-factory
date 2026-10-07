import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pult_core import PultError
from pult_satire import SatireStream, read_bank
from pult_store import Store
from telegram_pult import Pult


ROOT = Path(__file__).resolve().parent


class SatireTests(unittest.TestCase):
    def test_bank_and_weekday_slots_keep_mix(self):
        bank = read_bank(ROOT / "content/smk_satire_bank.json")
        self.assertEqual(len(bank), 60)
        self.assertEqual(sum(x["selected"] for x in bank), 40)
        with tempfile.TemporaryDirectory() as temp:
            stream = SatireStream(Store(Path(temp) / "pult.sqlite3"),
                                  ROOT / "content/smk_satire_bank.json",
                                  {"time": "08:30", "horizon_days": 13})
            stream.seed()
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
            stream.approve(planned[0][0], now)
            self.assertEqual(len(stream.due(datetime(2026, 10, 9, 5, 31, tzinfo=timezone.utc))), 1)
            self.assertTrue(stream.claim(planned[0][0], datetime(2026, 10, 9, 5, 31, tzinfo=timezone.utc)))
            stream.result(planned[0][0])
            self.assertEqual(stream.get(planned[0][0])["status"], "uncertain")
            self.assertFalse(stream.claim(planned[0][0], datetime(2026, 10, 9, 5, 31, tzinfo=timezone.utc)))

    def test_existing_schedule_is_unchanged_and_satire_off(self):
        config = json.loads((ROOT / "pult_config.json").read_text())
        self.assertEqual(config["channels"]["telegram"]["schedule"],
                         {"0": "19:00", "1": "18:00", "2": "13:00", "3": "18:00"})
        self.assertFalse(config["smk_satire"]["enabled"])
        self.assertEqual(config["smk_satire"]["time"], "08:30")

    def test_pult_sends_text_only_after_owner_approval(self):
        class API:
            def __init__(self):
                self.calls = []

            def call(self, method, payload):
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
            post_id = "SMK-001"
            self.assertFalse(any(payload.get("chat_id") == "@auqni_qms" for _, payload in api.calls))
            pult.satire.approve(post_id, before)
            pult.tick(due)
            sent = [(method, payload) for method, payload in api.calls if payload.get("chat_id") == "@auqni_qms"]
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0][0], "sendMessage")
            self.assertEqual(pult.satire.get(post_id)["status"], "published")
            pult.tick(due)
            self.assertEqual(sum(payload.get("chat_id") == "@auqni_qms" for _, payload in api.calls), 1)


if __name__ == "__main__":
    unittest.main()
