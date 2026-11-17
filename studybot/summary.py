"""Сборка вечерней сводки."""
from __future__ import annotations

import html
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config, llm
from .storage import Storage

log = logging.getLogger(__name__)

MAX_DIRECT_CHARS = 120_000   # больше — режем на куски (map-reduce)
CHUNK_CHARS = 60_000

SYSTEM = """Ты — помощник студента. Тебе дают переписку из чата его учебной группы за период.
Составь краткую сводку на русском языке.

Формат — обычный текст без markdown (никаких *, #, **), можно эмодзи. Разделы:
❗ Важное — дедлайны, новые задания, переносы и отмены пар, изменения расписания, объявления старосты и преподавателей, требования к сдаче. Всегда указывай предмет и дату/срок, если они есть.
📚 По предметам — что обсуждали по каждому предмету, 1–2 строки на предмет.
📎 Скинули — какие файлы и материалы прислали (кто и что).
💬 Прочее — остальное в одну-две строки.

Правила: пропускай пустые разделы; ничего не выдумывай — только то, что есть в переписке;
не пересказывай флуд и мемы; если ничего важного не было — так и напиши одной строкой."""

NOTES_SYSTEM = """Это фрагмент переписки учебной группы. Выпиши сжатые, но полные заметки:
задания, дедлайны, изменения расписания, объявления, кто какие файлы прислал, важные обсуждения по предметам.
Флуд пропускай. Обычный текст, списком."""


def format_transcript(rows, tz: ZoneInfo) -> str:
    by_id = {r["msg_id"]: r for r in rows}
    lines = []
    for r in rows:
        t = datetime.fromtimestamp(r["date"], tz).strftime("%H:%M")
        parts = [f"[{t}] {r['sender']}"]
        if r["forwarded"]:
            parts.append("(переслано)")
        if r["reply_to"] and r["reply_to"] in by_id:
            ref = by_id[r["reply_to"]]
            snippet = (ref["text"] or ref["media"] or "")[:60].replace("\n", " ")
            parts.append(f"(в ответ {ref['sender']}: «{snippet}»)")
        body = (r["text"] or "").strip()
        if r["media"]:
            body = f"{body} [{r['media']}]".strip()
        if not body:
            continue
        lines.append(" ".join(parts) + ": " + body)
    return "\n".join(lines)


def _chunks(text: str, size: int) -> list[str]:
    out, cur, n = [], [], 0
    for line in text.split("\n"):
        if n + len(line) > size and cur:
            out.append("\n".join(cur))
            cur, n = [], 0
        cur.append(line[:size])
        n += len(line) + 1
    if cur:
        out.append("\n".join(cur))
    return out


async def summarize_chat(title: str, transcript: str) -> str:
    if len(transcript) > MAX_DIRECT_CHARS:
        notes = []
        for i, part in enumerate(_chunks(transcript, CHUNK_CHARS), 1):
            notes.append(await llm.chat([
                {"role": "system", "content": NOTES_SYSTEM},
                {"role": "user", "content": part},
            ]))
            log.info("«%s»: обработан фрагмент %d", title, i)
        transcript = "Заметки по частям переписки (переписка была длинной):\n\n" + "\n\n".join(notes)
    return await llm.chat([
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"Чат: {title}\n\n{transcript}"},
    ])


async def build(store: Storage, chat_titles: dict[int, str], ts_from: float, ts_to: float) -> str:
    """Возвращает HTML для Telegram (текст модели экранируется)."""
    cfg = config.current()
    tz = ZoneInfo(cfg["summary"]["timezone"])
    t_from = datetime.fromtimestamp(ts_from, tz)
    t_to = datetime.fromtimestamp(ts_to, tz)

    out = [f"📋 <b>Сводка за {t_to:%d.%m}</b>  <i>(с {t_from:%H:%M %d.%m} по {t_to:%H:%M})</i>"]
    for chat in config.enabled_chats(cfg):
        cid = int(chat["id"])
        title = chat_titles.get(cid) or chat.get("title") or str(cid)
        rows = store.messages_between(cid, ts_from, ts_to)
        transcript = format_transcript(rows, tz)
        if not transcript:
            out.append(f"\n<b>💬 {html.escape(title)}</b>\nСообщений не было.")
            continue
        try:
            text = await summarize_chat(title, transcript)
        except llm.LLMError as e:
            log.error("Сводка по «%s» не удалась: %s", title, e)
            text = f"⚠️ Не удалось получить сводку от модели: {e}"
        out.append(f"\n<b>💬 {html.escape(title)}</b>  <i>({len(rows)} сообщ.)</i>\n{html.escape(text)}")

    mats = store.materials_since(ts_from)
    if mats:
        lines = [f"• {html.escape(m['subject'])} / {html.escape(m['kind'])} — {html.escape(m['title'])}" for m in mats]
        out.append("\n🗂 <b>Разложено в Obsidian</b>\n" + "\n".join(lines))

    result = "\n".join(out)
    store.add_summary(ts_from, ts_to, result)
    return result


def split_message(text: str, limit: int = 4000) -> list[str]:
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # очень длинная строка (без тегов — их пишем только в заголовках)
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur.strip():
        parts.append(cur)
    return parts
