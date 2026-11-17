"""Бот, который присылает вам сводки и отвечает на пару команд (только владельцу)."""
from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING

from telethon import TelegramClient, events

from . import config
from .summary import split_message

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger(__name__)

HELP = (
    "🤖 <b>StudyBot</b>\n"
    "Каждый вечер присылаю сводку по чатам группы и раскладываю материалы по предметам в Obsidian.\n\n"
    "/summary — сводка с прошлой вечерней по сейчас\n"
    "/today — сводка с начала дня\n"
    "/materials — последние сохранённые материалы\n"
    "/status — состояние\n\n"
    "Настройки — через SSH-панель: <code>ssh vps -t studybot</code>"
)


class Bot:
    def __init__(self, app: "App"):
        self.app = app
        self.client: TelegramClient | None = None
        self.me = None

    async def start(self) -> None:
        tg = config.current()["telegram"]
        bot_id = tg["bot_token"].split(":")[0]
        session = str(config.DATA_DIR / f"bot_{bot_id}")
        self.client = TelegramClient(session, int(tg["api_id"]), tg["api_hash"])
        await self.client.start(bot_token=tg["bot_token"])
        self.client.parse_mode = "html"
        self.me = await self.client.get_me()
        self.client.add_event_handler(self._on_message, events.NewMessage(incoming=True, pattern=r"^/"))
        log.info("Бот: @%s", self.me.username)

    @staticmethod
    def owner_id() -> int:
        return int(config.current()["telegram"].get("owner_id") or 0)

    async def send(self, text: str) -> None:
        owner = self.owner_id()
        if not owner or not self.client:
            log.warning("Некому отправлять: owner_id не задан")
            return
        for part in split_message(text):
            try:
                await self.client.send_message(owner, part, link_preview=False)
            except Exception as e:
                log.error("Бот не может написать владельцу (%s) — нажмите /start в чате с ботом", e)
                return

    async def _on_message(self, event) -> None:
        if not event.is_private:
            return
        cmd = event.raw_text.split()[0].split("@")[0].lower()
        if event.sender_id != self.owner_id():
            if cmd == "/start":
                await event.reply("Это приватный бот.")
            return
        try:
            if cmd in ("/start", "/help"):
                await event.reply(HELP)
            elif cmd == "/summary":
                await event.reply("⏳ Готовлю сводку…")
                await self.app.send_summary("manual")
            elif cmd == "/today":
                await event.reply("⏳ Готовлю сводку с начала дня…")
                await self.app.send_summary("today")
            elif cmd == "/status":
                await event.reply(self.app.status_text())
            elif cmd == "/materials":
                rows = self.app.store.last_materials(15)
                if not rows:
                    await event.reply("Пока ничего не сохранено.")
                else:
                    lines = [
                        f"• {html.escape(r['subject'])} / {html.escape(r['kind'])} — {html.escape(r['title'])}"
                        for r in rows
                    ]
                    await event.reply("🗂 <b>Последние материалы</b>\n" + "\n".join(lines))
            else:
                await event.reply("Не знаю такой команды. /help")
        except Exception as e:
            log.exception("Ошибка команды %s", cmd)
            await event.reply(f"⚠️ Ошибка: {html.escape(str(e))}")
