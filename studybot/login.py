"""Интерактивный вход в аккаунт Telegram (телефон → код → пароль 2FA).
Показывает, куда Telegram отправил код, и даёт запросить его повторно другим способом."""
from __future__ import annotations

import re
from getpass import getpass

from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.tl import functions

from .collector import DEVICE, display_name

CODE_TYPES = {
    "SentCodeTypeApp": "в приложение Telegram — чат «Telegram» (с синей галочкой) на ваших устройствах",
    "SentCodeTypeSms": "по SMS",
    "SentCodeTypeCall": "голосовым звонком",
    "SentCodeTypeFlashCall": "звонком-сбросом (код — последние цифры номера звонящего)",
    "SentCodeTypeMissedCall": "пропущенным звонком (код — последние цифры номера звонящего)",
    "SentCodeTypeEmailCode": "на почту, привязанную к аккаунту для входа",
    "SentCodeTypeFragmentSms": "через Fragment (анонимный номер) — смотрите на fragment.com",
    "SentCodeTypeFirebaseSms": "по SMS",
    "SentCodeTypeSmsWord": "по SMS (одно слово)",
    "SentCodeTypeSmsPhrase": "по SMS (фраза)",
    "CodeTypeSms": "по SMS",
    "CodeTypeCall": "голосовым звонком",
    "CodeTypeFlashCall": "звонком-сбросом",
    "CodeTypeMissedCall": "пропущенным звонком",
    "CodeTypeFragmentSms": "через Fragment",
}


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


def describe(sent) -> tuple[str, bool]:
    """Текст «куда отправлен код» и можно ли запросить другим способом."""
    name = type(sent.type).__name__
    if name == "SentCodeTypeSetUpEmailRequired":
        raise RuntimeError(
            "Telegram требует привязать почту для входа. Откройте Telegram → Настройки → "
            "Конфиденциальность → Почта для входа, привяжите её и запустите вход снова."
        )
    where = CODE_TYPES.get(name, name)
    nxt = getattr(sent, "next_type", None)
    text = f"  Код отправлен {where}."
    if nxt is not None:
        text += f"\n  Если не пришёл — нажмите Enter, и Telegram пришлёт его {CODE_TYPES.get(type(nxt).__name__, 'другим способом')}"
        if getattr(sent, "timeout", None):
            text += f" (можно через {sent.timeout} с)"
        text += "."
    return text, nxt is not None


def print_qr(url: str) -> None:
    import qrcode
    q = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
    q.add_data(url)
    q.make(fit=True)
    q.print_ascii(invert=True)


async def password_step(client: TelegramClient) -> None:
    for _ in range(3):
        try:
            await client.sign_in(password=getpass("Облачный пароль (2FA): "))
            return
        except errors.PasswordHashInvalidError:
            print("  Неверный пароль.")
    raise RuntimeError("неверный облачный пароль 3 раза")


async def qr_login(client: TelegramClient) -> None:
    import asyncio
    from datetime import datetime, timezone

    print("\n  На телефоне: Telegram → Настройки → Устройства → «Подключить устройство»")
    print("  и наведите камеру на QR-код ниже. Если окно узкое — разверните его.\n")
    qr = await client.qr_login()
    for _ in range(10):  # ~5 минут: QR живёт ~30 с, потом обновляется
        print_qr(qr.url)
        left = max(5, int((qr.expires - datetime.now(timezone.utc)).total_seconds()))
        print(f"  Жду сканирования ({left} с)…")
        try:
            await qr.wait(timeout=left)
            return
        except asyncio.TimeoutError:
            print("\n  QR устарел, вот новый:\n")
            await qr.recreate()
        except errors.SessionPasswordNeededError:
            await password_step(client)
            return
    raise RuntimeError("QR так и не отсканировали")


async def interactive_login(api_id: int, api_hash: str) -> tuple[str, str, list[dict]]:
    """Возвращает (StringSession, имя аккаунта, список групп/каналов)."""
    client = TelegramClient(StringSession(), api_id, api_hash, **DEVICE)
    await client.connect()
    try:
        print("  Способ входа:\n    1) QR-код — сканируете телефоном, код не нужен (рекомендуется)\n    2) Код по номеру телефона")
        if (input("  Выбор [1/2]: ").strip() or "1") != "2":
            await qr_login(client)
            return await _finish(client)

        # --- номер ---
        while True:
            phone = ask_phone()
            try:
                sent = await client.send_code_request(phone)
                break
            except errors.PhoneNumberInvalidError:
                print("  Telegram не принял номер. Проверьте, что он с кодом страны.")
            except errors.PhoneNumberBannedError:
                raise RuntimeError("этот номер заблокирован в Telegram")
            except errors.ApiIdInvalidError:
                raise RuntimeError("неверные api_id / api_hash — проверьте на my.telegram.org")
            except errors.FloodWaitError as e:
                raise RuntimeError(f"Telegram просит подождать {e.seconds // 60 + 1} мин перед новой попыткой")

        # --- код ---
        text, can_resend = describe(sent)
        print(text)
        tries = 0
        while True:
            code = re.sub(r"\D", "", input("Код (Enter — отправить заново): "))
            if not code:
                try:
                    sent = await client(functions.auth.ResendCodeRequest(phone, sent.phone_code_hash))
                    print(describe(sent)[0])
                except errors.SendCodeUnavailableError:
                    print("  Других способов доставки больше нет. Подождите пару минут и запустите вход заново.")
                except errors.FloodWaitError as e:
                    print(f"  Слишком часто. Подождите {e.seconds} с.")
                except errors.PhoneCodeExpiredError:
                    sent = await client.send_code_request(phone, force_sms=False)
                    print(describe(sent)[0])
                continue
            try:
                await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
                break
            except errors.SessionPasswordNeededError:
                await password_step(client)
                break
            except errors.PhoneCodeInvalidError:
                tries += 1
                if tries >= 5:
                    raise RuntimeError("неверный код 5 раз")
                print("  Неверный код, попробуйте ещё раз.")
            except errors.PhoneCodeExpiredError:
                print("  Код устарел — отправляю новый.")
                sent = await client.send_code_request(phone)
                print(describe(sent)[0])

        return await _finish(client)
    finally:
        await client.disconnect()


async def _finish(client: TelegramClient) -> tuple[str, str, list[dict]]:
    me = await client.get_me()
    print(f"  ✓ Вошли как {display_name(me)}")
    dialogs = []
    async for d in client.iter_dialogs():
        if d.is_group or d.is_channel:
            dialogs.append({"id": d.id, "title": d.name, "type": "группа" if d.is_group else "канал"})
    return client.session.save(), display_name(me), dialogs
