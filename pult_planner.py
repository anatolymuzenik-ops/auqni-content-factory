"""Daily editorial planning for the existing Pult queue. Never approves or publishes."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile

from pult_core import PultError, iso_utc, moscow_zone, utc_now


def upcoming_slots(schedule, occupied, now, min_lead_hours, horizon_days):
    local_now = now.astimezone(moscow_zone())
    earliest = local_now + timedelta(hours=min_lead_hours)
    latest = local_now + timedelta(days=horizon_days)
    occupied = set(occupied)
    slots = []
    for offset in range(horizon_days + 1):
        day = (local_now + timedelta(days=offset)).date()
        clock = schedule.get(str(day.weekday()))
        if not clock:
            continue
        hour, minute = map(int, clock.split(":"))
        slot = datetime(day.year, day.month, day.day, hour, minute, tzinfo=moscow_zone())
        value = iso_utc(slot)
        if earliest <= slot <= latest and value not in occupied:
            slots.append(value)
    return slots


def collect_evidence(root, per_source=3):
    """Refresh the existing monitor; only read article text or abstracts as evidence."""
    import sys
    monitor_path = root / "monitoring"
    if str(monitor_path) not in sys.path:
        sys.path.insert(0, str(monitor_path))
    import check
    import editorial

    errors = []
    if check.run():
        errors.append("Некоторые источники мониторинга временно недоступны")
    rows = []
    with sqlite3.connect(check.DATABASE) as db:
        db.row_factory = sqlite3.Row
        for source in ("nqi", "who", "pubmed"):
            rows.extend(dict(row) for row in db.execute(
                "SELECT source_id,external_id,url,title,published_at FROM materials "
                "WHERE source_id=? ORDER BY first_seen_at DESC LIMIT ?", (source, per_source)))
    evidence = []
    pubmed = [row for row in rows if row["source_id"] == "pubmed"]
    abstracts = {}
    if pubmed:
        try:
            abstracts = editorial.get_pubmed_content(pubmed)
        except Exception as exc:
            errors.append(f"PubMed abstract: {type(exc).__name__}")
    for row in rows:
        try:
            if row["source_id"] == "pubmed":
                body = abstracts.get(row["external_id"], "")
                coverage = "abstract"
            else:
                body, coverage = editorial.get_html_content(row)
            body = " ".join(body.split())[:4500]
            if len(body) < 120:
                continue
            evidence.append({"id": f"{row['source_id']}:{row['external_id']}",
                             "title": row["title"], "url": row["url"],
                             "published_at": row["published_at"],
                             "coverage": coverage, "text": body})
        except Exception as exc:
            errors.append(f"{row['source_id']} material: {type(exc).__name__}")
    return evidence, errors


def validate_candidates(answer, evidence, history, limit):
    if not isinstance(answer, dict) or not isinstance(answer.get("candidates"), list):
        raise PultError("Редакционный планировщик вернул неверный формат")
    known = {entry["id"] for entry in evidence}
    used = {re.sub(r"\W+", " ", row["title"].casefold()).strip() for row in history}
    output = []
    for entry in answer["candidates"]:
        if not isinstance(entry, dict):
            continue
        topic = entry.get("topic")
        problem = entry.get("problem")
        angle = entry.get("angle")
        stream = entry.get("stream")
        ids = entry.get("evidence_ids", [])
        if not all(isinstance(x, str) and 12 <= len(x.strip()) <= 180 for x in (topic, problem, angle)):
            continue
        if stream not in {"smk_problem", "smk_practice", "auqni_work", "external_environment"}:
            continue
        if not isinstance(ids, list) or any(not isinstance(x, str) or x not in known for x in ids):
            continue
        if stream == "external_environment" and not ids:
            continue
        key = re.sub(r"\W+", " ", topic.casefold()).strip()
        if key in used or re.search(r"\bCAPA\b", topic + problem + angle, re.I):
            continue
        used.add(key)
        output.append({"topic": topic.strip(), "problem": problem.strip(),
                       "angle": angle.strip(), "stream": stream,
                       "evidence_ids": list(dict.fromkeys(ids))})
        if len(output) >= limit:
            break
    return output


def archive_topics(root):
    """Include material created before the Pult database in duplicate checks."""
    found = []
    for path in sorted((root / "posts").glob("*-content.json"), reverse=True)[:100]:
        try:
            doc = json.loads(path.read_text(encoding="utf-8-sig"))
            title = doc.get("editorial_brief", {}).get("topic")
            if isinstance(title, str) and title.strip():
                found.append({"id": path.name, "title": title.strip(), "status": "archive"})
        except (OSError, ValueError, TypeError):
            continue
    return found


class EditorialPlanner:
    def __init__(self, root, workspace, command):
        self.root = Path(root).resolve()
        self.workspace = Path(workspace).resolve()
        self.command = command

    def propose(self, slots, history):
        history = history + archive_topics(self.root)
        evidence, errors = collect_evidence(self.root)
        # Short stable labels for this planning run avoid copying long URLs into JSON IDs.
        for number, entry in enumerate(evidence, 1):
            entry["source_key"] = entry["id"]
            entry["id"] = f"S{number}"
        registry = self.root.parent / "Экосистема Аукни" / "Внешняя среда AUQNI" / "registry.json"
        leads = []
        if registry.is_file():
            objects = json.loads(registry.read_text(encoding="utf-8-sig")).get("objects", [])
            leads = [{"name": x.get("name"), "url": x.get("official_resource")}
                     for x in objects if x.get("official_resource")][:25]
        prior = [{"id": x["id"], "topic": x["title"], "status": x["status"]} for x in history]
        prompt = (
            "Ты редакционный планировщик существующего Контент-завода AUQNI. "
            "Предложи темы для указанных свободных слотов, не создавая посты, изображения и не публикуя. "
            "Прочти docs/editorial-policy-v1.md в проекте, а также корневые docs/AUQNI_KNOWLEDGE_SOURCE.md "
            "и docs/AUQNI_COMMUNICATIONS_SOURCE.md. Используй четыре направления политики. "
            "Учитывай историю, разнообразие тем и практическую ценность для медорганизаций. "
            "Источники ниже с прочитанными фрагментами можно использовать как доказательства; "
            "реестр внешней среды содержит только направления поиска и сам по себе не доказывает фактов. "
            "Перед выбором тем изучи прочитанные материалы мониторинга и, если доступен веб-поиск, "
            "проверь актуальные профессиональные публикации и обсуждения по релевантным направлениям реестра. "
            "В ответе указывай только короткие evidence_ids вида S1, S2 из приложенных прочитанных "
            "фрагментов, копируя их точно; новые находки Writer проверит отдельно. "
            "Не утверждай, что сообщество часто обсуждает вопрос, по одному источнику. "
            "Не придумывай функции AUQNI, требования, цифры и преимущества. "
            "Не используй термин CAPA для соцсетей. Не заполняй слоты слабыми темами. "
            "Верни только JSON: {\"candidates\":[{\"topic\":\"...\",\"problem\":\"...\","
            "\"angle\":\"...\",\"stream\":\"smk_problem|smk_practice|auqni_work|external_environment\","
            "\"evidence_ids\":[\"...\"]}]}. Число тем не больше числа слотов.\n"
            f"Проект: {self.root}\nСвободные слоты: {json.dumps(slots, ensure_ascii=False)}\n"
            f"История: {json.dumps(prior, ensure_ascii=False)}\n"
            f"Прочитанные материалы: {json.dumps(evidence, ensure_ascii=False)}\n"
            f"Источники для дальнейшего поиска: {json.dumps(leads, ensure_ascii=False)}\n"
            f"Сбои источников: {json.dumps(errors, ensure_ascii=False)}\n"
        )
        with tempfile.TemporaryDirectory(prefix="auqni-plan-") as temp:
            result = Path(temp) / "plan.json"
            args = [part.format(workspace_root=self.workspace, project_root=self.root, result_path=result)
                    for part in self.command]
            env = os.environ.copy()
            for name in ("TELEGRAM_BOT_TOKEN", "AUQNI_OWNER_USER_ID", "OPENAI_API_KEY"):
                env.pop(name, None)
            try:
                proc = subprocess.run(args, input=prompt, text=True, cwd=self.workspace,
                                      env=env, capture_output=True, timeout=900, check=False)
                if proc.returncode:
                    raise PultError("Редакционный планировщик не завершил выбор тем")
                answer = json.loads(result.read_text(encoding="utf-8"))
            except (OSError, subprocess.TimeoutExpired, ValueError):
                raise PultError("Редакционный планировщик не вернул список тем") from None
        return validate_candidates(answer, evidence, history, len(slots)), evidence, errors


def candidate_inputs(candidate, evidence):
    selected = [item for item in evidence if item["id"] in candidate["evidence_ids"]]
    task = (f"Тема: {candidate['topic']}. Проблема аудитории: {candidate['problem']}. "
            f"Практический ракурс: {candidate['angle']}. Направление: {candidate['stream']}. "
            "Подготовь профессиональный пост для медицинских организаций по редакционной политике. "
            "Проверь реальные возможности AUQNI по внутренним материалам. "
            "Прочитанные внешние материалы ниже используй только для тех тезисов, которые они действительно подтверждают. "
            "При необходимости проверь дополнительные источники. "
            "Не используй термин CAPA в социальных сетях. Не заявляй превосходство без доказательств. "
            "Если источник один, не говори, что вся профессиональная среда часто обсуждает тему.")
    return {"text": task, "planner": {"topic": candidate["topic"],
            "stream": candidate["stream"], "evidence": selected}}
