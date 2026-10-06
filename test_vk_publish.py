import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.parse

import vk_publish as vk


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.text = self.root / "post.txt"
        self.text.write_text("Тест AUQNI", encoding="utf-8-sig")
        self.png = self.root / "image.png"
        self.png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fixture")

    def run_cli(self, arguments, token="test-only-credential"):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(vk, "read_token", return_value=token), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = vk.main(arguments)
        return result, out.getvalue(), err.getvalue()

    def test_dry_run_and_default_never_access_network(self):
        with patch.object(vk, "request_json", side_effect=AssertionError("network forbidden")) as net:
            for flags in ([], ["--dry-run"], ["--dry-run", "--image", str(self.png)]):
                code, out, err = self.run_cli(["--text-file", str(self.text)] + flags)
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(out)["network_requests"], 0)
                self.assertNotIn("test-only-credential", out + err)
            net.assert_not_called()

    def test_dry_run_without_token(self):
        code, out, _ = self.run_cli(["--text-file", str(self.text)], token="")
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)["token_available"])

    def test_existing_json_format_selects_vk_only(self):
        path = self.root / "post.json"
        path.write_text(json.dumps({"platforms": {
            "vk": {"content": "Текст VK"}, "telegram": {"content": "Другой текст"}
        }}), encoding="utf-8")
        self.assertEqual(vk.load_text(path), "Текст VK")

    def test_invalid_inputs_fail_before_network(self):
        with patch.object(vk, "request_json") as net:
            for text in ("", "x" * 16385):
                self.text.write_text(text, encoding="utf-8")
                self.assertEqual(self.run_cli(["--text-file", str(self.text)])[0], 1)
            self.text.write_text("valid", encoding="utf-8")
            self.png.write_bytes(b"not a PNG")
            self.assertEqual(self.run_cli([
                "--text-file", str(self.text), "--image", str(self.png)
            ])[0], 1)
            self.assertEqual(self.run_cli(["--text-file", str(self.root / "missing")])[0], 1)
            net.assert_not_called()

    def test_jpeg_signature(self):
        jpg = self.root / "photo.JPG"
        jpg.write_bytes(b"\xff\xd8\xfffixture")
        self.assertEqual(vk.load_image(jpg)[1], "image/jpeg")

    def test_text_publish_parameters(self):
        client = Mock()
        client.call.return_value = {"post_id": 10}
        self.assertEqual(vk.publish(client, "Текст", None), 10)
        client.call.assert_called_once_with(
            "wall.post", owner_id=-241580761, from_group=1, message="Текст"
        )
        client.upload.assert_not_called()

    def test_photo_publish_sequence(self):
        client = Mock()
        client.call.side_effect = [
            {"upload_url": "https://pu.vk.com/upload"},
            [{"owner_id": -241580761, "id": 22}], {"post_id": 33},
        ]
        client.upload.return_value = {"server": 1, "photo": "photo-data", "hash": "hash-data"}
        self.assertEqual(vk.publish(client, "Текст", vk.load_image(self.png)), 33)
        self.assertEqual([c.args[0] for c in client.call.call_args_list], [
            "photos.getWallUploadServer", "photos.saveWallPhoto", "wall.post"
        ])
        self.assertEqual(client.call.call_args_list[1].kwargs, {
            "group_id": 241580761, "server": 1, "photo": "photo-data", "hash": "hash-data"
        })
        self.assertEqual(client.call.call_args.kwargs["attachments"], "photo-241580761_22")

    def test_failed_upload_does_not_post(self):
        client = Mock()
        client.call.return_value = {"upload_url": "https://pu.vk.com/upload"}
        client.upload.return_value = {"photo": "[]"}
        with self.assertRaises(vk.PublishError):
            vk.publish(client, "Text", vk.load_image(self.png))
        self.assertEqual(client.call.call_count, 1)

    def test_api_error_does_not_leak_token_or_response(self):
        secret = "test-only-credential"
        with patch.object(vk, "request_json", return_value={"error": {
            "error_code": 27, "error_msg": secret, "request_params": [{"value": secret}]
        }}):
            with self.assertRaises(vk.PublishError) as raised:
                vk.VKClient(secret).call("wall.post", message="Text")
        self.assertIn("27", str(raised.exception))
        self.assertNotIn(secret, str(raised.exception))

    def test_token_only_in_post_body(self):
        with patch.object(vk, "request_json", return_value={"response": {"post_id": 1}}) as net:
            vk.VKClient("test-only-credential").call("wall.post", message="Текст")
        url, body, _ = net.call_args.args
        self.assertNotIn("test-only-credential", url)
        self.assertEqual(urllib.parse.parse_qs(body.decode())["access_token"], ["test-only-credential"])

    def test_upload_multipart_does_not_include_token(self):
        with patch.object(vk, "request_json", return_value={}) as net:
            vk.VKClient("test-only-credential").upload(
                "https://pu.vk.com/upload", vk.load_image(self.png)
            )
        _, body, mime = net.call_args.args
        self.assertIn(b'name="photo"', body)
        self.assertIn(self.png.read_bytes(), body)
        self.assertNotIn(b"test-only-credential", body)
        self.assertTrue(mime.startswith("multipart/form-data; boundary="))

    def test_untrusted_upload_url_rejected(self):
        with patch.object(vk, "request_json") as net:
            for url in ("http://pu.vk.com/upload", "https://vk.com.evil.example/upload"):
                with self.assertRaises(vk.PublishError):
                    vk.VKClient("fake").upload(url, vk.load_image(self.png))
            net.assert_not_called()

    def test_transport_error_is_sanitized_and_not_retried(self):
        opener = Mock()
        opener.open.side_effect = OSError("test-only-credential")
        with patch.object(vk.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(vk.PublishError) as raised:
                vk.request_json("https://api.vk.com/method/wall.post", b"", "test")
        self.assertNotIn("test-only-credential", str(raised.exception))
        self.assertEqual(opener.open.call_count, 1)

    def test_publish_without_token_is_blocked(self):
        with patch.object(vk, "request_json") as net:
            code, _, _ = self.run_cli(["--text-file", str(self.text), "--publish"], token="")
        self.assertEqual(code, 1)
        net.assert_not_called()

    def test_credential_in_content_is_blocked_without_echo(self):
        self.text.write_text("test-only-credential", encoding="utf-8")
        code, out, err = self.run_cli(["--text-file", str(self.text)])
        self.assertEqual(code, 1)
        self.assertNotIn("test-only-credential", out + err)


if __name__ == "__main__":
    unittest.main()
