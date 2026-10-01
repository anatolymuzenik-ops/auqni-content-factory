"""Telegram-only validation and adapter to the existing public publisher."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

from pult_core import PultError, validate_content


def validate_telegram_material(content_path, image_path, project_root):
    content, title, _ = validate_content(content_path, project_root)
    image_path = Path(image_path).resolve()
    root = Path(project_root).resolve()
    if not image_path.is_relative_to(root) or not image_path.is_file():
        raise PultError("Telegram PNG is missing or outside the project")
    image = image_path.read_bytes()
    if not image.startswith(b"\x89PNG\r\n\x1a\n"):
        raise PultError("Telegram image is not PNG")
    caption = content["platforms"]["telegram"]["content"]
    if caption != caption.strip() or "[уточнить]" in caption:
        raise PultError("Telegram text contains an unresolved marker or edge whitespace")
    return title, hashlib.sha256(caption.encode("utf-8")).hexdigest(), hashlib.sha256(image).hexdigest()


class TelegramPublisher:
    def __init__(self, project_root, journal=None):
        self.root = Path(project_root).resolve()
        self.journal = Path(journal).resolve() if journal else self.root / "telegram_publications.jsonl"
        self.script = self.root / "telegram_publish.py"

    def prepare(self, content, default_image):
        return {"text": content["platforms"]["telegram"]["content"],
                "media_path": default_image}

    def validate(self, content_path, text, media_path, project_root):
        content, _, _ = validate_content(content_path, project_root)
        if text != content["platforms"]["telegram"]["content"]:
            raise PultError("Telegram text differs from auqni-content/v1")
        return validate_telegram_material(content_path, media_path, project_root)

    def _call(self, content_path, image_path, mode):
        args = [sys.executable, "-B", str(self.script), "--content-json", str(content_path),
                "--image", str(image_path), "--channel", "@auqni_qms", "--journal", str(self.journal), mode]
        try:
            return subprocess.run(args, cwd=self.root, capture_output=True, text=True, timeout=50, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise PultError("Не удалось завершить вызов Telegram Publisher. Проверка результата нужна до повтора.") from None

    def dry_run(self, content_path, image_path):
        result = self._call(content_path, image_path, "--dry-run")
        if result.returncode:
            raise PultError("Telegram Publisher dry-run failed: " + result.stderr.strip()[:300])
        try:
            report = json.loads(result.stdout)
        except ValueError:
            raise PultError("Telegram Publisher dry-run returned invalid JSON") from None
        if report.get("validation") != "ok" or report.get("network_requests") != 0 or report.get("channel") != "@auqni_qms":
            raise PultError("Telegram Publisher dry-run did not validate the package")
        return report

    def publish(self, content_path, image_path):
        report = self.dry_run(content_path, image_path)
        result = self._call(content_path, image_path, "--publish")
        key = report["idempotency_key"]
        try:
            rows = [json.loads(line) for line in self.journal.read_text(encoding="utf-8").splitlines() if line.strip()]
            matches = [row for row in rows if row.get("idempotency_key") == key]
        except (OSError, ValueError):
            matches = []
        if result.returncode == 0 and matches and matches[-1].get("status") == "published":
            row = matches[-1]
            return row["message_id"], row["publication_url"]
        raise PultError("Telegram result uncertain; check the channel manually before any retry")
