"""Интерактивный вход в аккаунт Telegram (телефон → код → пароль 2FA)."""
from __future__ import annotations

import re
from getpass import getpass

from telethon import TelegramClient, errors
from telethon.sessions import StringSession

from .collector import DEVICE, display_name


def normalize_phone(raw: str) -> str:
    """9110890996 / 89110890996 / 7 911 089-09-96 / +7 (911) ... -> +79110890996"""
    raw = raw.strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+"):
        return "+" + digits
    if len(digits) == 10 and digits.startswith("9"):   # российский мобильный без кода страны
        return "+7" + digits
    if len(digits) == 11 and digits[0] in "78":         # 8XXXXXXXXXX или 7XXXXXXXXXX
        return "+7" + digits[1:]
    return "+" + digits


def ask_phone() -> str:
    while True:
        phone = normalize_phone(input("Номер телефона (+7...): "))
        if len(phone) >= 11:
            print(f"  → {phone}")
            return phone
        print("  Номер слишком короткий, введите с кодом страны, например +79991234567")


def ask_code() -> str:
    print("  Код пришёл в приложение Telegram (чат «Telegram» с галочкой), не по SMS.")
    return re.sub(r"\D", "", input("Код из Telegram: "))


async def interactive_login(api_id: int, api_hash: str) -> tuple[str, str, list[dict]]:
    """Возвращает (StringSession, имя аккаунта, список групп/каналов)."""
    client = TelegramClient(StringSession(), api_id, api_hash, **DEVICE)
    try:
        for attempt in range(3):
            try:
                await client.start(
                    phone=ask_phone,
                    code_callback=ask_code,
                    password=lambda: getpass("Облачный пароль (2FA): "),
                    max_attempts=3,
                )
                break
            except errors.PhoneNumberInvalidError:
                print("  Telegram не принял номер. Проверьте, что он с кодом страны.")
            except errors.PhoneNumberBannedError:
                raise RuntimeError("этот номер заблокирован в Telegram")
            except errors.FloodWaitError as e:
                raise RuntimeError(f"Telegram просит подождать {e.seconds // 60 + 1} мин перед новой попыткой")
            except errors.ApiIdInvalidError:
                raise RuntimeError("неверные api_id / api_hash — проверьте на my.telegram.org")
        else:
            raise RuntimeError("не удалось войти за 3 попытки")

        me = await client.get_me()
        dialogs = []
        async for d in client.iter_dialogs():
            if d.is_group or d.is_channel:
                dialogs.append({"id": d.id, "title": d.name, "type": "группа" if d.is_group else "канал"})
        return client.session.save(), display_name(me), dialogs
    finally:
        await client.disconnect()
