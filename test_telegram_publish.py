import contextlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import telegram_publish as telegram


def png(width=100, height=100):
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + b"fixture"


class TelegramPublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.text = self.root / "post.md"
        self.text.write_text("  Text AUQNI  ", encoding="utf-8")
        self.content = self.root / "content.json"
        self.content.write_text(json.dumps({
            "schema_version": "auqni-content/v1",
            "platforms": {"telegram": {"content": "Text AUQNI"}},
        }), encoding="utf-8")
        self.image = self.root / "image.png"
        self.image.write_bytes(png())

    def run_cli(self, extra=(), token="test-only-credential"):
        out, err = io.StringIO(), io.StringIO()
        args = ["--text-file", str(self.text), "--image", str(self.image), *extra]
        with patch.object(telegram, "read_token", return_value=token), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = telegram.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_never_uses_network(self):
        with patch.object(telegram, "request_json", side_effect=AssertionError("network forbidden")) as net:
            code, out, err = self.run_cli()
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["network_requests"], 0)
        self.assertEqual(result["multipart_fields"], ["chat_id", "caption", "photo"])
        self.assertEqual(result["caption_characters"], 10)
        self.assertNotIn("test-only-credential", out + err)
        net.assert_not_called()

    def test_dry_run_works_without_token(self):
        code, out, _ = self.run_cli(token="")
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)["token_available"])

    def test_invalid_caption_and_png_fail_before_network(self):
        with patch.object(telegram, "request_json") as net:
            self.text.write_text("x" * 1025, encoding="utf-8")
            self.assertEqual(self.run_cli()[0], 1)
            self.text.write_text("valid", encoding="utf-8")
            self.image.write_bytes(b"not png")
            self.assertEqual(self.run_cli()[0], 1)
        net.assert_not_called()

    def test_png_limits(self):
        self.image.write_bytes(png(9999, 2))
        self.assertEqual(self.run_cli()[0], 1)
        self.image.write_bytes(png(2000, 1))
        self.assertEqual(self.run_cli()[0], 1)

    def test_multipart_contract(self):
        body, content_type = telegram.multipart_body("@auqni_qms", "Caption", self.image.read_bytes())
        self.assertIn(b'name="chat_id"', body)
        self.assertIn(b'name="caption"', body)
        self.assertIn(b'name="photo"; filename="image.png"', body)
        self.assertIn(b"image/png", body)
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))

    def test_publish_response_and_url(self):
        response = {"ok": True, "result": {"message_id": 17,
                    "chat": {"id": -1001, "username": "auqni_qms"}}}
        with patch.object(telegram, "request_json", return_value=response):
            message_id, chat_id = telegram.publish(
                "test-only-credential", "@auqni_qms", "Caption", self.image.read_bytes())
        self.assertEqual((message_id, chat_id), (17, -1001))
        self.assertEqual(telegram.publication_url("@auqni_qms", message_id),
                         "https://t.me/auqni_qms/17")

    def test_publish_requires_token_before_network(self):
        with patch.object(telegram, "request_json") as net:
            code, _, _ = self.run_cli(["--publish", "--content-json", str(self.content)], token="")
        self.assertEqual(code, 1)
        net.assert_not_called()

    def test_text_file_alone_cannot_publish(self):
        with patch.object(telegram, "request_json") as net:
            code, _, err = self.run_cli(["--publish"])
        self.assertEqual(code, 1)
        self.assertIn("--content-json", err)
        net.assert_not_called()

    def test_json_caption_is_canonical_without_text_file(self):
        with patch.object(telegram, "request_json", side_effect=AssertionError("network forbidden")) as net:
            code, out, err = self.run_cli(["--content-json", str(self.content), "--dry-run"])
        self.assertEqual(code, 1, "Legacy text copy must not silently override a mismatched JSON")
        self.assertIn("differs", err)
        net.assert_not_called()

        out, err = io.StringIO(), io.StringIO()
        args = ["--content-json", str(self.content), "--image", str(self.image), "--dry-run"]
        with patch.object(telegram, "request_json", side_effect=AssertionError("network forbidden")) as net, \
                patch.object(telegram, "read_token", return_value=""), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = telegram.main(args)
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["caption_characters"], len("Text AUQNI"))
        net.assert_not_called()

    def test_mismatched_text_copy_blocks_publish_before_network(self):
        with patch.object(telegram, "request_json") as net:
            code, _, err = self.run_cli(["--publish", "--content-json", str(self.content)])
        self.assertEqual(code, 1)
        self.assertIn("differs", err)
        net.assert_not_called()

    def test_matching_text_copy_can_reach_publish_path(self):
        self.text.write_text("Text AUQNI", encoding="utf-8")
        journal = self.root / "journal.jsonl"
        response = {"ok": True, "result": {"message_id": 17,
                    "chat": {"id": -1001, "username": "auqni_qms"}}}
        with patch.object(telegram, "request_json", return_value=response) as net:
            code, out, err = self.run_cli([
                "--publish", "--content-json", str(self.content), "--journal", str(journal)])
        self.assertEqual(code, 0, err)
        self.assertIn("Published: https://t.me/auqni_qms/17", out)
        net.assert_called_once()

    def test_api_error_is_sanitized(self):
        secret = "test-only-credential"
        with patch.object(telegram, "request_json", return_value={
                "ok": False, "error_code": 400, "description": secret}):
            with self.assertRaises(telegram.ApiRejected) as raised:
                telegram.publish(secret, "@auqni_qms", "Caption", self.image.read_bytes())
        self.assertIn("400", str(raised.exception))
        self.assertNotIn(secret, str(raised.exception))

    def test_journal_lock_is_exclusive(self):
        journal = self.root / "journal.jsonl"
        with telegram.JournalLock(journal):
            with self.assertRaises(telegram.PublishError):
                with telegram.JournalLock(journal):
                    pass
        self.assertFalse(Path(str(journal) + ".lock").exists())


if __name__ == "__main__":
    unittest.main()
