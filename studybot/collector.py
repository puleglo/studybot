"""Юзербот на вашем аккаунте: только читает выбранные чаты, ничего не пишет
и не отмечает прочитанным."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaWebPage, PeerChannel, User

from . import config
from .storage import Storage

log = logging.getLogger(__name__)

DEVICE = dict(device_model="StudyBot", system_version="Linux", app_version="1.0")


def make_user_client(session: str = "") -> TelegramClient:
    tg = config.current()["telegram"]
    return TelegramClient(StringSession(session), int(tg["api_id"]), tg["api_hash"], **DEVICE)


def display_name(entity) -> str:
    if entity is None:
        return "?"
    if isinstance(entity, User):
        name = " ".join(x for x in (entity.first_name, entity.last_name) if x)
        return name or entity.username or str(entity.id)
    return getattr(entity, "title", None) or str(getattr(entity, "id", "?"))


def describe_media(msg) -> str | None:
    if not msg.media or isinstance(msg.media, MessageMediaWebPage):
        return None
    if msg.photo:
        return "фото"
    if msg.sticker:
        return "стикер"
    if msg.voice:
        return "голосовое"
    if msg.video_note:
        return "кружок"
    if msg.gif:
        return "gif"
    if msg.poll:
        q = msg.poll.poll.question
        return f"опрос: {getattr(q, 'text', q)}"
    if msg.geo:
        return "геопозиция"
    if msg.contact:
        return "контакт"
    if msg.video:
        return f"видео {msg.file.name or ''}".strip()
    if msg.audio:
        return f"аудио {msg.file.name or ''}".strip()
    if msg.document:
        return f"файл {msg.file.name or 'без имени' + (msg.file.ext or '')}"
    return "медиа"


def message_link(chat_id: int, msg_id: int, username: str | None = None) -> str | None:
    if username:
        return f"https://t.me/{username}/{msg_id}"
    real_id, peer_type = utils.resolve_id(chat_id)
    if peer_type is PeerChannel:
        return f"https://t.me/c/{real_id}/{msg_id}"
    return None


class Collector:
    def __init__(self, store: Storage, on_message: Callable[[object, dict], Awaitable[None]] | None = None):
        self.store = store
        self.on_message = on_message
        self.client: TelegramClient | None = None
        self.me = None
        self.chat_info: dict[int, dict] = {}  # id -> {"title", "username"}
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        self.client = make_user_client(config.current()["telegram"]["session"])
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("Сессия аккаунта недействительна — выполните `studybot login`")
        self.me = await self.client.get_me()
        self.client.add_event_handler(self._on_new, events.NewMessage())
        self.client.add_event_handler(self._on_edit, events.MessageEdited())
        log.info("Аккаунт: %s (id %s)", display_name(self.me), self.me.id)

    @staticmethod
    def enabled_ids() -> set[int]:
        return {int(c["id"]) for c in config.enabled_chats()}

    def chat_title(self, chat_id: int) -> str:
        info = self.chat_info.get(chat_id)
        if info:
            return info["title"]
        for c in config.current()["chats"]:
            if int(c["id"]) == chat_id:
                return c.get("title") or str(chat_id)
        return str(chat_id)

    def link(self, chat_id: int, msg_id: int) -> str | None:
        return message_link(chat_id, msg_id, (self.chat_info.get(chat_id) or {}).get("username"))

    # --- обработчики ---
    async def _on_new(self, event) -> None:
        if event.chat_id not in self.enabled_ids():
            return
        try:
            await self._ingest(event.message)
        except Exception:
            log.exception("Ошибка обработки сообщения")

    async def _on_edit(self, event) -> None:
        if event.chat_id not in self.enabled_ids():
            return
        self.store.update_text(event.chat_id, event.message.id, event.message.message or "")

    async def _ingest(self, msg) -> None:
        if getattr(msg, "action", None):  # служебные сообщения
            return
        try:
            sender = msg.sender or await msg.get_sender()
        except Exception:
            sender = None
        row = {
            "chat_id": msg.chat_id,
            "msg_id": msg.id,
            "date": int(msg.date.timestamp()),
            "sender_id": msg.sender_id,
            "sender": display_name(sender),
            "text": msg.message or "",
            "media": describe_media(msg),
            "reply_to": msg.reply_to_msg_id,
            "grouped_id": msg.grouped_id,
            "forwarded": 1 if msg.fwd_from else 0,
        }
        if self.store.add_message(row) and self.on_message:
            t = asyncio.create_task(self.on_message(msg, row))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)

    # --- догрузка пропущенного (после рестарта / нового чата) ---
    async def catch_up(self, only_ids: set[int] | None = None) -> None:
        tz = ZoneInfo(config.current()["summary"]["timezone"])
        midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        for cid in self.enabled_ids():
            if only_ids is not None and cid not in only_ids:
                continue
            try:
                last = self.store.last_msg_id(cid)
                if last:
                    it = self.client.iter_messages(cid, min_id=last, reverse=True, limit=3000)
                else:
                    it = self.client.iter_messages(cid, offset_date=midnight, reverse=True, limit=3000)
                n = 0
                async for msg in it:
                    await self._ingest(msg)
                    n += 1
                if n:
                    log.info("Догружено %d сообщений из «%s»", n, self.chat_title(cid))
            except Exception:
                log.exception("Не удалось догрузить чат %s", cid)

    async def dump_dialogs(self) -> None:
        """Список групп/каналов -> data/dialogs.json (для выбора чатов в панели).
        Заодно прогревает кэш сущностей Telethon."""
        out = []
        async for d in self.client.iter_dialogs():
            if not (d.is_group or d.is_channel):
                continue
            username = getattr(d.entity, "username", None)
            self.chat_info[d.id] = {"title": d.name, "username": username}
            out.append({
                "id": d.id,
                "title": d.name,
                "type": "группа" if d.is_group else "канал",
                "username": username,
            })
        config.DIALOGS_PATH.write_text(
            json.dumps({"updated": time.time(), "dialogs": out}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        # актуализируем названия чатов в конфиге
        titles = {d["id"]: d["title"] for d in out}

        def fix(cfg):
            for c in cfg["chats"]:
                if int(c["id"]) in titles:
                    c["title"] = titles[int(c["id"])]

        if any(int(c["id"]) in titles and c.get("title") != titles[int(c["id"])] for c in config.current()["chats"]):
            config.update(fix)
        log.info("Список диалогов обновлён: %d групп/каналов", len(out))
