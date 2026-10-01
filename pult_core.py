"""Shared validation, timing and pipeline helpers for the AUQNI Content Factory."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class PultError(Exception):
    pass


def moscow_zone():
    try:
        return ZoneInfo("Europe/Moscow")
    except ZoneInfoNotFoundError:
        # Windows without tzdata; Ubuntu uses the system IANA database.
        return timezone(timedelta(hours=3), "Europe/Moscow")


def utc_now():
    return datetime.now(timezone.utc)


def iso_utc(value):
    if value.tzinfo is None:
        raise ValueError("A timezone-aware datetime is required")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def local_label(value):
    if not value:
        return "без даты"
    return datetime.fromisoformat(value).astimezone(moscow_zone()).strftime("%d.%m.%Y · %H:%M МСК")


def next_slot(schedule, occupied, now=None):
    """First free configured slot, strictly after now, in Europe/Moscow."""
    now = (now or utc_now()).astimezone(moscow_zone())
    occupied = set(occupied)
    for day_offset in range(90):
        day = (now + timedelta(days=day_offset)).date()
        at = schedule.get(str(day.weekday()))
        if not at:
            continue
        hour, minute = map(int, at.split(":"))
        candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=moscow_zone())
        value = iso_utc(candidate)
        if candidate > now and value not in occupied:
            return value
    raise PultError("No free configured slot in the next 90 days")


def parse_local_time(value, now=None):
    """Accept DD.MM[.YYYY] HH:MM, with an optional comma or 'на'."""
    import re
    match = re.search(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\s+(\d{1,2}):(\d{2})", value)
    if not match:
        raise PultError("Укажите дату и время: ДД.ММ.ГГГГ ЧЧ:ММ")
    day, month, year, hour, minute = match.groups()
    today = (now or utc_now()).astimezone(moscow_zone())
    year = int(year) if year else today.year
    try:
        candidate = datetime(year, int(month), int(day), int(hour), int(minute), tzinfo=moscow_zone())
    except ValueError:
        raise PultError("Некорректная дата или время") from None
    if candidate <= today and not match.group(3):
        candidate = candidate.replace(year=year + 1)
    if candidate <= today:
        raise PultError("Дата должна быть в будущем")
    return iso_utc(candidate)


def validate_content(content_path, project_root):
    """Validate the unchanged auqni-content/v1 contract without choosing a channel."""
    root = Path(project_root).resolve()
    content_path = Path(content_path).resolve()
    if not content_path.is_relative_to(root) or not content_path.is_file():
        raise PultError("Content JSON is missing or outside the project")
    try:
        raw = content_path.read_bytes()
        content = json.loads(raw.decode("utf-8-sig"))
        if content["schema_version"] != "auqni-content/v1" or content["publication_status"] != "draft":
            raise ValueError
        required = {"source", "editorial_brief", "claims", "platforms", "image_prompt", "media", "review"}
        if not required.issubset(content) or set(content["platforms"]) != {"website", "telegram", "vk", "instagram"}:
            raise ValueError
        claim_ids = {claim["id"] for claim in content["claims"]}
        for platform in content["platforms"].values():
            if not isinstance(platform["content"], str) or not platform["content"].strip():
                raise ValueError
            if not set(platform["claim_ids"]).issubset(claim_ids):
                raise ValueError
        if any(re.search(r"\bCAPA\b", content["platforms"][name]["content"], re.I)
               for name in ("telegram", "vk", "instagram")):
            raise ValueError
        if not isinstance(content["image_prompt"], str) or not content["image_prompt"].strip():
            raise ValueError
        first_platform = next(iter(content["platforms"].values()))
        title = content.get("editorial_brief", {}).get("topic") or first_platform["content"].splitlines()[0]
    except (OSError, ValueError, KeyError, TypeError, AttributeError, UnicodeError):
        raise PultError("Pipeline returned an invalid auqni-content/v1") from None
    return content, title, hashlib.sha256(raw).hexdigest()


class Pipeline:
    def __init__(self, project_root, workspace_root, data_dir, command):
        self.root = Path(project_root).resolve()
        self.workspace = Path(workspace_root).resolve()
        self.data = Path(data_dir).resolve()
        self.command = command

    def run(self, item, instruction, inputs, previous=None):
        if not self.command:
            raise PultError("Pipeline command is not configured")
        self.data.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="auqni-pipeline-", dir=self.data) as temp:
            result = Path(temp) / "result.json"
            prompt = (
                "Используй существующий skill auqni-content-orchestrator и его Writer/Image Agent; "
                "сохрани auqni-content/v1 и PNG в существующих posts/ и images/. "
                "Не публикуй ничего. Не запускай Publisher в режиме publish. "
                "Учитывай Visual Guide и только подтверждённые факты AUQNI. "
                "Верни в финальном сообщении только JSON с абсолютными путями "
                '{"json_path":"...","image_path":"..."}.\n'
                f"Корень проекта: {self.root}\n"
                f"Материал №{item['id']}; задание: {instruction}\n"
                f"Вход: {json.dumps(inputs, ensure_ascii=False)}\n"
            )
            if previous:
                prompt += (f"Текущая версия: JSON {previous['content_path']}; PNG {previous['image_path']}. "
                           "Создай новую версию того же материала. Сохрани неизменённые части буквально.\n")
            args = [piece.format(workspace_root=self.workspace, project_root=self.root, result_path=result) for piece in self.command]
            env = os.environ.copy()
            env.pop("TELEGRAM_BOT_TOKEN", None)
            env.pop("AUQNI_OWNER_USER_ID", None)
            env.pop("OPENAI_API_KEY", None)
            try:
                proc = subprocess.run(args, input=prompt, text=True, cwd=self.workspace, env=env,
                                      capture_output=True, timeout=1800, check=False)
            except (OSError, subprocess.TimeoutExpired):
                raise PultError("Оркестратор не запустился или превысил время подготовки. Проверьте Codex на сервере.") from None
            if proc.returncode:
                raise PultError("Оркестратор остановил подготовку. Материал не готов; проверьте журнал Codex на сервере.")
            try:
                answer = json.loads(result.read_text(encoding="utf-8"))
                content_path = Path(answer["json_path"])
                image_path = Path(answer["image_path"])
            except (OSError, ValueError, KeyError, TypeError):
                raise PultError("Оркестратор не вернул пути к готовому тексту и изображению.") from None
            return content_path, image_path


def snapshot_version(data_dir, item_id, version, content_path, media_paths):
    """Keep a common version plus optional, independent channel media files."""
    import re
    target = Path(data_dir) / "materials" / str(item_id) / f"v{version}"
    if target.exists():
        raise PultError("This version directory already exists")
    target.mkdir(parents=True)
    content_target = target / "content.json"
    shutil.copyfile(content_path, content_target)
    saved = {}
    for channel, source in media_paths.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", channel):
            raise PultError("Invalid channel name")
        if source is None:
            saved[channel] = None
            continue
        source = Path(source)
        media_target = target / (channel + "-media" + source.suffix.lower())
        shutil.copyfile(source, media_target)
        saved[channel] = media_target
    return content_target, saved
