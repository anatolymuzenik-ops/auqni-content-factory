"""Read-only Telegram calendar view over the existing Pult SQLite and schedules."""

from __future__ import annotations

from datetime import datetime, time, timedelta

from pult_core import iso_utc, moscow_zone, utc_now


WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
STATUS = {
    "idea": "Идея", "needs_material": "Нужен материал",
    "preparing": "Готовится", "ready": "Готово, ждёт «Принято»",
    "approved": "Согласовано", "publishing": "Отправляется",
    "uncertain": "Отправка требует проверки", "published": "Опубликовано",
    "scheduled": "Запланировано",
}
MAX_MESSAGE = 3500


def _label(value):
    compact = " ".join((value or "").split())
    return compact[:87] + "…" if len(compact) > 90 else compact or "Без темы"


def _chunks(lines):
    output, current = [], ""
    for line in lines:
        piece = line + "\n"
        if current and len(current) + len(piece) > MAX_MESSAGE:
            output.append(current.rstrip())
            current = ""
        current += piece
    if current:
        output.append(current.rstrip())
    return output


def schedule_messages(store, main_schedule, satire_settings, days=7, free_only=False, now=None):
    """Use configured slots as the only source of free times; do not mutate state."""
    if days not in (7, 14):
        raise ValueError("Only 7 or 14 days are supported")
    local_now = (now or utc_now()).astimezone(moscow_zone())
    first = local_now.date()
    end = first + timedelta(days=days)
    start_utc = iso_utc(datetime.combine(first, time.min, moscow_zone()))
    end_utc = iso_utc(datetime.combine(end, time.min, moscow_zone()))
    satire_on = satire_settings.get("enabled", False)
    with store.connect() as db:
        main_rows = [dict(row) for row in db.execute("""SELECT i.id,i.title,c.status,c.scheduled_at
            FROM items i JOIN item_channels c ON c.item_id=i.id
            WHERE c.channel='telegram' AND c.selected=1 AND c.status!='rejected'
              AND c.scheduled_at>=? AND c.scheduled_at<? ORDER BY c.scheduled_at,i.id""",
            (start_utc, end_utc))]
        satire_rows = ([dict(row) for row in db.execute("""SELECT id,text,status,scheduled_at
            FROM satire_posts WHERE selected=1 AND status!='rejected'
              AND scheduled_at>=? AND scheduled_at<? ORDER BY scheduled_at,id""",
            (start_utc, end_utc))] if satire_on else [])
    main_at, satire_at = {}, {}
    for row in main_rows:
        main_at.setdefault(row["scheduled_at"], []).append(row)
    for row in satire_rows:
        satire_at.setdefault(row["scheduled_at"], []).append(row)

    main_total = main_busy = main_free = main_passed = 0
    satire_total = satire_busy = satire_free = satire_passed = 0
    day_lines = []
    for offset in range(days):
        day = first + timedelta(days=offset)
        entries = []
        expected_main = main_schedule.get(str(day.weekday()))
        expected_satire = (satire_settings.get("time", "08:30")
                           if day.weekday() in satire_settings.get("weekdays", [0, 1, 2, 3, 4]) else None)
        canonical_main = None
        if expected_main:
            hour, minute = map(int, expected_main.split(":"))
            slot = datetime.combine(day, time(hour, minute), moscow_zone())
            canonical_main = iso_utc(slot)
            main_total += 1
            rows = main_at.pop(canonical_main, [])
            if rows:
                main_busy += 1
                if not free_only:
                    for row in rows:
                        entries.append((expected_main, f"{expected_main} | Основной контент · №{row['id']}\n"
                                        f"«{_label(row['title'])}»\n{STATUS.get(row['status'], row['status'])}"))
            elif slot > local_now:
                main_free += 1
                entries.append((expected_main, f"{expected_main} | Основной контент\nСВОБОДНЫЙ СЛОТ"))
            else:
                main_passed += 1
                if not free_only:
                    entries.append((expected_main, f"{expected_main} | Основной контент\nВремя слота прошло"))
        if expected_satire:
            hour, minute = map(int, expected_satire.split(":"))
            slot = datetime.combine(day, time(hour, minute), moscow_zone())
            stamp = iso_utc(slot)
            if not satire_on:
                if not free_only:
                    entries.append((expected_satire, f"{expected_satire} | SMK_SATIRE\nПоток выключен"))
            else:
                satire_total += 1
                rows = satire_at.pop(stamp, [])
                if rows:
                    satire_busy += 1
                    if not free_only:
                        for row in rows:
                            entries.append((expected_satire, f"{expected_satire} | SMK_SATIRE · {row['id']}\n"
                                            f"«{_label(row['text'])}»\n{STATUS.get(row['status'], row['status'])}"))
                elif slot > local_now:
                    satire_free += 1
                    entries.append((expected_satire, f"{expected_satire} | SMK_SATIRE\nСВОБОДНЫЙ СЛОТ"))
                else:
                    satire_passed += 1
                    if not free_only:
                        entries.append((expected_satire, f"{expected_satire} | SMK_SATIRE\nВремя слота прошло"))
        # Existing manually moved publications are visible without inventing free off-grid slots.
        if not free_only:
            for stamp in list(main_at):
                slot = datetime.fromisoformat(stamp).astimezone(moscow_zone())
                if slot.date() == day:
                    for row in main_at.pop(stamp):
                        clock = slot.strftime("%H:%M")
                        entries.append((clock, f"{clock} | Основной контент · №{row['id']} (вне регулярного слота)\n"
                                               f"«{_label(row['title'])}»\n{STATUS.get(row['status'], row['status'])}"))
            for stamp in list(satire_at):
                slot = datetime.fromisoformat(stamp).astimezone(moscow_zone())
                if slot.date() == day:
                    for row in satire_at.pop(stamp):
                        clock = slot.strftime("%H:%M")
                        entries.append((clock, f"{clock} | SMK_SATIRE · {row['id']} (вне регулярного слота)\n"
                                               f"«{_label(row['text'])}»\n{STATUS.get(row['status'], row['status'])}"))
        if entries:
            day_lines.append(f"{WEEKDAYS[day.weekday()]} {day:%d.%m}")
            for _, body in sorted(entries, key=lambda entry: entry[0]):
                day_lines.extend((body, ""))

    title = "СВОБОДНЫЕ СЛОТЫ" if free_only else "РАСПИСАНИЕ AUQNI"
    lines = [f"{title} · {days} дней", f"{first:%d.%m}–{(end - timedelta(days=1)):%d.%m}", "",
             f"Основной контент: занято {main_busy} из {main_total}, свободно {main_free}" +
             (f", прошло {main_passed}" if main_passed else ""),
             (f"SMK_SATIRE: занято {satire_busy} из {satire_total}, свободно {satire_free}" +
              (f", прошло {satire_passed}" if satire_passed else "") if satire_on else
              "SMK_SATIRE: поток выключен"), ""]
    if day_lines:
        lines.extend(day_lines)
    else:
        lines.append("Свободных слотов нет." if free_only else "Публикационных слотов нет.")
    if free_only:
        lines.extend(("", f"Всего свободно: {main_free + satire_free}"))
    return _chunks(lines)
