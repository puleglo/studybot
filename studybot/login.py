"""Интерактивный вход в аккаунт Telegram (телефон → код → пароль 2FA)."""
from __future__ import annotations

from getpass import getpass

from telethon import TelegramClient
from telethon.sessions import StringSession

from .collector import DEVICE, display_name


async def interactive_login(api_id: int, api_hash: str) -> tuple[str, str, list[dict]]:
    """Возвращает (StringSession, имя аккаунта, список групп/каналов)."""
    client = TelegramClient(StringSession(), api_id, api_hash, **DEVICE)
    await client.start(
        phone=lambda: input("Номер телефона (+7...): ").strip(),
        code_callback=lambda: input("Код из Telegram: ").strip(),
        password=lambda: getpass("Облачный пароль (2FA): "),
    )
    try:
        me = await client.get_me()
        dialogs = []
        async for d in client.iter_dialogs():
            if d.is_group or d.is_channel:
                dialogs.append({"id": d.id, "title": d.name, "type": "группа" if d.is_group else "канал"})
        return client.session.save(), display_name(me), dialogs
    finally:
        await client.disconnect()
