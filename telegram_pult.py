"""Private Telegram control bot for the existing AUQNI content factory.

Run with `python -B telegram_pult.py --config pult_config.json`. This process
uses Bot API only for the owner's private chat. Public posts always go through
the existing telegram_publish.py.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import time
import urllib.request
import uuid

from pult_core import (PultError, Pipeline, iso_utc, moscow_zone,
                       local_label, next_slot, parse_local_time,
                       snapshot_version, utc_now, validate_content)
from pult_store import Store
from pult_satire import SatireStream, review_label
from pult_schedule import main_variant, schedule_messages
from telegram_channel import TelegramPublisher


class BotAPI:
    def __init__(self, token):
        if not token:
            raise PultError("TELEGRAM_BOT_TOKEN is required")
        self._base = f"https://api.telegram.org/bot{token}/"
        self._file_base = f"https://api.telegram.org/file/bot{token}/"

    def call(self, method, fields=None, files=None, timeout=35):
        fields = fields or {}
        if files:
            boundary = "auqni" + uuid.uuid4().hex
            chunks = []
            for key, value in fields.items():
                chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode())
            for key, (filename, body, mime) in files.items():
                chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'
                               f'Content-Type: {mime}\r\n\r\n').encode() + body + b"\r\n")
            chunks.append(f"--{boundary}--\r\n".encode())
            data = b"".join(chunks)
            content_type = "multipart/form-data; boundary=" + boundary
        else:
            data = json.dumps(fields, ensure_ascii=False).encode("utf-8")
            content_type = "application/json"
        request = urllib.request.Request(self._base + method, data=data, method="POST",
                                         headers={"Content-Type": content_type})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                answer = json.load(response)
            if not answer.get("ok"):
                raise ValueError
            return answer.get("result")
        except Exception:
            # Never print a token-bearing URL or the raw Telegram response.
            raise PultError("Private Telegram Bot API request failed") from None

    def download(self, file_id, target, limit):
        meta = self.call("getFile", {"file_id": file_id})
        if not meta or meta.get("file_size", 0) > limit:
            raise PultError("Файл превышает лимит загрузки")
        remote = meta.get("file_path", "")
        if not remote or ".." in Path(remote).parts:
            raise PultError("Telegram returned an invalid file path")
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(self._file_base + remote)
        try:
            with urllib.request.urlopen(request, timeout=60) as response, target.open("wb") as stream:
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > limit:
                        raise PultError("Файл превышает лимит загрузки")
                    stream.write(chunk)
        except PultError:
            target.unlink(missing_ok=True)
            raise
        except Exception:
            target.unlink(missing_ok=True)
            raise PultError("Не удалось получить вложение из Telegram") from None
        return target

    def transcribe(self, audio_path, model):
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise PultError("Для голосовых нужен OPENAI_API_KEY на сервере")
        boundary = "auqni" + uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{model}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="voice.ogg"\r\n'
                f'Content-Type: audio/ogg\r\n\r\n').encode() + Path(audio_path).read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request("https://api.openai.com/v1/audio/transcriptions", data=body, method="POST",
                                         headers={"Authorization": "Bearer " + key,
                                                  "Content-Type": "multipart/form-data; boundary=" + boundary})
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                result = json.load(response)
            text = result["text"].strip()
            if not text:
                raise ValueError
            return text
        except Exception:
            raise PultError("Не удалось расшифровать голосовое сообщение") from None


class Pult:
    MAX_TODAY_REJECTIONS = 6

    def __init__(self, root, config, api, store=None, pipeline=None, publisher=None, planner=None):
        self.root = Path(root).resolve()
        self.workspace = self.root.parent.parent.resolve()
        self.config = config
        self.owner_id = int(os.environ.get("AUQNI_OWNER_USER_ID", "0"))
        if self.owner_id <= 0:
            raise PultError("AUQNI_OWNER_USER_ID is required")
        self.api = api
        self.data = self.root / config.get("data_dir", "pult_data")
        self.data.mkdir(parents=True, exist_ok=True)
        self.store = store or Store(self.data / "pult.sqlite3")
        self.pipeline = pipeline or Pipeline(self.root, self.workspace, self.data, config.get("pipeline_command"))
        self.channel = "telegram"  # This module is the current owner UI adapter.
        self.publisher = publisher or TelegramPublisher(self.root)
        self.publishers = {self.channel: self.publisher}
        channel_config = config.get("channels", {"telegram": {"enabled": True, "schedule": config.get("schedule", {})}})
        self.schedule = channel_config[self.channel]["schedule"]
        self.max_attachment = int(config.get("max_attachment_bytes", 20 * 1024 * 1024))
        self.transcription_model = config.get("transcription_model", "gpt-4o-mini-transcribe")
        self.planner = planner
        satire_settings = config.get("smk_satire", {})
        self.satire = (SatireStream(self.store, self.root / "content/smk_satire_bank.json", satire_settings)
                       if satire_settings.get("enabled") else None)
        if self.satire:
            self.satire.seed()

    def plan_autonomously(self, now=None):
        """Fill free configured slots with unapproved drafts through the existing queue."""
        from pult_planner import EditorialPlanner, candidate_inputs, upcoming_slots
        now = now or utc_now()
        settings = self.config.get("autonomous_planning", {})
        if not settings.get("enabled"):
            return 0
        local = now.astimezone(moscow_zone())
        if local.hour < int(settings.get("run_after_hour", 7)):
            return 0
        urgent_added = self.plan_today(now)
        today = local.date().isoformat()
        if self.store.get_kv("autoplan_last_attempt") == today:
            return urgent_added
        self.store.set_kv("autoplan_last_attempt", today)
        slots = upcoming_slots(self.schedule, self.store.occupied_slots(self.channel), now,
                               int(settings.get("min_lead_hours", 48)),
                               int(settings.get("horizon_days", 10)))
        slots = slots[:int(settings.get("max_new_per_run", 4))]
        if not slots:
            # Keep the professional environment fresh even when the calendar is full.
            from pult_planner import collect_evidence
            try:
                collect_evidence(self.root)
            except Exception as error:
                print(f"Autoplanner source scan failed: {type(error).__name__}", file=sys.stderr)
            return urgent_added
        planner = self.planner or EditorialPlanner(self.root, self.workspace,
                                                  self.config.get("pipeline_command"))
        try:
            candidates, evidence, errors = planner.propose(slots, self.store.topic_history())
            counts = getattr(planner, "last_review_counts", (len(candidates), len(candidates)))
            print(f"Autoplanner candidates: received={counts[0]}, passed={counts[1]}", file=sys.stderr)
            added = 0
            for slot, candidate in zip(slots, candidates):
                inputs = candidate_inputs(candidate, evidence)
                item_id = self.store.create("post", candidate["topic"],
                                            json.dumps(inputs, ensure_ascii=False), (self.channel,))
                self.store.set_schedule(item_id, self.channel, slot)
                self.store.enqueue(item_id, inputs["text"], inputs)
                added += 1
            if added:
                self.safe_say(f"Контент-завод выбрал {added} тем и готовит публикации заранее. "
                              "Готовые текст и визуал придут сюда на согласование; без «Принято» отправки не будет.")
            else:
                self.safe_say("Контент-завод не нашёл достаточно сильных тем для свободных слотов на будущем горизонте. "
                              "Публикации без вашего согласования не будет.")
            if errors:
                print("Autoplanner source warning: " + ", ".join(errors[:5]), file=sys.stderr)
            return urgent_added + added
        except Exception as error:
            detail = str(error) if isinstance(error, PultError) else "внутренняя ошибка выбора тем"
            self.safe_say(f"Автоплан на сегодня не составлен: {detail}. Существующие материалы и расписание сохранены.")
            print(f"Autoplanner failed: {type(error).__name__}", file=sys.stderr)
            return urgent_added

    def plan_today(self, now=None):
        """Try an open slot today using existing materials, then the existing planner and pipeline."""
        from pult_planner import EditorialPlanner, candidate_inputs
        now = now or utc_now()
        local = now.astimezone(moscow_zone())
        clock = self.schedule.get(str(local.weekday()))
        if not clock:
            return 0
        hour, minute = map(int, clock.split(":"))
        slot = iso_utc(datetime(local.year, local.month, local.day, hour, minute, tzinfo=moscow_zone()))
        if slot <= iso_utc(now) or slot in self.store.occupied_slots(self.channel):
            return 0
        key = f"autoplan_today_attempt:{local.date().isoformat()}"
        if self.store.get_kv(key):
            return 0
        self.store.set_kv(key, slot)  # one exhaustive attempt, even across worker restarts
        self.store.set_kv(f"autoplan_today_state:{slot}", "searching")
        rejected = json.loads(self.store.get_kv(f"autoplan_today_rejected:{slot}") or "[]")
        if len(rejected) >= self.MAX_TODAY_REJECTIONS:
            self.store.set_kv(f"autoplan_today_state:{slot}", "not_found")
            self.safe_say(f"Основной слот сегодня, {clock} МСК, остался свободным: "
                          f"отклонены {len(rejected)} кандидатов для этого слота. "
                          "Без «Принято» публикации нет.")
            return 0

        def offer(item_id):
            self.store.set_schedule(item_id, self.channel, slot)
            try:
                self.show_review(item_id)
                return True
            except PultError:
                self.store.set_schedule(item_id, self.channel, None)
                return False

        for item in self.store.list_plan():
            state = item["channels"].get(self.channel, {})
            if item["current_version"] and state.get("selected") and not state.get("scheduled_at") and state.get("status") == "ready":
                version = self.store.version(item["id"])
                material = version["channels"].get(self.channel, {})
                try:
                    image = Path(material["media_path"])
                    self.publisher.validate(version["content_path"], material["text"], image, self.root)
                    self.publisher.dry_run(version["content_path"], image)
                except (PultError, KeyError, TypeError, OSError):
                    continue
                if offer(item["id"]):
                    return 1

        history = self.store.topic_history()
        for row in list(history):
            try:
                original_topic = json.loads(row["brief"]).get("planner", {}).get("topic")
            except (ValueError, TypeError, AttributeError):
                original_topic = None
            if original_topic:
                history.append({**row, "title": original_topic})
        known_titles = {re.sub(r"\W+", " ", row["title"].casefold()).strip()
                        for row in history}
        for content in sorted((self.root / "posts").glob("*-content.json"), reverse=True):
            item_id = None
            image = self.root / "images" / (content.name.removesuffix("-content.json") + ".png")
            if not image.is_file():
                continue
            try:
                obj, title, _ = validate_content(content, self.root)
                normalized = re.sub(r"\W+", " ", title.casefold()).strip()
                if normalized in known_titles:
                    continue
                unresolved = obj.get("review", {}).get("unresolved", [])
                if any("telegram" in str(field.get("field", "")).lower()
                       for field in unresolved if isinstance(field, dict)):
                    continue
                text = obj["platforms"]["telegram"]["content"]
                self.publisher.validate(content, text, image, self.root)
                self.publisher.dry_run(content, image)
                item_id = self.store.create("post", title, json.dumps({"archive_path": str(content)}, ensure_ascii=False),
                                            (self.channel,))
                self.store.set_schedule(item_id, self.channel, slot)
                saved, media = snapshot_version(self.data, item_id, 1, content, {self.channel: image})
                _, _, content_sha = validate_content(saved, self.root)
                _, text_sha, media_sha = self.publisher.validate(saved, text, media[self.channel], self.root)
                self.publisher.dry_run(saved, media[self.channel])
                self.store.add_version(item_id, saved, content_sha, title, {self.channel: {
                    "text": text, "media_path": media[self.channel],
                    "text_sha256": text_sha, "media_sha256": media_sha}})
                self.show_review(item_id)
                return 1
            except (PultError, OSError, ValueError, KeyError, TypeError) as error:
                if item_id is not None:
                    try:
                        self.store.set_schedule(item_id, self.channel, None)
                    except PultError:
                        pass
                    known_titles.add(normalized)
                print(f"Archive review skipped: {type(error).__name__}", file=sys.stderr)

        for item in self.store.list_plan():
            state = item["channels"].get(self.channel, {})
            if item["kind"] != "idea" or item["current_version"] or state.get("scheduled_at") or not state.get("selected"):
                continue
            try:
                inputs = json.loads(item["brief"])
                inputs["urgent_today"] = True
                if self._prepare_today(item["id"], inputs.get("text") or item["title"], inputs, slot):
                    return 1
            except (PultError, ValueError, TypeError):
                continue

        planner = self.planner or EditorialPlanner(self.root, self.workspace, self.config.get("pipeline_command"))
        checked = accepted = 0
        planner_failed = False
        try:
            candidates, evidence, errors = planner.propose([slot], history)
            checked, accepted = getattr(planner, "last_review_counts", (len(candidates), len(candidates)))
            print(f"Today planner candidates: received={checked}, passed={accepted}", file=sys.stderr)
            for candidate in candidates:
                normalized = re.sub(r"\W+", " ", candidate["topic"].casefold()).strip()
                if normalized in known_titles:
                    continue
                known_titles.add(normalized)
                inputs = candidate_inputs(candidate, evidence)
                inputs["urgent_today"] = True
                item_id = self.store.create("post", candidate["topic"], json.dumps(inputs, ensure_ascii=False), (self.channel,))
                if self._prepare_today(item_id, inputs["text"], inputs, slot):
                    return 1
            if errors:
                print("Today planner source warning: " + ", ".join(errors[:5]), file=sys.stderr)
        except Exception as error:
            planner_failed = True
            print(f"Today planner failed: {type(error).__name__}", file=sys.stderr)
        self.safe_say(f"Основной слот сегодня, {clock} МСК, остался свободным "
                      f"после проверки готовых материалов, черновиков и тем: рассмотрено {checked}, "
                      f"прошло отбор {accepted}. "
                      f"{'Проверка тем завершилась ошибкой' if planner_failed else 'Качественный пост не подготовлен'}; "
                      "без «Принято» публикации нет.")
        self.store.set_kv(f"autoplan_today_state:{slot}", "not_found")
        return 0

    def _prepare_today(self, item_id, instruction, inputs, slot):
        self.store.set_schedule(item_id, self.channel, slot)
        self.store.enqueue(item_id, instruction, inputs)
        self.process_one_job()
        item = self.store.get(item_id)
        if item["current_version"] and item["status"] == "ready":
            return True
        with self.store.connect() as db:
            pending = db.execute("SELECT 1 FROM jobs WHERE item_id=? AND status IN ('queued','running')", (item_id,)).fetchone()
        if pending:
            return True  # another queued job won the race; this one remains scheduled
        self.store.set_schedule(item_id, self.channel, None)
        return False

    @staticmethod
    def permanent_keyboard():
        return {"keyboard": [[{"text": "Создать пост"}, {"text": "Контент-план"}],
                            [{"text": "Расписание"}]],
                "resize_keyboard": True, "is_persistent": True}

    @staticmethod
    def inline(buttons):
        return {"inline_keyboard": [[{"text": text, "url": data} if data.startswith("https://")
                                     else {"text": text, "callback_data": data} for text, data in row]
                                    for row in buttons]}

    def say(self, text, buttons=None, permanent=False):
        payload = {"chat_id": self.owner_id, "text": text}
        if buttons:
            payload["reply_markup"] = self.inline(buttons)
        elif permanent:
            payload["reply_markup"] = self.permanent_keyboard()
        return self.api.call("sendMessage", payload)

    def safe_say(self, text, buttons=None):
        try:
            self.say(text, buttons)
        except PultError:
            pass

    def show_review(self, item_id):
        item = self.store.get(item_id)
        if not item or not item["current_version"]:
            raise PultError("Пост ещё не подготовлен")
        state = item["channels"].get(self.channel)
        if not state:
            raise PultError("Telegram не выбран для этого материала")
        shown_version = state["published_version"] if state["status"] == "published" else (
            state["attempt_version"] if state["status"] == "uncertain" else item["current_version"])
        version = self.store.version(item_id, shown_version)
        material = version["channels"].get(self.channel)
        if not state or not material or not material["media_path"]:
            raise PultError("Для Telegram нет готового текста или PNG")
        caption = material["text"]
        image = Path(material["media_path"]).read_bytes()
        slot = state["scheduled_at"]
        candidate = main_variant(self.store, slot, item_id, shown_version)
        visible_status = {"published": "опубликовано", "uncertain": "требует проверки",
                          "rejected": "отклонено", "preparing": "готовится"}.get(
                              state["status"], "согласовано" if state["approved_version"] == shown_version
                              else "ожидает согласования")
        heading = (f"Основной контент\nСлот: {local_label(slot)}\n{candidate}\n"
                   f"Статус: {visible_status}\n"
                   f"ID: {item_id}" if slot else
                   f"Материал · ID: {item_id}" + (f" · версия {shown_version}" if shown_version > 1 else ""))
        self.api.call("sendPhoto", {"chat_id": self.owner_id, "caption": heading},
                      {"photo": ("image.png", image, "image/png")})
        v = shown_version
        if state["status"] == "published":
            self.say(caption + f"\n\n{candidate} опубликован · ID: {item_id}\n"
                     f"Дата: {local_label(state['published_at'])}\n"
                     f"Статус: опубликовано\nСсылка: {state['public_url']}",
                     [[("Открыть публикацию", state["public_url"])]])
            return
        if state["status"] == "uncertain":
            self.say(caption + "\n\nРезультат публикации uncertain. Проверьте канал вручную; повтора нет.")
            return
        if state["status"] == "rejected":
            self.say(caption + "\n\nМатериал отклонён.")
            return
        if item["status"] == "preparing":
            self.say(caption + "\n\nГотовится новая версия; текущая версия закрыта для публикации.")
            return
        status = "\nПринято для плановой публикации." if state["approved_version"] == v else ""
        self.say(caption + status, [
            [("Принято", f"approve:{item_id}:{v}"), ("Опубликовать сейчас", f"publish:{item_id}:{v}")],
            [("Изменить", f"edit:{item_id}:{v}"), ("В контент-план", f"plan:{item_id}:{v}"), ("Отклонить", f"reject:{item_id}:{v}")],
        ])

    def show_item(self, item_id):
        item = self.store.get(item_id)
        if not item:
            raise PultError("Материал не найден")
        if item["current_version"]:
            self.show_review(item_id)
        else:
            self.say(f"Идея №{item_id}\n{item['title']}\n{item['brief'][:1000]}",
                     [[("Создать пост", f"make:{item_id}")]])

    def show_plan(self):
        rows = self.store.list_plan()
        scheduled = [r for r in rows if any(c["selected"] and c["scheduled_at"] for c in r["channels"].values())]
        ideas = [r for r in rows if r not in scheduled]
        lines = ["Контент-план · Europe/Moscow", "Ближайшие публикации:"]
        for row in scheduled[:10]:
            parts = [f"{name}: {local_label(c['scheduled_at'])} / {'принято' if c['approved_version'] == row['current_version'] and row['current_version'] else c['status']}"
                     for name, c in row["channels"].items() if c["selected"] and c["scheduled_at"]]
            slot = row["channels"].get(self.channel, {}).get("scheduled_at")
            label = main_variant(self.store, slot, row["id"], row["current_version"]) if slot else f"ID: {row['id']}"
            lines.append(f"{label} · ID: {row['id']} · {', '.join(parts)} · {row['title'][:70]}")
        if not scheduled:
            lines.append("Пока пусто")
        lines.append("\nБанк идей:")
        lines += [f"№{r['id']} · {r['title'][:90]}" for r in ideas[:10]] or ["Пока пусто"]
        lines.append("\nКоманды: «Покажи №37», «Поправь №37: ...», «Перенеси №37 на 02.10.2026 19:00», «Убери №37».")
        buttons = [[("Добавить идею", "idea:new")]]
        buttons += [[(f"№{r['id']} → пост", f"make:{r['id']}")] for r in ideas[:5] if not r["current_version"]]
        buttons += [[(f"Показать №{r['id']}", f"show:{r['id']}")] for r in scheduled[:5]]
        self.say("\n".join(lines), buttons)

    def show_schedule(self, days=7, free_only=False, now=None):
        pages = schedule_messages(self.store, self.schedule, self.config.get("smk_satire", {}),
                                  days=days, free_only=free_only, now=now)
        buttons = [[("7 дней", "schedule:7:all"), ("14 дней", "schedule:14:all")],
                   [("Все слоты" if free_only else "Только свободные",
                     f"schedule:{days}:{'all' if free_only else 'free'}")]]
        for number, page in enumerate(pages):
            self.say(page, buttons if number == len(pages) - 1 else None)

    def _input_from_message(self, message):
        parts = []
        text = message.get("text") or message.get("caption")
        if text:
            parts.append(text.strip())
        attachments = []
        for kind in ("voice", "photo", "video", "document"):
            if kind not in message:
                continue
            media = message[kind][-1] if kind == "photo" else message[kind]
            if media.get("file_size", 0) > self.max_attachment:
                raise PultError("Файл слишком большой")
            extension = {"voice": ".ogg", "photo": ".jpg", "video": ".mp4", "document": Path(media.get("file_name", "file.bin")).suffix.lower()}[kind]
            if not re.fullmatch(r"\.[a-z0-9]{1,10}", extension):
                extension = ".bin"
            path = self.data / "inbox" / (uuid.uuid4().hex + extension)
            self.api.download(media["file_id"], path, self.max_attachment)
            attachments.append({"type": kind, "path": str(path)})
            if kind == "voice":
                parts.append(self.api.transcribe(path, self.transcription_model))
        if not parts and not attachments:
            raise PultError("Пришлите тему, текст или вложение")
        return {"text": "\n".join(parts), "attachments": attachments}

    def handle_message(self, message):
        user = message.get("from", {})
        chat = message.get("chat", {})
        if user.get("id") != self.owner_id or chat.get("id") != self.owner_id or chat.get("type") != "private":
            return
        text = (message.get("text") or "").strip()
        if text == "/start":
            self.store.clear_pending()
            self.say("Личный пульт AUQNI. Выберите действие.", permanent=True)
            return
        if text == "Создать пост":
            self.store.set_pending({"kind": "create_input"})
            self.say("Пришлите тему, текст, голосовое, фото, видео или документ.", permanent=True)
            return
        if text == "Контент-план":
            self.show_plan()
            return
        if text == "Расписание":
            self.show_schedule()
            return
        show = re.fullmatch(r"Покажи\s+№?(\d+)", text, re.I)
        edit = re.fullmatch(r"Поправь\s+№?(\d+)\s*:\s*(.+)", text, re.I | re.S)
        move = re.fullmatch(r"Перенеси\s+№?(\d+)\s+на\s+(.+)", text, re.I)
        remove = re.fullmatch(r"Убери\s+№?(\d+)", text, re.I)
        if show:
            self.show_item(int(show.group(1)))
            return
        if edit:
            item_id = int(edit.group(1)); item = self.store.get(item_id)
            if not item:
                raise PultError("Материал не найден")
            if item["current_version"]:
                self.store.start_edit(item_id, item["current_version"])
            self.store.enqueue(item_id, edit.group(2), {"text": edit.group(2), "attachments": []})
            self.say(f"№{item_id}: правка принята в работу. Статус «Принято» снят.")
            return
        if move:
            item_id = int(move.group(1))
            self.store.set_schedule(item_id, self.channel, parse_local_time(move.group(2)))
            state = self.store.get(item_id)["channels"][self.channel]
            self.say(f"№{item_id} · Telegram перенесён на {local_label(state['scheduled_at'])}. Примите новую дату перед автопубликацией.")
            return
        if remove:
            item_id = int(remove.group(1))
            self.store.set_schedule(item_id, self.channel, None)
            self.say(f"№{item_id} · Telegram убран из календаря и сохранён в плане без даты.")
            return
        pending = self.store.get_pending()
        if not pending:
            self.say("Выберите «Создать пост» или «Контент-план». Можно также написать «Покажи №37».", permanent=True)
            return
        kind = pending["kind"]
        if kind in ("create_input", "idea_input", "edit_input"):
            inputs = self._input_from_message(message)
            if kind == "edit_input":
                item_id = pending["item_id"]
                item = self.store.get(item_id)
                if not item or item["current_version"] != pending["version"]:
                    raise PultError("Версия изменилась; откройте материал заново")
                self.store.start_edit(item_id, pending["version"])
                self.store.enqueue(item_id, inputs["text"] or "Измени по приложенному материалу", inputs)
                self.store.clear_pending()
                self.say(f"№{item_id}: новая версия готовится. Статус «Принято» снят.")
            elif kind == "idea_input":
                item_id = self.store.create("idea", (inputs["text"] or "Идея из вложения")[:100], json.dumps(inputs, ensure_ascii=False), channels=[self.channel])
                self.store.clear_pending()
                self.say(f"Идея сохранена как №{item_id}.", [[("Создать пост", f"make:{item_id}")]])
            else:
                self.store.set_pending({"kind": "create_confirm", "inputs": inputs})
                summary = (inputs["text"] or "Вложение")[:400]
                self.say("Вход для поста:\n" + summary + "\n\nЧто сделать?", [[("Создать", "confirm:create"), ("В контент-план", "confirm:plan"), ("Отмена", "confirm:cancel")]])

    def handle_callback(self, query):
        user = query.get("from", {})
        message = query.get("message", {})
        if user.get("id") != self.owner_id or message.get("chat", {}).get("id") != self.owner_id:
            return
        data = query.get("data", "")
        self.api.call("answerCallbackQuery", {"callback_query_id": query["id"]})
        schedule = re.fullmatch(r"schedule:(7|14):(all|free)", data)
        if schedule:
            self.show_schedule(int(schedule.group(1)), schedule.group(2) == "free")
            return
        if data.startswith("satire:"):
            if not self.satire:
                raise PultError("Поток SMK_SATIRE выключен")
            match = re.fullmatch(r"satire:(approve|reject):(SMK-\d{3})", data)
            if not match:
                raise PultError("Неизвестная кнопка SMK_SATIRE")
            action, post_id = match.groups()
            if action == "approve":
                self.satire.approve(post_id)
                self.say(f"{post_id} принят для {local_label(self.satire.get(post_id)['scheduled_at'])}.")
            else:
                self.satire.reject(post_id)
                self.say(f"{post_id} отклонён; публикации не будет.")
            return
        if data.startswith("confirm:"):
            pending = self.store.get_pending()
            if not pending or pending.get("kind") != "create_confirm":
                raise PultError("Ввод уже обработан")
            self.store.clear_pending()
            if data == "confirm:cancel":
                self.say("Отменено.")
            elif data == "confirm:plan":
                inputs = pending["inputs"]
                item_id = self.store.create("idea", (inputs["text"] or "Идея из вложения")[:100], json.dumps(inputs, ensure_ascii=False), channels=[self.channel])
                self.say(f"Сохранено в банк идей как №{item_id}.")
            elif data == "confirm:create":
                inputs = pending["inputs"]
                when = next_slot(self.schedule, self.store.occupied_slots(self.channel))
                item_id = self.store.create("post", (inputs["text"] or "Материал из вложения")[:100], json.dumps(inputs, ensure_ascii=False), channels=[self.channel])
                self.store.set_schedule(item_id, self.channel, when)
                self.store.enqueue(item_id, inputs["text"] or "Подготовь материал из приложенного файла", inputs)
                self.say(f"Публикация №{item_id} готовится. План: {local_label(when)}.")
            return
        if data == "idea:new":
            self.store.set_pending({"kind": "idea_input"})
            self.say("Пришлите идею текстом или голосовым сообщением.")
            return
        if data.startswith("show:"):
            self.show_item(int(data.split(":")[1]))
            return
        if data.startswith("make:"):
            item_id = int(data.split(":")[1]); item = self.store.get(item_id)
            if not item or item["kind"] != "idea" or item["current_version"]:
                raise PultError("Идея уже обработана")
            when = next_slot(self.schedule, self.store.occupied_slots(self.channel, item_id))
            self.store.set_schedule(item_id, self.channel, when)
            inputs = json.loads(item["brief"])
            self.store.enqueue(item_id, inputs.get("text") or "Подготовь пост из идеи", inputs)
            self.say(f"№{item_id} готовится. План: {local_label(when)}.")
            return
        match = re.fullmatch(r"(approve|publish|edit|plan|reject):(\d+):(\d+)", data)
        if not match:
            raise PultError("Неизвестная кнопка")
        action, raw_id, raw_version = match.groups()
        item_id, version = int(raw_id), int(raw_version)
        item = self.store.get(item_id)
        if not item or item["current_version"] != version:
            raise PultError("Кнопка относится к старой версии")
        if action == "approve":
            self.store.approve(item_id, version, self.channel)
            slot = item["channels"][self.channel]["scheduled_at"]
            candidate = main_variant(self.store, slot, item_id, version)
            self.say(f"{candidate} принят. Слот {local_label(slot)} заполнен. ID: {item_id}.")
        elif action == "publish":
            self.publish_item(item_id, version, scheduled=False)
        elif action == "edit":
            self.store.set_pending({"kind": "edit_input", "item_id": item_id, "version": version})
            self.say("Что изменить? Ответьте текстом или голосовым сообщением.")
        elif action == "plan":
            self.store.to_plan(item_id, self.channel)
            self.say(f"№{item_id} оставлен в контент-плане без статуса «Принято».")
        elif action == "reject":
            scheduled = item["channels"][self.channel]["scheduled_at"]
            candidate = main_variant(self.store, scheduled, item_id, version) if scheduled else f"Материал ID: {item_id}"
            now = utc_now()
            local = now.astimezone(moscow_zone())
            clock = self.schedule.get(str(local.weekday()))
            today_slot = (iso_utc(datetime(local.year, local.month, local.day, *map(int, clock.split(":")),
                                           tzinfo=moscow_zone())) if clock else None)
            retry_slot = scheduled if scheduled == today_slot and scheduled > iso_utc(now) else None
            self.store.reject(item_id, self.channel, retry_slot=retry_slot)
            self.say(f"{candidate} отклонён." +
                     (f" Подбираю следующий материал для слота {local_label(retry_slot)}." if retry_slot else ""))

    def publish_item(self, item_id, version, scheduled, now=None, channel="telegram"):
        publisher = self.publishers.get(channel)
        if publisher is None:
            raise PultError("Канал не подключён к Publisher")
        row = self.store.version(item_id, version)
        if not row:
            raise PultError("Нет сохранённой версии")
        material = row["channels"].get(channel)
        if not material:
            raise PultError("У версии нет материала для выбранного канала")
        content = Path(row["content_path"])
        image = Path(material["media_path"]) if material["media_path"] else None
        title, text_hash, media_hash = publisher.validate(content, material["text"], image, self.root)
        _, _, content_hash = validate_content(content, self.root)
        if (content_hash != row["content_sha256"] or text_hash != material["text_sha256"]
                or media_hash != material["media_sha256"]):
            raise PultError("Файлы версии изменились после проверки; публикация остановлена")
        publisher.dry_run(content, image)
        if not self.store.claim_publish(item_id, version, channel, scheduled=scheduled, now=now):
            return False
        try:
            message_id, url = publisher.publish(content, image)
        except Exception:
            self.store.publication_result(item_id, channel, "uncertain")
            self.safe_say(f"№{item_id} · {channel}: результат отправки uncertain. Автоматического повтора не будет; проверьте канал вручную.")
            return False
        self.store.publication_result(item_id, channel, "published", message_id, url)
        published_at = self.store.get(item_id)["channels"][channel]["published_at"]
        slot = self.store.get(item_id)["channels"][channel]["scheduled_at"]
        candidate = main_variant(self.store, slot, item_id, version) if channel == self.channel else f"ID: {item_id}"
        self.safe_say(f"{candidate} опубликован · ID: {item_id}\n«{title}»\n"
                      f"Дата: {local_label(published_at)}\nСтатус: опубликовано\nСсылка: {url}",
                      [[("Открыть публикацию", url)]])
        return True

    def tick_satire(self, now):
        for post_id, body, stamp in self.satire.plan(now):
            self.safe_say(review_label(post_id, body, stamp),
                          [[("Принято", f"satire:approve:{post_id}"),
                            ("Отклонить", f"satire:reject:{post_id}")]])
        for row in self.satire.due(now):
            if not self.satire.claim(row["id"], now):
                continue
            try:
                result = self.api.call("sendMessage", {"chat_id": "@auqni_qms", "text": row["text"]})
                if (not isinstance(result, dict) or type(result.get("message_id")) is not int
                        or result.get("chat", {}).get("username") != "auqni_qms"):
                    raise PultError("Telegram не подтвердил адрес и номер публикации")
                self.satire.result(row["id"], result["message_id"])
                self.safe_say(f"{row['id']} опубликован: https://t.me/auqni_qms/{result['message_id']}")
            except Exception:
                self.satire.result(row["id"])
                self.safe_say(f"{row['id']}: результат отправки uncertain. Проверьте канал вручную; повтора нет.")

    def tick(self, now=None):
        now = now or utc_now()
        if self.satire:
            try:
                self.tick_satire(now)
            except Exception as error:
                print(f"SMK_SATIRE tick failed: {type(error).__name__}", file=sys.stderr)
        lead = timedelta(hours=int(self.config.get("prepare_lead_hours", 72)))
        for idea in self.store.ideas_to_prepare(now + lead):
            inputs = json.loads(idea["brief"])
            self.store.enqueue(idea["id"], inputs.get("text") or "Подготовь пост из запланированной идеи", inputs)
            self.safe_say(f"№{idea['id']}: плановая идея поставлена на подготовку. После готовности пришлю пост и визуал на согласование.")
        for item_id, version, channel in self.store.due(now, channels=self.publishers):
            try:
                self.publish_item(item_id, version, scheduled=True, now=now, channel=channel)
            except Exception as error:
                state = self.store.get(item_id)["channels"][channel]
                if state["status"] == "publishing":
                    # A request may have reached the network. Never retry automatically.
                    self.store.publication_result(item_id, channel, "uncertain")
                    self.safe_say(f"№{item_id} · {channel}: результат отправки uncertain. Проверьте канал вручную; повтора нет.")
                elif state["status"] == "approved":
                    # The failure happened before the network attempt.
                    self.store.to_plan(item_id, channel)
                    detail = str(error) if isinstance(error, PultError) else "внутренняя ошибка проверки"
                    self.safe_say(f"№{item_id} · {channel}: плановая публикация остановлена до отправки: {detail}. Нужно повторное «Принято».")

    def process_one_job(self):
        job = self.store.claim_job()
        if not job:
            return False
        item_id = job["item_id"]
        inputs = {}
        try:
            item = self.store.get(item_id)
            previous = self.store.version(item_id) if item["current_version"] else None
            if previous:
                previous = {"content_path": previous["content_path"],
                            "image_path": previous["channels"].get(self.channel, {}).get("media_path")}
            inputs = json.loads(job["input_json"])
            content, image = self.pipeline.run(item, job["instruction"], inputs, previous)
            obj, title, _ = validate_content(content, self.root)
            prepared = {channel: adapter.prepare(obj, image)
                        for channel, adapter in self.publishers.items()
                        if channel in self.store.selected_channels(item_id)}
            if not prepared:
                raise PultError("Нет подключённого канала для подготовки")
            for channel, source in prepared.items():
                self.publishers[channel].validate(content, source["text"], source["media_path"], self.root)
            version = item["current_version"] + 1
            saved_content, media = snapshot_version(self.data, item_id, version, content,
                                                     {channel: source["media_path"] for channel, source in prepared.items()})
            obj, title, content_sha = validate_content(saved_content, self.root)
            materials = {}
            for channel, platform in obj["platforms"].items():
                text = platform["content"]
                materials[channel] = {"text": text,
                                      "media_path": media.get(channel),
                                      "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                                      "media_sha256": hashlib.sha256(Path(media[channel]).read_bytes()).hexdigest() if media.get(channel) else None}
            for channel, source in prepared.items():
                _, text_sha, media_sha = self.publishers[channel].validate(saved_content, source["text"], media[channel], self.root)
                self.publishers[channel].dry_run(saved_content, media[channel])
                materials[channel] = {"text": source["text"], "media_path": media[channel],
                                      "text_sha256": text_sha, "media_sha256": media_sha}
            self.store.add_version(item_id, saved_content, content_sha, title, materials)
            self.store.finish_job(job["id"])
            try:
                self.show_review(item_id)
            except PultError:
                pass
        except Exception as error:
            self.store.finish_job(job["id"], str(error)[:300])
            if inputs.get("urgent_today"):
                print(f"Today preparation failed for №{item_id}: {type(error).__name__}", file=sys.stderr)
            else:
                self.safe_say(f"№{item_id}: подготовка остановлена. {str(error)[:250]}")
        return True

    def handle_update(self, update):
        try:
            if "message" in update:
                self.handle_message(update["message"])
            elif "callback_query" in update:
                self.handle_callback(update["callback_query"])
            elif self.satire and "message_reaction_count" in update:
                self.satire.record_reaction_count(update["message_reaction_count"])
        except PultError as error:
            self.say(str(error))

    def run(self):
        stop = threading.Event()
        interrupted = self.store.recover_jobs()
        uncertain = self.store.recover_publications()
        satire_uncertain = self.satire.recover() if self.satire else []

        def worker():
            while not stop.is_set():
                try:
                    if not self.process_one_job():
                        self.plan_autonomously()
                        stop.wait(3)
                except Exception:
                    print("Pult worker encountered an internal error", file=sys.stderr)
                    stop.wait(3)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        self.say("Пульт AUQNI запущен." + (f" Незавершённых заданий после перезапуска: {interrupted}." if interrupted else ""), permanent=True)
        for state in uncertain:
            self.safe_say(f"№{state['item_id']} · {state['channel']}: отправка прервалась при перезапуске. "
                          "Статус uncertain; проверьте канал вручную. Автоматического повтора нет.")
        for post_id in satire_uncertain:
            self.safe_say(f"{post_id}: отправка прервалась. Статус uncertain; проверьте канал вручную.")
        try:
            while True:
                self.tick()
                try:
                    allowed = ["message", "callback_query"]
                    if self.satire:
                        allowed.append("message_reaction_count")
                    updates = self.api.call("getUpdates", {"offset": self.store.get_offset(), "timeout": 5,
                                                           "allowed_updates": allowed}, timeout=15)
                    for update in updates or []:
                        self.handle_update(update)
                        self.store.set_offset(update["update_id"] + 1)
                except PultError:
                    time.sleep(5)
        finally:
            stop.set()
            thread.join(timeout=5)


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    configured = config.get("channels", {})
    if set(configured) != {"telegram"} or configured["telegram"].get("enabled") is not True:
        raise PultError("Only Telegram is connected in this MVP")
    schedule = configured["telegram"].get("schedule", {})
    if not isinstance(schedule, dict) or not schedule:
        raise PultError("Schedule is missing")
    for weekday, clock in schedule.items():
        if weekday not in {str(i) for i in range(7)} or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clock):
            raise PultError("Invalid schedule entry")
    if config.get("timezone") != "Europe/Moscow":
        raise PultError("Only Europe/Moscow is supported")
    satire = config.get("smk_satire", {})
    if not isinstance(satire, dict) or not isinstance(satire.get("enabled", False), bool):
        raise PultError("Invalid SMK_SATIRE configuration")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", satire.get("time", "08:30")):
        raise PultError("Invalid SMK_SATIRE time")
    if satire.get("weekdays", [0, 1, 2, 3, 4]) != [0, 1, 2, 3, 4]:
        raise PultError("SMK_SATIRE runs Monday through Friday only")
    if type(satire.get("horizon_days", 10)) is not int or not 1 <= satire.get("horizon_days", 10) <= 30:
        raise PultError("Invalid SMK_SATIRE horizon")
    launch = satire.get("launch_queue", [])
    if (not isinstance(launch, list) or len(launch) not in (0, 10)
            or any(not isinstance(post_id, str) or not re.fullmatch(r"SMK-\d{3}", post_id) for post_id in launch)
            or len(set(launch)) != len(launch)):
        raise PultError("Invalid SMK_SATIRE launch queue")
    return config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="pult_config.json")
    parser.add_argument("--check", action="store_true", help="Local deployment preflight; no Telegram requests")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    try:
        config = load_config(root / args.config)
        if args.check:
            workspace = root.parent.parent
            checks = {
                "owner_id_configured": os.environ.get("AUQNI_OWNER_USER_ID", "").isdigit() and int(os.environ.get("AUQNI_OWNER_USER_ID", "0")) > 0,
                "telegram_token_available": bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
                "project_env_file_absent": not (root / ".env").exists(),
                "codex_command_available": bool(shutil.which(config.get("pipeline_command", [""])[0])),
                "orchestrator_skill_present": (workspace / ".agents/skills/auqni-content-orchestrator/SKILL.md").is_file(),
                "writer_agent_present": (workspace / ".codex/agents/auqni-content-writer.toml").is_file(),
                "image_agent_present": (workspace / ".codex/agents/auqni-image-generator.toml").is_file(),
                "visual_guide_present": (root / "docs/visual-guide-auqni-content-v1.md").is_file(),
                "publisher_present": (root / "telegram_publish.py").is_file(),
                "voice_transcription_key_available": bool(os.environ.get("OPENAI_API_KEY")),
            }
            print(json.dumps(checks, ensure_ascii=False, indent=2))
            return 0 if all(value for key, value in checks.items() if key != "voice_transcription_key_available") else 1
        if (root / ".env").exists():
            raise PultError("Move the existing .env outside the workspace before starting the Codex pipeline")
        Pult(root, config, BotAPI(os.environ.get("TELEGRAM_BOT_TOKEN", ""))).run()
    except PultError as error:
        print("Pult error: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
