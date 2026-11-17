"""Распознавание учебных материалов и раскладка по предметам:
  <Учёба>/<Предмет>/<Тип>/<дата> <файл>  +  заметка .md с описанием и ссылкой на сообщение."""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from . import config, llm
from .livesync import LiveSync, LiveSyncError
from .storage import Storage

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger(__name__)

KINDS = ["Задания", "Решения", "Отчёты", "Лекции и методички", "Прочее"]
NO_SUBJECT = "Без предмета"
KEYWORDS = re.compile(
    r"(задани|лаб|л/р|дз|д/з|домашк|дедлайн|сдать|сдач|срок|отч[её]т|курсов|реферат|эссе|контрольн|"
    r"экзамен|зач[её]т|вариант|решени|ответ|билет|методич|лекци|практик|семинар|тест|кр\b|ргр|доклад|презентац)",
    re.I,
)

CLASSIFY_SYSTEM = """Ты сортируешь учебные материалы из чата студенческой группы.
Дано: контекст (предыдущие сообщения) и целевое сообщение. Определи, является ли целевое сообщение
учебным материалом: условие задания/лабы/ДЗ, объявление о задании с дедлайном, методичка, лекция/конспект,
отчёт, решение или ответы, которые скинули одногруппники, билеты, примеры работ.
НЕ материал: мемы, бытовые фото, скриншоты переписок не по учёбе, болтовня, организационный флуд.

Верни строго JSON:
{"is_material": true/false,
 "subject": "предмет",
 "kind": "Задания" | "Решения" | "Отчёты" | "Лекции и методички" | "Прочее",
 "title": "короткое название для файла, до 60 символов, без даты",
 "summary": "1–2 предложения: что это",
 "deadline": "срок сдачи как в тексте, или null"}

subject: выбери из известных предметов: {subjects}.
Если ни один не подходит, но предмет понятен — дай короткое название, как в расписании, с большой буквы.
Если предмет понять нельзя — "Без предмета"."""

_BAD_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]')


def safe_name(s: str, limit: int = 90) -> str:
    s = _BAD_CHARS.sub("_", s or "")
    s = re.sub(r"\s+", " ", s).strip().strip(".")
    if len(s) > limit:
        stem, dot, ext = s.rpartition(".")
        s = (stem[: limit - len(ext) - 1] + "." + ext) if dot and len(ext) <= 8 else s[:limit]
    return s or "без названия"


class Sink:
    """Куда класть файлы: CouchDB (LiveSync) и/или локальная папка data/materials."""

    def __init__(self, notify):
        self.notify = notify
        self._ls: LiveSync | None = None
        self._ls_sig = None
        self._warned = False

    def livesync(self) -> LiveSync | None:
        cc = config.current()["couchdb"]
        if not cc["enabled"]:
            return None
        sig = json.dumps(cc, sort_keys=True)
        if sig != self._ls_sig:
            if self._ls:
                asyncio.create_task(self._ls.close())
            self._ls, self._ls_sig, self._warned = LiveSync(cc), sig, False
        return self._ls

    async def close(self) -> None:
        if self._ls:
            await self._ls.close()

    @staticmethod
    def local_path(path: str) -> Path:
        return config.MATERIALS_DIR / Path(*path.split("/"))

    async def exists(self, path: str) -> bool:
        if self.local_path(path).exists():
            return True
        ls = self.livesync()
        if ls:
            try:
                return await ls.exists(path)
            except Exception:
                return False
        return False

    async def unique(self, folder: str, name: str) -> str:
        stem, dot, ext = name.rpartition(".")
        if not dot or len(ext) > 8:
            stem, ext = name, ""
        for i in range(1, 200):
            cand = name if i == 1 else (f"{stem} ({i}).{ext}" if ext else f"{stem} ({i})")
            path = f"{folder}/{cand}"
            if not await self.exists(path):
                return path
        raise RuntimeError("не удалось подобрать имя файла")

    async def write(self, path: str, data: bytes, overwrite: bool = False) -> None:
        cfg = config.current()
        couch_ok = False
        ls = self.livesync()
        if ls:
            try:
                await ls.write(path, data, overwrite=overwrite)
                couch_ok = True
            except Exception as e:
                log.error("CouchDB: не удалось записать %s: %s", path, e)
                if not self._warned:
                    self._warned = True
                    await self.notify(f"⚠️ Не получилось записать в Obsidian (CouchDB): {e}\nСохраняю локально на сервере.")
        if cfg["materials"]["save_local"] or not couch_ok:
            p = self.local_path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)


class Materials:
    def __init__(self, store: Storage, sink: Sink, app: "App"):
        self.store = store
        self.sink = sink
        self.app = app
        self.sem = asyncio.Semaphore(2)
        self._verdicts: dict[tuple, asyncio.Task] = {}
        self._groups: dict[tuple, dict] = {}   # альбомы: одна заметка на весь альбом

    @staticmethod
    def has_file(msg) -> bool:
        if msg.photo:
            return bool(config.current()["materials"]["photos"])
        if msg.document and not (msg.sticker or msg.voice or msg.video_note or msg.gif or msg.audio):
            return True
        return False

    async def handle(self, msg, row: dict) -> None:
        cfg = config.current()
        if not cfg["materials"]["enabled"]:
            return
        has_file = self.has_file(msg)
        text = row["text"] or ""
        if not has_file and not (len(text) >= 25 and KEYWORDS.search(text)):
            return
        if self.store.material_exists(row["chat_id"], row["msg_id"]):
            return
        try:
            await asyncio.sleep(4)  # дать долететь остальным фото альбома и подписи
            async with self.sem:
                verdict = await self._verdict(row)
                if not verdict.get("is_material"):
                    return
                await self._save(msg, row, verdict, has_file)
        except llm.LLMError as e:
            log.error("Классификация не удалась: %s", e)
            self.app.record_error(f"классификация: {e}")
        except Exception as e:
            log.exception("Ошибка сохранения материала")
            self.app.record_error(f"материал: {e}")

    # --- классификация ---
    async def _verdict(self, row: dict) -> dict:
        key = ("g", row["chat_id"], row["grouped_id"]) if row["grouped_id"] else ("m", row["chat_id"], row["msg_id"])
        task = self._verdicts.get(key)
        if task is None:
            task = asyncio.create_task(self._classify(row))
            self._verdicts[key] = task
            if len(self._verdicts) > 300:
                for k in list(self._verdicts)[:100]:
                    self._verdicts.pop(k, None)
        return await task

    def _fmt(self, r) -> str:
        tz = ZoneInfo(config.current()["summary"]["timezone"])
        t = datetime.fromtimestamp(r["date"], tz).strftime("%d.%m %H:%M")
        body = (r["text"] or "").strip()
        if r["media"]:
            body = f"{body} [{r['media']}]".strip()
        return f"[{t}] {r['sender']}: {body}"

    async def _classify(self, row: dict) -> dict:
        cfg = config.current()
        target = [row]
        first_id = row["msg_id"]
        if row["grouped_id"]:
            target = self.store.group_messages(row["chat_id"], row["grouped_id"]) or [row]
            first_id = target[0]["msg_id"]
        context = self.store.context_before(row["chat_id"], first_id, 12)
        subjects = ", ".join(cfg["subjects"]) or "(список пуст)"
        user = (
            f"Чат: {self.app.collector.chat_title(row['chat_id'])}\n\n"
            "Контекст:\n" + ("\n".join(self._fmt(r) for r in context) or "(нет)") +
            "\n\nЦелевое сообщение" + (" (альбом)" if len(target) > 1 else "") + ":\n" +
            "\n".join(self._fmt(r) for r in target)
        )
        v = await llm.chat_json([
            {"role": "system", "content": CLASSIFY_SYSTEM.replace("{subjects}", subjects)},
            {"role": "user", "content": user},
        ], temperature=0.1, max_tokens=400)
        v["subject"] = self._normalize_subject(str(v.get("subject") or NO_SUBJECT))
        v["kind"] = v.get("kind") if v.get("kind") in KINDS else "Прочее"
        v["title"] = str(v.get("title") or "Материал")[:80]
        log.info("Классификация %s/%s: %s", row["chat_id"], row["msg_id"],
                 f"{v['subject']} / {v['kind']} — {v['title']}" if v.get("is_material") else "не материал")
        return v

    def _normalize_subject(self, name: str) -> str:
        name = re.sub(r"\s+", " ", name).strip().strip(".") or NO_SUBJECT
        if name.lower() in ("без предмета", "неизвестно", "unknown", "none", "null"):
            return NO_SUBJECT
        subjects = config.current()["subjects"]
        for s in subjects:
            if s.lower() == name.lower():
                return s
        close = difflib.get_close_matches(name.lower(), [s.lower() for s in subjects], n=1, cutoff=0.85)
        if close:
            return next(s for s in subjects if s.lower() == close[0])
        # новый предмет — запоминаем, в панели можно переименовать/удалить
        config.update(lambda c: c["subjects"].append(name) if name not in c["subjects"] else None)
        log.info("Новый предмет: %s", name)
        return name

    # --- сохранение ---
    async def _save(self, msg, row: dict, v: dict, has_file: bool) -> None:
        cfg = config.current()
        tz = ZoneInfo(cfg["summary"]["timezone"])
        date = datetime.fromtimestamp(row["date"], tz).strftime("%Y-%m-%d")
        base = cfg["couchdb"]["base_folder"].strip("/") or "Учёба"
        folder = f"{base}/{safe_name(v['subject'])}/{v['kind']}"

        embed = None
        if has_file:
            size = msg.file.size or 0
            max_bytes = int(cfg["materials"]["max_file_mb"] * 1024 * 1024)
            if size > max_bytes:
                embed = f"> [!warning] Файл «{msg.file.name or 'файл'}» ({size / 1048576:.1f} МБ) больше лимита — не скачан, смотри по ссылке."
            else:
                orig = msg.file.name or (f"фото {msg.id}.jpg" if msg.photo else f"файл {msg.id}{msg.file.ext or ''}")
                file_path = await self.sink.unique(folder, f"{date} {safe_name(orig)}")
                data = await msg.download_media(file=bytes)
                await self.sink.write(file_path, data)
                embed = f"![[{file_path}]]"

        key = ("g", row["chat_id"], row["grouped_id"]) if row["grouped_id"] else ("m", row["chat_id"], row["msg_id"])
        group = self._groups.setdefault(key, {"lock": asyncio.Lock(), "path": None, "embeds": []})
        async with group["lock"]:
            if embed:
                group["embeds"].append(embed)
            if group["path"] is None:
                group["path"] = await self.sink.unique(folder, f"{date} {safe_name(v['title'], 70)}.md")
            note = self._note(row, v, date, group["embeds"])
            await self.sink.write(group["path"], note.encode("utf-8"), overwrite=True)
        if len(self._groups) > 300:
            for k in list(self._groups)[:100]:
                self._groups.pop(k, None)
        self.store.add_material(row["chat_id"], row["msg_id"], v["subject"], v["kind"], v["title"], group["path"])
        log.info("Сохранено: %s", group["path"])

    def _note(self, row: dict, v: dict, date: str, embeds: list[str]) -> str:
        chat = self.app.collector.chat_title(row["chat_id"])
        link = self.app.collector.link(row["chat_id"], row["msg_id"])
        texts = [row["text"] or ""]
        if row["grouped_id"]:
            texts = [r["text"] for r in self.store.group_messages(row["chat_id"], row["grouped_id"]) if r["text"]]

        def y(s) -> str:  # безопасное значение для YAML
            return json.dumps(str(s), ensure_ascii=False)

        fm = [
            "---",
            f"предмет: {y(v['subject'])}",
            f"тип: {y(v['kind'])}",
            f"дата: {date}",
            f"от: {y(row['sender'])}",
            f"чат: {y(chat)}",
        ]
        if v.get("deadline"):
            fm.append(f"дедлайн: {y(v['deadline'])}")
        if link:
            fm.append(f"источник: {link}")
        fm += ["tags: [учеба]", "---", ""]
        body = [f"# {v['title']}", ""]
        if v.get("summary"):
            body += [str(v["summary"]), ""]
        if v.get("deadline"):
            body += [f"**Срок:** {v['deadline']}", ""]
        quoted = "\n".join(t for t in texts if t).strip()
        if quoted:
            body += ["\n".join("> " + line for line in quoted.splitlines()), ""]
        body += [f"— {row['sender']}, {chat}" + (f" · [сообщение]({link})" if link else ""), ""]
        body += embeds
        return "\n".join(fm + body) + "\n"
