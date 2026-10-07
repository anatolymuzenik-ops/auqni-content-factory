"""Optional SMK satire stream inside the existing Pult store, owner UI and tick."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
import hashlib
import json
import os
from pathlib import Path
import subprocess
import struct
import tempfile
import uuid
import zlib

from pult_core import PultError, iso_utc, local_label, moscow_zone, utc_now


MIX = ("pure", "problem", "pure", "soft", "pure",
       "problem", "pure", "soft", "pure", "problem")
MAKEUP = ("pure", "soft", "pure", "pure", "soft",
          "pure", "soft", "pure", "pure", "soft")
VISUAL_ROLES = (
    "женщина, специалист по качеству, деловая одежда, ироничное недоумение",
    "мужчина, внутренний аудитор среднего возраста, рубашка, удивление",
    "женщина, врач в медицинском халате, решительная улыбка",
    "пожилой мужчина, руководитель процесса, деловой костюм, растерянность",
    "мужчина, медработник в хирургическом костюме, озадаченность",
    "женщина, молодой координатор СМК, деловая одежда, живое раздражение",
)


def visual_file(path, root):
    """Accept only a real square PNG in the project image directory."""
    path = Path(path).resolve()
    if not path.is_relative_to((Path(root) / "images" / "smk_satire").resolve()):
        raise PultError("Визуал SMK_SATIRE находится вне каталога проекта")
    try:
        body = path.read_bytes()
        if len(body) > 10 * 1024 * 1024 or body[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError
        width, height = struct.unpack(">II", body[16:24])
        if width != height or width < 1000 or width > 2000:
            raise ValueError
        offset, seen_idat, seen_end = 8, False, False
        while offset + 12 <= len(body):
            length = struct.unpack(">I", body[offset:offset + 4])[0]
            kind = body[offset + 4:offset + 8]
            end = offset + 12 + length
            if end > len(body) or zlib.crc32(body[offset + 4:offset + 8 + length]) != struct.unpack(">I", body[end - 4:end])[0]:
                raise ValueError
            seen_idat |= kind == b"IDAT"
            if kind == b"IEND":
                seen_end = end == len(body)
                break
            offset = end
        if not seen_idat or not seen_end:
            raise ValueError
    except (OSError, ValueError, struct.error):
        raise PultError("Нужен готовый квадратный PNG SMK_SATIRE") from None
    return str(path), hashlib.sha256(body).hexdigest()


def reuse_visual(old, new):
    """Conservative: reuse only for small wording edits of the same joke."""
    if not old or not old.get("image_path") or old["genre"] != new["genre"] or old["topic"] != new["topic"]:
        return False
    return SequenceMatcher(None, old["text"].casefold(), new["text"].casefold()).ratio() >= 0.88


class SatireImageGenerator:
    """Use the existing Codex/imagegen command, with the approved style image as reference."""

    def __init__(self, root, workspace, data_dir, command):
        self.root, self.workspace, self.data_dir = map(lambda x: Path(x).resolve(), (root, workspace, data_dir))
        self.command = command

    def run(self, candidate, role):
        if not self.command:
            raise PultError("Команда генерации изображения SMK_SATIRE не настроена")
        reference = self.root / "visual-tests/smk-satire-chaos/03-character.png"
        if not reference.is_file():
            raise PultError("Не найден утверждённый визуальный референс SMK_SATIRE")
        target_dir = self.root / "images/smk_satire"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"smk-{uuid.uuid4().hex}.png"
        with tempfile.TemporaryDirectory(prefix="smk-visual-", dir=self.data_dir) as temp:
            result = Path(temp) / "result.json"
            prompt = (
                "Подготовь РОВНО ОДНУ квадратную PNG-иллюстрацию 1254×1254 для утреннего "
                "SMK_SATIRE. Используй встроенный image_gen.imagegen согласно skill imagegen. "
                "Приложенная картинка — ТОЛЬКО стилевой референс: сохрани белый воздушный фон, "
                "живую иллюстрацию и палитру AUQNI (тёмно-синий, яркий синий, бирюза), "
                "но создай иную композицию, ситуацию и лицо. Никаких реалистичных фотолюдей. "
                "Персонаж: " + role + ". Изобрази одну ясную сатирическую метафору конкретного "
                "поста с чек-листами, интерфейсами, документами или рабочим абсурдом по смыслу. "
                "Не копируй сцену с перепутанными стрелками. Не добавляй рекламную плашку, "
                "логотип, метку SMK_SATIRE или полный текст поста. Избегай текста на картинке: "
                "допустим только один короткий, безошибочный статус, если он усилит шутку. "
                f"Текст поста: {candidate['text']}\nТема: {candidate['topic']}. "
                f"Сохрани готовый PNG по абсолютному пути {target}. "
                "Не меняй другие файлы проекта и ничего не публикуй. "
                "Последний ответ строго JSON: "
                '{"status":"ok","local_image_path":"' + str(target) + '","image_prompt":"краткое описание сцены"}'
            )
            args = [piece.format(workspace_root=self.workspace, project_root=self.root,
                                 result_path=result) for piece in self.command]
            args[args.index("-"):args.index("-")] = ["-i", str(reference)]
            env = os.environ.copy()
            for key in ("TELEGRAM_BOT_TOKEN", "AUQNI_OWNER_USER_ID", "OPENAI_API_KEY"):
                env.pop(key, None)
            try:
                proc = subprocess.run(args, input=prompt, text=True, cwd=self.workspace,
                                      env=env, capture_output=True, timeout=900, check=False)
            except (OSError, subprocess.TimeoutExpired):
                raise PultError("Генератор изображения SMK_SATIRE не запустился или превысил время") from None
            if proc.returncode:
                raise PultError("Генератор изображения SMK_SATIRE завершился с ошибкой")
            try:
                payload = json.loads(result.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise PultError("Генератор изображения SMK_SATIRE вернул неверный формат") from None
            if payload.get("status") != "ok" or payload.get("local_image_path") != str(target):
                raise PultError("Генератор изображения SMK_SATIRE не создал нужный файл")
            path, digest = visual_file(target, self.root)
            return {"image_path": path, "image_sha256": digest,
                    "visual_role": role, "visual_prompt": str(payload.get("image_prompt", ""))[:1000]}


class SatireWriter:
    """Use the configured Codex writer command for short, text-only satire."""

    def __init__(self, root, workspace, data_dir, command):
        self.root = Path(root).resolve()
        self.workspace = Path(workspace).resolve()
        self.data_dir = Path(data_dir).resolve()
        self.command = command

    def run(self, idea, previous=None, instruction=None, mix_type=None):
        if not self.command:
            raise PultError("Команда подготовки SMK_SATIRE не настроена")
        with tempfile.TemporaryDirectory(prefix="smk-satire-", dir=self.data_dir) as temp:
            result = Path(temp) / "result.json"
            prompt = (
                "Ты редактор короткой профессиональной сатиры AUQNI SMK. "
                "Создай внутри минимум три разных варианта и выбери самый смешной. "
                "Критерии: узнаваемая боль СМК, логичный абсурд, сильный финал, пересылаемость. "
                "Не пиши аффирмацию, экспертный пост с одной шуткой, мораль или рекламный слоган. "
                "Не копируй шутки из банка. Новые материалы могут опираться на процессы, риски, "
                "аудиты, корректирующие действия, показатели, документы, наблюдения и продуктовые гипотезы. "
                "Допустимы чистая сатира, сатира с короткой мыслью, с человеческим вопросом или "
                "мягкой связью с AUQNI. Вопрос о боли аудитории и приглашение к CustDev уместны "
                "только если продолжают саму шутку; не добавляй их механически. "
                "Ссылку и упоминание AUQNI не вставляй автоматически. "
                "pure — без CTA и рекламы; problem — узнаваемая проблема; soft — уместный "
                "вопрос или мягкая связь с AUQNI. Пиши по-русски, обычно 1–4 короткие фразы. "
                "Не публикуй и не меняй файлы проекта. Верни ТОЛЬКО JSON объект "
                '{"text":"...","genre":"...","topic":"...","mix_type":"pure|problem|soft",'
                '"product_context":null} без Markdown.\n'
                f"Идея пользователя или редакционный вход: {idea}\n"
            )
            if previous:
                prompt += (f"Это правка существующего поста. Текущая версия: {previous}. "
                           f"Пожелание владельца: {instruction}. Сохрани категорию {mix_type}. "
                           "Напиши новую редакцию, не повторяй старую буквально.\n")
            args = [piece.format(workspace_root=self.workspace, project_root=self.root,
                                 result_path=result) for piece in self.command]
            env = os.environ.copy()
            for key in ("TELEGRAM_BOT_TOKEN", "AUQNI_OWNER_USER_ID", "OPENAI_API_KEY"):
                env.pop(key, None)
            try:
                proc = subprocess.run(args, input=prompt, text=True, cwd=self.workspace,
                                      env=env, capture_output=True, timeout=900, check=False)
            except (OSError, subprocess.TimeoutExpired):
                raise PultError("Редактор SMK_SATIRE не запустился или превысил время подготовки") from None
            if proc.returncode:
                raise PultError("Редактор SMK_SATIRE не смог подготовить пост")
            try:
                candidate = json.loads(result.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise PultError("Редактор SMK_SATIRE вернул неверный формат") from None
            return validate_candidate(candidate, mix_type)


def validate_candidate(candidate, required_mix=None):
    if not isinstance(candidate, dict):
        raise PultError("Некорректный кандидат SMK_SATIRE")
    for key, limit in (("text", 900), ("genre", 80), ("topic", 120)):
        value = candidate.get(key)
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
            raise PultError("Некорректный текст или метаданные SMK_SATIRE")
    if len(candidate["text"].strip()) < 25:
        raise PultError("Сатирический пост слишком короткий")
    kind = candidate.get("mix_type")
    if kind not in {"pure", "problem", "soft"} or (required_mix and kind != required_mix):
        raise PultError("Нарушена категория SMK_SATIRE")
    context = candidate.get("product_context")
    if context is not None and (not isinstance(context, str) or len(context) > 160):
        raise PultError("Некорректный продуктовый контекст SMK_SATIRE")
    return {"text": candidate["text"].strip(), "genre": candidate["genre"].strip(),
            "topic": candidate["topic"].strip(), "mix_type": kind,
            "product_context": context}


def read_bank(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema_version") != "smk-satire-bank/v1":
        raise PultError("Неверная версия банка SMK_SATIRE")
    rows = data.get("candidates")
    if not isinstance(rows, list) or len(rows) < 60:
        raise PultError("Банк SMK_SATIRE неполон")
    ids = set()
    for row in rows:
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k].strip()
            for k in ("id", "text", "genre", "topic")):
            raise PultError("Некорректная запись SMK_SATIRE")
        if row["id"] in ids or row.get("mix_type") not in MIX or not isinstance(row.get("selected"), bool):
            raise PultError("Дубли или некорректная категория SMK_SATIRE")
        ids.add(row["id"])
    selected = [row for row in rows if row["selected"]]
    if len(selected) < 40 or [sum(r["mix_type"] == kind for r in selected) for kind in ("pure", "problem", "soft")] != [20, 12, 8]:
        raise PultError("Нарушена пропорция отбора SMK_SATIRE")
    return rows


class SatireStream:
    def __init__(self, store, bank_path, settings, root=None):
        self.store = store
        self.bank = read_bank(bank_path)
        self.settings = settings
        self.root = Path(root or Path(bank_path).resolve().parent.parent).resolve()

    def require_visual(self, post_id, version=None):
        row = self.version(post_id, version)
        if not row or not row["image_path"] or not row["image_sha256"]:
            raise PultError("Изображение SMK_SATIRE ещё не готово; согласование недоступно")
        path, digest = visual_file(row["image_path"], self.root)
        if digest != row["image_sha256"]:
            raise PultError("Изображение SMK_SATIRE изменилось; нужно подготовить его заново")
        return path

    def recover_visuals(self):
        """An old text-only approval cannot authorize a newly generated image."""
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            revoked = [row[0] for row in db.execute("""SELECT p.id FROM satire_posts p
                JOIN satire_versions v ON v.post_id=p.id AND v.version=p.current_version
                WHERE p.status='approved' AND v.image_path IS NULL""")]
            db.execute("""UPDATE satire_posts SET status='scheduled' WHERE id IN (
                SELECT p.id FROM satire_posts p JOIN satire_versions v
                ON v.post_id=p.id AND v.version=p.current_version
                WHERE p.status='approved' AND v.image_path IS NULL)""")
            db.execute("UPDATE satire_versions SET visual_state='failed' WHERE visual_state='preparing'")
            return revoked

    def claim_visual(self, now=None):
        now = now or utc_now()
        retry_before = iso_utc(now - timedelta(minutes=10))
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT v.*,p.status,p.scheduled_at FROM satire_versions v
                JOIN satire_posts p ON p.id=v.post_id AND p.current_version=v.version
                WHERE p.status IN ('scheduled','draft') AND v.image_path IS NULL
                AND (v.visual_state IS NULL OR (v.visual_state='failed'
                     AND v.visual_attempts<3 AND v.visual_last_attempt<=?))
                ORDER BY CASE WHEN p.scheduled_at IS NULL THEN 1 ELSE 0 END,
                         p.scheduled_at, v.created_at LIMIT 1""", (retry_before,)).fetchone()
            if not row:
                return None
            db.execute("""UPDATE satire_versions SET visual_state='preparing',
                visual_attempts=visual_attempts+1,visual_last_attempt=?
                WHERE post_id=? AND version=?""", (iso_utc(now), row["post_id"], row["version"]))
            roles = [r[0] for r in db.execute("""SELECT visual_role FROM satire_versions
                WHERE visual_role IS NOT NULL ORDER BY COALESCE(visual_last_attempt,created_at) DESC LIMIT 2""")]
            available = [role for role in VISUAL_ROLES if role not in roles]
            count = db.execute("SELECT count(*) FROM satire_versions WHERE visual_role IS NOT NULL").fetchone()[0]
            return dict(row), available[count % len(available)]

    def next_visual_role(self):
        with self.store.connect() as db:
            roles = [r[0] for r in db.execute("""SELECT visual_role FROM satire_versions
                WHERE visual_role IS NOT NULL ORDER BY COALESCE(visual_last_attempt,created_at) DESC LIMIT 2""")]
            available = [role for role in VISUAL_ROLES if role not in roles]
            count = db.execute("SELECT count(*) FROM satire_versions WHERE visual_role IS NOT NULL").fetchone()[0]
            return available[count % len(available)]

    def invalidate_visual(self, post_id, version):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE satire_posts SET status='scheduled' WHERE id=? AND current_version=? AND status='approved'",
                       (post_id, version))
            db.execute("""UPDATE satire_versions SET image_path=NULL,image_sha256=NULL,visual_state=NULL,
                visual_attempts=0 WHERE post_id=? AND version=?""", (post_id, version))

    def finish_visual(self, post_id, version, visual):
        path, digest = visual_file(visual["image_path"], self.root)
        if digest != visual["image_sha256"]:
            raise PultError("Хеш изображения SMK_SATIRE не совпал")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT p.status,v.visual_state FROM satire_posts p JOIN satire_versions v
                ON v.post_id=p.id AND v.version=p.current_version
                WHERE p.id=? AND p.current_version=?""", (post_id, version)).fetchone()
            if not row or row["visual_state"] != "preparing" or row["status"] not in ("scheduled", "draft"):
                raise PultError("Версия SMK_SATIRE изменилась во время подготовки изображения")
            db.execute("""UPDATE satire_versions SET image_path=?,image_sha256=?,visual_role=?,
                visual_prompt=?,visual_source_version=?,visual_state='ready'
                WHERE post_id=? AND version=?""", (path, digest, visual["visual_role"],
                visual["visual_prompt"], version, post_id, version))

    def fail_visual(self, post_id, version):
        with self.store.connect() as db:
            db.execute("""UPDATE satire_versions SET visual_state='failed'
                WHERE post_id=? AND version=? AND visual_state='preparing'""", (post_id, version))

    def retry_visual(self, post_id, version):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute("""UPDATE satire_versions SET visual_state=NULL,visual_attempts=0,
                visual_last_attempt=NULL WHERE post_id=? AND version=? AND visual_state='failed'
                AND image_path IS NULL AND EXISTS (SELECT 1 FROM satire_posts p
                    WHERE p.id=satire_versions.post_id AND p.current_version=?
                    AND p.status IN ('scheduled','draft'))""", (post_id, version, version))
            if not result.rowcount:
                raise PultError("Эта версия уже изменилась или изображение готовится")

    def seed(self):
        with self.store.connect() as db:
            for row in self.bank:
                db.execute("""INSERT OR IGNORE INTO satire_posts
                    (id,text,genre,topic,mix_type,product_context,selected,status,reactions_json)
                    VALUES(?,?,?,?,?,?,?,'unused',?)""",
                    (row["id"], row["text"], row["genre"], row["topic"],
                     row["mix_type"], row.get("product_context"), int(row["selected"]), None))
            # Snapshot the actual production text, including any older edits,
            # rather than replacing it with the bank on restart.
            db.execute("""INSERT OR IGNORE INTO satire_versions
                (post_id,version,text,genre,topic,mix_type,product_context,source,instruction,created_at)
                SELECT id,1,text,genre,topic,mix_type,product_context,'bank',NULL,?
                FROM satire_posts WHERE origin='bank'""", (iso_utc(utc_now()),))

    def add_idea(self, text):
        text = text.strip()
        if not text or len(text) > 2000:
            raise PultError("Идея SMK_SATIRE должна быть короче 2000 символов")
        with self.store.connect() as db:
            return db.execute("""INSERT INTO satire_submissions(kind,text,status,submitted_at)
                VALUES('text',?,'USER_SUBMISSION',?)""", (text, iso_utc(utc_now()))).lastrowid

    def begin_edit(self, post_id, version):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,current_version FROM satire_posts WHERE id=?", (post_id,)).fetchone()
            if not row or row["current_version"] != version or row["status"] not in ("scheduled", "approved", "draft"):
                raise PultError("Пост уже изменился или недоступен для правки")
            db.execute("UPDATE satire_posts SET status='editing' WHERE id=?", (post_id,))

    def cancel_edit(self, post_id, version):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT scheduled_at,current_version,status FROM satire_posts WHERE id=?", (post_id,)).fetchone()
            if not row or row["current_version"] != version or row["status"] != "editing":
                raise PultError("Правка уже обработана")
            if db.execute("""SELECT 1 FROM satire_submissions WHERE target_post_id=?
                AND base_version=? AND status IN ('USER_SUBMISSION','MODERATION') AND last_error IS NULL""",
                (post_id, version)).fetchone():
                raise PultError("Правка уже готовится; дождитесь новой версии")
            future = row["scheduled_at"] and row["scheduled_at"] > iso_utc(utc_now())
            db.execute("UPDATE satire_posts SET status=?,scheduled_at=? WHERE id=?",
                       ("scheduled" if future else "draft", row["scheduled_at"] if future else None, post_id))

    def queue_edit(self, post_id, version, instruction):
        instruction = instruction.strip()
        if not instruction or len(instruction) > 2000:
            raise PultError("Опишите правку SMK_SATIRE короче 2000 символов")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,current_version FROM satire_posts WHERE id=?", (post_id,)).fetchone()
            if not row or row["current_version"] != version or row["status"] != "editing":
                raise PultError("Пост уже изменился или недоступен для правки")
            if db.execute("""SELECT 1 FROM satire_submissions WHERE target_post_id=?
                AND base_version=? AND status IN ('USER_SUBMISSION','MODERATION') AND last_error IS NULL""",
                (post_id, version)).fetchone():
                raise PultError("Правка уже готовится")
            return db.execute("""INSERT INTO satire_submissions
                (kind,text,status,submitted_at,target_post_id,base_version)
                VALUES('text',?,'USER_SUBMISSION',?,?,?)""",
                (instruction, iso_utc(utc_now()), post_id, version)).lastrowid

    def claim_submission(self):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM satire_submissions WHERE status='USER_SUBMISSION' ORDER BY id LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE satire_submissions SET status='MODERATION' WHERE id=?", (row["id"],))
            return dict(row)

    def recover_submissions(self):
        with self.store.connect() as db:
            db.execute("""UPDATE satire_submissions SET status='USER_SUBMISSION'
                WHERE status='MODERATION' AND result_post_id IS NULL AND last_error IS NULL""")

    def finish_submission(self, submission, candidate, visual):
        candidate = validate_candidate(candidate)
        path, digest = visual_file(visual["image_path"], self.root)
        if digest != visual["image_sha256"]:
            raise PultError("Хеш изображения SMK_SATIRE не совпал")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT status,target_post_id,base_version FROM satire_submissions WHERE id=?",
                             (submission["id"],)).fetchone()
            if not job or job["status"] != "MODERATION":
                raise PultError("Идея SMK_SATIRE уже обработана")
            target = job["target_post_id"]
            if target:
                old = db.execute("SELECT * FROM satire_posts WHERE id=?", (target,)).fetchone()
                if (not old or old["status"] != "editing" or old["current_version"] != job["base_version"]
                        or candidate["mix_type"] != old["mix_type"]):
                    raise PultError("Пост изменился во время подготовки")
                version = old["current_version"] + 1
                future = old["scheduled_at"] and old["scheduled_at"] > iso_utc(utc_now())
                db.execute("""UPDATE satire_posts SET text=?,genre=?,topic=?,product_context=?,
                    current_version=?,status=?,scheduled_at=? WHERE id=?""",
                    (candidate["text"], candidate["genre"], candidate["topic"],
                     candidate["product_context"], version, "scheduled" if future else "draft",
                     old["scheduled_at"] if future else None, target))
                post_id = target
            else:
                duplicate = db.execute("SELECT id FROM satire_posts WHERE lower(trim(text))=lower(trim(?))",
                                       (candidate["text"],)).fetchone()
                if duplicate:
                    raise PultError("Такая шутка уже есть в SMK_SATIRE")
                last = db.execute("SELECT MAX(CAST(SUBSTR(id,5) AS INTEGER)) FROM satire_posts WHERE id GLOB 'SMK-[0-9]*'").fetchone()[0] or 0
                post_id = f"SMK-{last + 1:03d}"
                version = 1
                db.execute("""INSERT INTO satire_posts
                    (id,text,genre,topic,mix_type,product_context,selected,status,current_version,origin,source_submission_id)
                    VALUES(?,?,?,?,?,?,1,'draft',1,'user',?)""",
                    (post_id, candidate["text"], candidate["genre"], candidate["topic"],
                     candidate["mix_type"], candidate["product_context"], submission["id"]))
            db.execute("""INSERT INTO satire_versions
                (post_id,version,text,genre,topic,mix_type,product_context,source,instruction,created_at,
                 image_path,image_sha256,visual_role,visual_prompt,visual_source_version,visual_state,visual_last_attempt)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (post_id, version, candidate["text"], candidate["genre"], candidate["topic"],
                 candidate["mix_type"], candidate["product_context"], "user", submission["text"],
                 iso_utc(utc_now()), path, digest, visual["visual_role"], visual["visual_prompt"],
                 visual.get("visual_source_version", version), "ready", iso_utc(utc_now())))
            db.execute("UPDATE satire_submissions SET result_post_id=?,edited_text=? WHERE id=?",
                       (post_id, candidate["text"], submission["id"]))
            return post_id

    def fail_submission(self, submission, error):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE satire_submissions SET last_error=? WHERE id=? AND status='MODERATION'",
                       (str(error)[:250], submission["id"]))
            if submission["target_post_id"]:
                old = db.execute("SELECT scheduled_at FROM satire_posts WHERE id=? AND status='editing'",
                                 (submission["target_post_id"],)).fetchone()
                if old:
                    future = old["scheduled_at"] and old["scheduled_at"] > iso_utc(utc_now())
                    db.execute("UPDATE satire_posts SET status=?,scheduled_at=? WHERE id=?",
                               ("scheduled" if future else "draft", old["scheduled_at"] if future else None,
                                submission["target_post_id"]))

    def retry_submission(self, submission_id):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM satire_submissions WHERE id=?", (submission_id,)).fetchone()
            if not row or row["status"] != "MODERATION" or not row["last_error"] or row["result_post_id"]:
                raise PultError("Эту идею уже нельзя повторить")
            if row["target_post_id"]:
                post = db.execute("SELECT status,current_version FROM satire_posts WHERE id=?",
                                  (row["target_post_id"],)).fetchone()
                if not post or post["current_version"] != row["base_version"] or post["status"] not in ("scheduled", "draft"):
                    raise PultError("Пост уже изменился; создайте новую правку")
                db.execute("UPDATE satire_posts SET status='editing' WHERE id=?", (row["target_post_id"],))
            db.execute("UPDATE satire_submissions SET status='USER_SUBMISSION',last_error=NULL WHERE id=?",
                       (submission_id,))

    def version(self, post_id, version=None):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM satire_versions WHERE post_id=? AND version=COALESCE(?,"
                             "(SELECT current_version FROM satire_posts WHERE id=?))",
                             (post_id, version, post_id)).fetchone()
            return dict(row) if row else None

    def plan(self, now=None):
        """Reserve morning slots; never approve a candidate or publish it."""
        now = (now or utc_now()).astimezone(moscow_zone())
        horizon = int(self.settings.get("horizon_days", 10))
        clock = self.settings.get("time", "08:30")
        hour, minute = map(int, clock.split(":"))
        added = []
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            launch = self.settings.get("launch_queue", [])
            # The owner-approved first ten are intentionally 4/6/0. The next ten
            # restore 50/30/20 across the first twenty planned posts.
            launch_pending = [post_id for post_id in launch if db.execute(
                "SELECT 1 FROM satire_posts WHERE id=? AND status='unused' AND selected=1",
                (post_id,)).fetchone()]
            nonlaunch_count = db.execute(
                "SELECT count(*) FROM satire_posts WHERE origin='bank' AND status!='unused' AND id NOT IN (" +
                ",".join("?" for _ in launch) + ")", launch).fetchone()[0] if launch else 0
            count = db.execute("SELECT count(*) FROM satire_posts WHERE origin='bank' AND status NOT IN ('unused','rejected')").fetchone()[0]
            for offset in range(horizon + 1):
                day = (now + timedelta(days=offset)).date()
                if day.weekday() >= 5:
                    continue
                slot = datetime(day.year, day.month, day.day, hour, minute, tzinfo=moscow_zone())
                if slot <= now:
                    continue
                stamp = iso_utc(slot)
                if db.execute("SELECT 1 FROM satire_posts WHERE scheduled_at=?", (stamp,)).fetchone():
                    continue
                if launch_pending:
                    post_id = launch_pending.pop(0)
                    row = db.execute("SELECT id,text FROM satire_posts WHERE id=?", (post_id,)).fetchone()
                else:
                    kind = (MAKEUP[nonlaunch_count] if launch and nonlaunch_count < len(MAKEUP)
                            else MIX[(nonlaunch_count - len(MAKEUP)) % len(MIX)] if launch
                            else MIX[count % len(MIX)])
                    row = db.execute("""SELECT id,text FROM satire_posts
                        WHERE selected=1 AND status='unused' AND mix_type=? ORDER BY id LIMIT 1""", (kind,)).fetchone()
                    nonlaunch_count += 1
                if not row:
                    break
                db.execute("UPDATE satire_posts SET status='scheduled', scheduled_at=? WHERE id=?", (stamp, row["id"]))
                added.append((row["id"], row["text"], stamp))
                count += 1
        return added

    def get(self, post_id):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM satire_posts WHERE id=?", (post_id,)).fetchone()
            return dict(row) if row else None

    def _free_slot(self, db, now):
        local = now.astimezone(moscow_zone())
        hour, minute = map(int, self.settings.get("time", "08:30").split(":"))
        weekdays = self.settings.get("weekdays", [0, 1, 2, 3, 4])
        for offset in range(91):
            day = (local + timedelta(days=offset)).date()
            if day.weekday() not in weekdays:
                continue
            slot = datetime(day.year, day.month, day.day, hour, minute, tzinfo=moscow_zone())
            if slot <= local:
                continue
            stamp = iso_utc(slot)
            if not db.execute("SELECT 1 FROM satire_posts WHERE scheduled_at=?", (stamp,)).fetchone():
                return stamp
        raise PultError("Нет свободного утреннего слота SMK_SATIRE в ближайшие 90 дней")

    def approve(self, post_id, now=None, version=None):
        now = now or utc_now()
        self.require_visual(post_id, version)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,scheduled_at,current_version FROM satire_posts WHERE id=?", (post_id,)).fetchone()
            if not row or (version is not None and row["current_version"] != version):
                raise PultError("Карточка SMK_SATIRE устарела")
            if row["status"] not in ("scheduled", "draft"):
                raise PultError("Шутка уже недоступна для принятия; проверьте дату")
            stamp = row["scheduled_at"] if row["status"] == "scheduled" else self._free_slot(db, now)
            if stamp <= iso_utc(now):
                raise PultError("Время публикации прошло; сначала перенесите публикацию")
            db.execute("UPDATE satire_posts SET status='approved',scheduled_at=? WHERE id=?", (stamp, post_id))
            db.execute("UPDATE satire_submissions SET status='APPROVED',reviewed_at=? "
                       "WHERE result_post_id=? AND status='MODERATION' AND last_error IS NULL",
                       (iso_utc(now), post_id))
            return stamp

    def reject(self, post_id, version=None):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,current_version FROM satire_posts WHERE id=?", (post_id,)).fetchone()
            if not row or (version is not None and row["current_version"] != version):
                raise PultError("Карточка SMK_SATIRE устарела")
            if row["status"] not in ("scheduled", "approved", "draft"):
                raise PultError("Шутку нельзя отклонить")
            db.execute("UPDATE satire_posts SET status='rejected', scheduled_at=NULL WHERE id=?", (post_id,))
            db.execute("UPDATE satire_submissions SET status='REJECTED',reviewed_at=? "
                       "WHERE result_post_id=? AND status='MODERATION'",
                       (iso_utc(utc_now()), post_id))

    def due(self, now=None):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute("""SELECT * FROM satire_posts
                WHERE status='approved' AND scheduled_at<=? ORDER BY scheduled_at""", (iso_utc(now or utc_now()),))]

    def claim(self, post_id, now=None):
        self.require_visual(post_id)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute("""UPDATE satire_posts SET status='publishing'
                WHERE id=? AND status='approved' AND scheduled_at<=?""", (post_id, iso_utc(now or utc_now())))
            return result.rowcount == 1

    def result(self, post_id, message_id=None):
        with self.store.connect() as db:
            if message_id is None:
                db.execute("UPDATE satire_posts SET status='uncertain' WHERE id=? AND status='publishing'", (post_id,))
            else:
                db.execute("""UPDATE satire_posts SET status='published', published_at=?, external_id=?, public_url=?
                    WHERE id=? AND status='publishing'""", (iso_utc(utc_now()), str(message_id),
                    f"https://t.me/auqni_qms/{message_id}", post_id))
                db.execute("UPDATE satire_submissions SET status='PUBLISHED' WHERE result_post_id=? AND status='APPROVED'",
                           (post_id,))

    def recover(self):
        with self.store.connect() as db:
            rows = [r[0] for r in db.execute("SELECT id FROM satire_posts WHERE status='publishing'")]
            db.execute("UPDATE satire_posts SET status='uncertain' WHERE status='publishing'")
            return rows

    def record_reaction_count(self, update):
        """Persist an observed Bot API count snapshot for our published post only."""
        chat = update.get("chat", {})
        message_id = update.get("message_id")
        timestamp = update.get("date")
        reactions = update.get("reactions")
        if (not isinstance(chat, dict) or chat.get("username") != "auqni_qms"
                or type(message_id) is not int or type(timestamp) is not int
                or not isinstance(reactions, list)):
            return False
        counts = []
        for item in reactions:
            if not isinstance(item, dict) or not isinstance(item.get("type"), dict) or type(item.get("total_count")) is not int:
                return False
            counts.append({"type": item["type"], "count": item["total_count"]})
        try:
            observed_at = iso_utc(datetime.fromtimestamp(timestamp, tz=timezone.utc))
        except (OverflowError, OSError, ValueError):
            return False
        snapshot = json.dumps({"observed_at": observed_at, "counts": counts,
                               "total": sum(entry["count"] for entry in counts),
                               "source": "telegram_bot_api_message_reaction_count"}, ensure_ascii=False)
        with self.store.connect() as db:
            row = db.execute("SELECT id,reactions_json FROM satire_posts WHERE external_id=? AND status='published'",
                             (str(message_id),)).fetchone()
            if not row:
                return False
            old = json.loads(row["reactions_json"]) if row["reactions_json"] else None
            if old and old["observed_at"] > observed_at:
                return False
            db.execute("UPDATE satire_posts SET reactions_json=? WHERE id=?", (snapshot, row["id"]))
            return True


def review_label(post_id, text, stamp, version=1):
    when = f" · {local_label(stamp)}" if stamp else " · слот после согласования"
    return f"SMK_SATIRE {post_id} · версия {version}{when}\n\n{text}"
