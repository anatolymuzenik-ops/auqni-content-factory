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
        today = local.date().isoformat()
        if self.store.get_kv("autoplan_last_attempt") == today:
            return 0
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
            return 0
        planner = self.planner or EditorialPlanner(self.root, self.workspace,
                                                  self.config.get("pipeline_command"))
        try:
            candidates, evidence, errors = planner.propose(slots, self.store.topic_history())
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
                self.safe_say("Контент-завод сегодня не нашёл достаточно сильных тем для свободных слотов. "
                              "Публикации без вашего согласования не будет.")
            if errors:
                print("Autoplanner source warning: " + ", ".join(errors[:5]), file=sys.stderr)
            return added
        except Exception as error:
            detail = str(error) if isinstance(error, PultError) else "внутренняя ошибка выбора тем"
            self.safe_say(f"Автоплан на сегодня не составлен: {detail}. Существующие материалы и расписание сохранены.")
            print(f"Autoplanner failed: {type(error).__name__}", file=sys.stderr)
            return 0

    @staticmethod
    def permanent_keyboard():
        return {"keyboard": [[{"text": "Создать пост"}, {"text": "Контент-план"}]],
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
        heading = f"Публикация №{item_id} · v{shown_version}\nПлан: {local_label(state['scheduled_at'])}"
        self.api.call("sendPhoto", {"chat_id": self.owner_id, "caption": heading},
                      {"photo": ("image.png", image, "image/png")})
        v = shown_version
        if state["status"] == "published":
            self.say(caption + f"\n\nОпубликовано · №{item_id} v{v}\n"
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
            lines.append(f"№{row['id']} · v{row['current_version']} · {', '.join(parts)} · {row['title'][:70]}")
        if not scheduled:
            lines.append("Пока пусто")
        lines.append("\nБанк идей:")
        lines += [f"№{r['id']} · {r['title'][:90]}" for r in ideas[:10]] or ["Пока пусто"]
        lines.append("\nКоманды: «Покажи №37», «Поправь №37: ...», «Перенеси №37 на 02.10.2026 19:00», «Убери №37».")
        buttons = [[("Добавить идею", "idea:new")]]
        buttons += [[(f"№{r['id']} → пост", f"make:{r['id']}")] for r in ideas[:5] if not r["current_version"]]
        buttons += [[(f"Показать №{r['id']}", f"show:{r['id']}")] for r in scheduled[:5]]
        self.say("\n".join(lines), buttons)

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
            self.say(f"Принято · №{item_id} v{version} · Telegram. Публикация: {local_label(item['channels'][self.channel]['scheduled_at'])}.")
        elif action == "publish":
            self.publish_item(item_id, version, scheduled=False)
        elif action == "edit":
            self.store.set_pending({"kind": "edit_input", "item_id": item_id, "version": version})
            self.say("Что изменить? Ответьте текстом или голосовым сообщением.")
        elif action == "plan":
            self.store.to_plan(item_id, self.channel)
            self.say(f"№{item_id} оставлен в контент-плане без статуса «Принято».")
        elif action == "reject":
            self.store.reject(item_id, self.channel)
            self.say(f"№{item_id} отклонён.")

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
        self.safe_say(f"Опубликовано · №{item_id} v{version}\n«{title}»\n"
                      f"Дата: {local_label(published_at)}\nСтатус: опубликовано\nСсылка: {url}",
                      [[("Открыть публикацию", url)]])
        return True

    def tick(self, now=None):
        now = now or utc_now()
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
            self.safe_say(f"№{item_id}: подготовка остановлена. {str(error)[:250]}")
        return True

    def handle_update(self, update):
        try:
            if "message" in update:
                self.handle_message(update["message"])
            elif "callback_query" in update:
                self.handle_callback(update["callback_query"])
        except PultError as error:
            self.say(str(error))

    def run(self):
        stop = threading.Event()
        interrupted = self.store.recover_jobs()
        uncertain = self.store.recover_publications()

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
        try:
            while True:
                self.tick()
                try:
                    updates = self.api.call("getUpdates", {"offset": self.store.get_offset(), "timeout": 5,
                                                           "allowed_updates": ["message", "callback_query"]}, timeout=15)
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
