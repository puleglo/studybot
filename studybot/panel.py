"""Панель управления в терминале. Доступна только через SSH — сетевых портов нет.
Изменения пишутся в config.json, демон подхватывает их на лету; для действий,
которые нужно выполнить в демоне, кладётся файл-флаг в data/control/."""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import secrets
import subprocess
import time
from datetime import datetime
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from . import config, llm
from .livesync import LiveSync

console = Console()

# Обозначение клавиши в подсказках: [s], [e], [0] и т.п.
_KEY_RE = re.compile(r"\[([^\]]+)]")


# ---------- помощники ----------
def control(cmd: str) -> None:
    config.CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    (config.CONTROL_DIR / cmd).touch()


def daemon_status() -> tuple[bool, dict]:
    try:
        st = json.loads(config.STATUS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return False, {}
    return time.time() - st.get("heartbeat", 0) < 60, st


def systemd_state() -> str:
    try:
        return subprocess.run(["systemctl", "is-active", "studybot"], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return "?"


def pause() -> None:
    Prompt.ask("[dim]Enter — назад[/dim]", default="", show_default=False)


def hint(text: str) -> None:
    """Печать строки подсказки с клавишами.

    rich считает [s], [e], [a], [d], [c], [k] и другие одиночные буквы в
    скобках тегами стилей (зачёркивание, курсив и т.п.) и молча вырезает их,
    из-за чего в подменю оставались только [0] и [1]. Оформляем каждую
    клавишу явно, чтобы она всегда была видна.
    """
    console.print(_KEY_RE.sub(lambda m: f"[bold cyan]\\[{m.group(1)}][/bold cyan]", text))


def parse_indexes(s: str, n: int) -> list[int]:
    out = []
    for part in s.replace(" ", "").split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            out += list(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return [i - 1 for i in out if 1 <= i <= n]


def header() -> None:
    cfg = config.current()
    alive, st = daemon_status()
    state = "[green]● работает[/green]" if alive else f"[red]● не отвечает[/red] (systemd: {systemd_state()})"
    name = cfg["llm"]["active"]
    prov = cfg["llm"]["providers"].get(name, {})
    problems = config.validate(cfg)
    lines = [
        f"Демон: {state}" + (f"   аккаунт: {st.get('account')}   бот: @{st.get('bot')}" if alive else ""),
        f"LLM: [bold]{name}[/bold] / {prov.get('model', '—')}   "
        f"Чатов: {len(config.enabled_chats(cfg))}/{len(cfg['chats'])}   "
        f"Сводка: {cfg['summary']['time'] if cfg['summary']['enabled'] else 'выкл'}   "
        f"Obsidian: {'вкл' if cfg['couchdb']['enabled'] else 'выкл'}",
    ]
    if problems:
        lines.append("[yellow]Не настроено: " + "; ".join(problems) + "[/yellow]")
    if st.get("errors"):
        lines.append("[red]Ошибки:[/red] " + " | ".join(st["errors"][-2:]))
    console.print(Panel("\n".join(lines), title="StudyBot", border_style="cyan"))


# ---------- чаты ----------
def load_dialogs(max_age: int = 600) -> list[dict]:
    def read():
        try:
            return json.loads(config.DIALOGS_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {"updated": 0, "dialogs": []}

    data = read()
    if time.time() - data["updated"] > max_age and daemon_status()[0]:
        control("refresh_dialogs")
        with console.status("Запрашиваю список чатов у демона…"):
            for _ in range(30):
                time.sleep(1)
                new = read()
                if new["updated"] > data["updated"]:
                    data = new
                    break
    return data["dialogs"]


def menu_chats() -> None:
    while True:
        console.clear()
        cfg = config.current()
        t = Table(title="Отслеживаемые чаты")
        t.add_column("#", justify="right")
        t.add_column("Вкл")
        t.add_column("Название")
        t.add_column("ID", style="dim")
        for i, c in enumerate(cfg["chats"], 1):
            t.add_row(str(i), "✅" if c.get("enabled", True) else "⏸", c.get("title", ""), str(c["id"]))
        console.print(t)
        hint("[a] добавить из списка  [m] добавить по ID  [t] вкл/выкл  [d] удалить  [0] назад")
        ch = Prompt.ask("Действие", default="0").lower()
        changed = False
        if ch == "0":
            return
        elif ch == "a":
            dialogs = load_dialogs()
            if not dialogs:
                console.print("[red]Список пуст: демон не запущен? Можно добавить по ID.[/red]")
                pause()
                continue
            q = Prompt.ask("Фильтр по названию (Enter — все)", default="").lower()
            have = {int(c["id"]) for c in cfg["chats"]}
            shown = [d for d in dialogs if q in d["title"].lower() and d["id"] not in have]
            t = Table()
            t.add_column("#", justify="right")
            t.add_column("Название")
            t.add_column("Тип", style="dim")
            for i, d in enumerate(shown, 1):
                t.add_row(str(i), d["title"], d["type"])
            console.print(t)
            sel = Prompt.ask("Номера через запятую (напр. 1,3 или 2-4)", default="")
            picked = [shown[i] for i in parse_indexes(sel, len(shown))]
            if picked:
                config.update(lambda c: c["chats"].extend(
                    {"id": d["id"], "title": d["title"], "enabled": True} for d in picked))
                changed = True
        elif ch == "m":
            cid = IntPrompt.ask("ID чата (например -1001234567890)")
            title = Prompt.ask("Название (для себя)", default=str(cid))
            config.update(lambda c: c["chats"].append({"id": cid, "title": title, "enabled": True}))
            changed = True
        elif ch in ("t", "d"):
            if not cfg["chats"]:
                continue
            idx = parse_indexes(Prompt.ask("Номер(а)"), len(cfg["chats"]))
            ids = {cfg["chats"][i]["id"] for i in idx}
            if ch == "t":
                def toggle(c):
                    for x in c["chats"]:
                        if x["id"] in ids:
                            x["enabled"] = not x.get("enabled", True)
                config.update(toggle)
            elif ids and Confirm.ask(f"Удалить {len(ids)} чат(ов)? (история в базе останется)"):
                config.update(lambda c: c.__setitem__("chats", [x for x in c["chats"] if x["id"] not in ids]))
            changed = True
        if changed:
            control("chats_changed")


# ---------- LLM ----------
def edit_provider(p: dict) -> dict:
    p["base_url"] = Prompt.ask("Base URL (OpenAI-совместимый)", default=p.get("base_url", ""))
    p["model"] = Prompt.ask("Модель", default=p.get("model", ""))
    key = Prompt.ask(f"API-ключ (Enter — оставить {config.mask(p.get('api_key', ''))})",
                     default="", show_default=False, password=True)
    if key:
        p["api_key"] = key.strip()
    return p


def test_provider(p: dict) -> None:
    async def run():
        reply = await llm.chat([{"role": "user", "content": "Ответь одним словом: работает?"}],
                               provider=p, max_tokens=20, timeout=60)
        console.print(f"[green]Ответ модели:[/green] {reply}")
        try:
            models = await llm.list_models(p)
            console.print("[dim]Доступные модели: " + ", ".join(models[:30]) + "[/dim]")
        except Exception:
            pass
    try:
        with console.status("Проверяю…"):
            asyncio.run(run())
    except Exception as e:
        console.print(f"[red]Ошибка: {e}[/red]")


def menu_llm() -> None:
    while True:
        console.clear()
        cfg = config.current()
        provs = cfg["llm"]["providers"]
        names = list(provs)
        t = Table(title="LLM-провайдеры")
        for col in ("#", "", "Имя", "Base URL", "Модель", "Ключ"):
            t.add_column(col)
        for i, n in enumerate(names, 1):
            p = provs[n]
            t.add_row(str(i), "★" if n == cfg["llm"]["active"] else "", n, p.get("base_url", ""),
                      p.get("model", ""), config.mask(p.get("api_key", "")))
        console.print(t)
        console.print(f"Температура: {cfg['llm']['temperature']}")
        hint("[s] сделать активным  [e] изменить  [a] добавить  [d] удалить  [c] проверить  [k] температура  [0] назад")
        ch = Prompt.ask("Действие", default="0").lower()
        if ch == "0":
            return
        if ch == "a":
            preset = Prompt.ask("Шаблон", choices=[*config.PROVIDER_PRESETS, "другой"], default="deepseek")
            name = Prompt.ask("Имя провайдера", default=preset if preset != "другой" else "custom")
            p = edit_provider(dict(config.PROVIDER_PRESETS.get(preset, {})))
            config.update(lambda c: c["llm"]["providers"].__setitem__(name, p))
            if Confirm.ask("Сделать активным?", default=True):
                config.update(lambda c: c["llm"].__setitem__("active", name))
            continue
        if ch == "k":
            temp = float(Prompt.ask("Температура (0–1.5)", default=str(cfg["llm"]["temperature"])))
            config.update(lambda c: c["llm"].__setitem__("temperature", temp))
            continue
        if ch not in ("s", "e", "d", "c") or not names:
            continue
        default = str(names.index(cfg["llm"]["active"]) + 1) if cfg["llm"]["active"] in names else "1"
        idx = parse_indexes(Prompt.ask("Номер", default=default), len(names))
        if not idx:
            continue
        name = names[idx[0]]
        if ch == "s":
            config.update(lambda c: c["llm"].__setitem__("active", name))
        elif ch == "e":
            p = edit_provider(dict(provs[name]))
            config.update(lambda c: c["llm"]["providers"].__setitem__(name, p))
        elif ch == "d":
            if name == cfg["llm"]["active"]:
                console.print("[red]Нельзя удалить активный провайдер[/red]")
                pause()
            elif Confirm.ask(f"Удалить «{name}»?"):
                config.update(lambda c: c["llm"]["providers"].pop(name, None))
        elif ch == "c":
            test_provider(provs[name])
            pause()


# ---------- сводка ----------
def menu_summary() -> None:
    while True:
        console.clear()
        s = config.current()["summary"]
        console.print(Panel(
            f"Ежедневная сводка: {'[green]вкл[/green]' if s['enabled'] else '[red]выкл[/red]'}\n"
            f"Время: {s['time']}   Часовой пояс: {s['timezone']}", title="Сводка"))
        hint("[e] вкл/выкл  [t] время  [z] часовой пояс  [n] прислать сейчас  [v] показать последнюю  [0] назад")
        ch = Prompt.ask("Действие", default="0").lower()
        if ch == "0":
            return
        if ch == "e":
            config.update(lambda c: c["summary"].__setitem__("enabled", not c["summary"]["enabled"]))
        elif ch == "t":
            v = Prompt.ask("Время ЧЧ:ММ", default=s["time"])
            try:
                hh, mm = (int(x) for x in v.split(":"))
                assert 0 <= hh < 24 and 0 <= mm < 60
                config.update(lambda c: c["summary"].__setitem__("time", f"{hh:02d}:{mm:02d}"))
            except Exception:
                console.print("[red]Неверный формат[/red]")
                pause()
        elif ch == "z":
            v = Prompt.ask("Часовой пояс (напр. Europe/Moscow, Asia/Yekaterinburg)", default=s["timezone"])
            try:
                ZoneInfo(v)
                config.update(lambda c: c["summary"].__setitem__("timezone", v))
            except Exception:
                console.print("[red]Неизвестный часовой пояс[/red]")
                pause()
        elif ch == "n":
            control("summary_now")
            console.print("Отправлено демону — сводка придёт в бота через минуту-другую.")
            pause()
        elif ch == "v":
            from .storage import Storage
            row = Storage(config.DB_PATH).last_summary()
            if row:
                console.print(Panel(re.sub(r"</?[bi]>|</?code>", "", row["text"]),
                                    title=f"от {datetime.fromtimestamp(row['created']):%d.%m %H:%M}"))
            else:
                console.print("Сводок ещё не было.")
            pause()


# ---------- предметы ----------
def menu_subjects() -> None:
    while True:
        console.clear()
        subs = config.current()["subjects"]
        t = Table(title="Предметы (папки в Obsidian)")
        t.add_column("#", justify="right")
        t.add_column("Предмет")
        for i, s in enumerate(subs, 1):
            t.add_row(str(i), s)
        console.print(t)
        console.print("[dim]Новые предметы бот добавляет сам. Переименование влияет только на новые файлы.[/dim]")
        hint("[a] добавить  [r] переименовать  [d] удалить  [0] назад")
        ch = Prompt.ask("Действие", default="0").lower()
        if ch == "0":
            return
        if ch == "a":
            names = [x.strip() for x in Prompt.ask("Название(я) через запятую").split(",") if x.strip()]
            config.update(lambda c: c["subjects"].extend(n for n in names if n not in c["subjects"]))
        elif ch in ("r", "d") and subs:
            idx = parse_indexes(Prompt.ask("Номер"), len(subs))
            if not idx:
                continue
            old = subs[idx[0]]
            if ch == "r":
                new = Prompt.ask("Новое название", default=old).strip()
                config.update(lambda c: c["subjects"].__setitem__(c["subjects"].index(old), new))
            elif Confirm.ask(f"Удалить «{old}»?"):
                config.update(lambda c: c["subjects"].remove(old))


# ---------- подключение Obsidian по QR ----------
LIVESYNC_URI = "obsidian://livesync-connect?v=1&{}"
LIVESYNC_PREFIX = "obsidian://livesync-connect?v=1&"
_SETUP_URI_RE = re.compile(r"obsidian://setuplivesync\?settings=(.+)$", re.S)
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_OAUTH_CLIENT = ("http://localhost/", "use the custom redirect_uri", ["http://localhost/"])
_OAUTH_FIELDS = {
    "register": ["redirect_uris", "client_name", "token_types_supported", "grant_types"],
    "token": ["grant_type", "client_id", "code", "client_secret"],
}


def _stash(cc: dict) -> str:
    """Параметры подключения в короткой кодировке: base64url без паддингов.

    JSON-вариант LiveSync (~400 символов) даёт QR из 113 модулей: на терминале
    такие клетки мельче пикселя и не сканируются. Здесь ~250 символов при
    https-адресе и обычном пароле; с коротким паролем отдельного пользователя —
    108 символов, то есть QR 45x45, который читается с экрана телефона.
    """
    fields = ["web", cc["url"], cc["database"], cc["username"], cc["password"]]
    return base64.urlsafe_b64encode("\x1f".join(fields).encode()).decode().rstrip("=")


def _take(s: str) -> list[str]:
    """Обратная операция к _stash (для тестов и разбора готовых ссылок)."""
    m = _SETUP_URI_RE.match(s.strip())
    if m:
        raw = unquote(m.group(1))
    elif s.strip().startswith(LIVESYNC_PREFIX):
        raw = s.strip()[len(LIVESYNC_PREFIX):].split("&")[0]
    else:
        raw = s.strip()
    try:
        parts = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode().split("\x1f")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return []
    return parts if len(parts) == 5 and parts[0] == "web" else []


def qr_connect_uri(cc: dict) -> str:
    if not cc.get("url") or not cc.get("database"):
        raise ValueError("не заданы URL и имя базы CouchDB")
    if not cc.get("username"):
        raise ValueError("нет логина CouchDB — без него Obsidian не подключится")
    return LIVESYNC_URI.format(_stash(cc))


def print_qr(text: str, ec: str = "L") -> None:
    """Печатает QR прямо в терминале — экран можно сканировать камерой по SSH."""
    import io

    import qrcode

    q = qrcode.QRCode(border=2, error_correction=getattr(qrcode.constants, f"ERROR_CORRECT_{ec}"))
    q.add_data(text)
    q.make(fit=True)
    buf = io.StringIO()
    q.print_ascii(out=buf, invert=True)
    # qrcode рисует модуль двумя символами: для 45 модулей это 90 колонок, и на
    # 80-колоночном SSH код рвётся. Сжимаем пары в один символ (как cli-qrcode).
    half = q.modules_count // 2 + 2
    for line in buf.getvalue().splitlines():
        if not line.strip():
            continue
        chunks = []
        for i in range(half):
            pair = line[2 * i:2 * i + 2].ljust(2)
            chunks.append(pair[0] if pair[0] == pair[1] else "█")
        console.print("".join(chunks).rstrip(), markup=False, highlight=False, soft_wrap=True)


def check_livesync_hint(cc: dict) -> None:
    """Предупреждает, если подключиться по QR заведомо не выйдет."""
    if (urlparse(cc.get("url") or "").hostname or "").lower() in _LOOPBACK:
        console.print("[yellow]URL CouchDB локальный — на телефоне такой адрес ничего не найдёт. "
                      "Для подключения с устройств нужен домен с HTTPS.[/yellow]")


async def _oauth_post(url: str, data: list[tuple[str, str]]) -> dict:
    import httpx

    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(url, data=data)
        r.raise_for_status()
        return r.json()


def _redirects(v) -> list[str]:
    return [x for x in v if x] if isinstance(v, (list, tuple)) else [v or "http://localhost/"]


async def oauth_post(method: str, url: str, data: dict) -> dict:
    """POST к CouchDB-OAuth + докладка поля совместимости в документ клиента.

    Подключиться из Obsidian к CouchDB можно только по OAuth: плагин не отправляет
    в CouchDB логин и пароль. Но новая версия плагина читает из `_clients/<id>`
    поле `redirect_uris`, а CouchDB пишет `redirect_uri` — и проверки падают.
    Поэтому у гранта держим оба поля. Ошибку докладки гасим: не пускает — значит,
    эта версия сервера уже понимает нужное поле.
    """
    body = await _oauth_post(url, [(k, data[k]) for k in _OAUTH_FIELDS[method] if data.get(k, "") != ""])
    cid = str(body.get("client_id") or "")
    if not cid:
        return body
    root = "/".join(url.split("/")[:3])
    extra = ({"redirect_uris": _redirects(body.get("redirect_uri"))} if method == "register"
             else {"redirect_uri": _redirects(body.get("redirect_uri"))[0]})
    try:
        await _oauth_post(f"{root}/_clients/{quote(cid, safe='')}", list(extra.items()))
    except Exception:
        pass
    return body


def make_connect_link(cc: dict, user: str) -> str:
    """OAuth-грант в CouchDB для пользователя Obsidian → ссылка, её и кладём в QR.

    Плагин, получив ссылку, обменивает код на токен и сам создаёт подключение:
    ни логин, ни пароль на телефоне вводить не нужно.
    """
    import httpx

    base = cc["url"].rstrip("/")
    parsed = urlparse(base)
    root, scheme = f"{parsed.scheme}://{parsed.netloc}", parsed.scheme or "https"  # _oauth живёт в корне
    with httpx.Client(auth=(user, cc["password"]), timeout=30) as c:
        r = c.get(f"{base}/_session")
        r.raise_for_status()
        sess = r.json()
        reg = c.post(f"{base}/_oauth/clients", form={
            "redirect_uris": json.dumps(_OAUTH_CLIENT[2]),
            "client_name": "StudyBot",
            "token_types_supported": json.dumps(["bearer"]),
            "grant_types": json.dumps(["authorization_code"]),
        })
        reg.raise_for_status()
        client = reg.json()

    async def flow() -> dict:
        client["scope"] = " ".join(client.get("scopes") or [])
        await oauth_post("register", f"{root}/_oauth/clients", client)
        authorize = await oauth_post("authorize", f"{root}/_oauth/authorize",
                                     {**client, "redirect_uri": _OAUTH_CLIENT[0],
                                      "code": sess["info"]["authenticated"]})
        access = await oauth_post("token", f"{root}/_oauth/token", {
            "grant_type": "authorization_code", "client_id": client["client_id"],
            "code": authorize.get("code") or sess["info"]["authenticated"],
            "client_secret": client["client_secret"],
        })
        access["redirect_uri"] = authorize.get("redirect_uri") or _OAUTH_CLIENT[0]
        return access

    auth = asyncio.run(flow())
    grant = {k: auth.get(k) for k in ("access_token", "token_type", "refresh_token", "expires_in", "scope")}
    uri, sep = auth["redirect_uri"], "&" if "?" in auth["redirect_uri"] else "?"
    return uri + sep + "grant=" + quote(base64.b64encode(json.dumps(grant).encode()).decode(), safe="") \
        + f"&category={scheme}&obfuscate=false"


def setup_uri(cc: dict, passphrase: str) -> str:
    """Шифрованный Setup URI LiveSync — «Import / Copy settings as a new Setup URI».

    шифр — формат openssl по умолчанию: EVP_BytesToKey(MD5) даёт материал на
    48 байт (32 — ключ AES-256-CBC, 16 — iv), блок «Salted__» + iv + данные.
    Именно так шифрует LiveSync, поэтому ссылка читается его «Import / Copy a
    settings URI». Она длинная (~330 символов) и предназначена для копирования:
    QR по ней на телефоне генерирует сам Obsidian.
    """
    inner = qr_connect_uri(cc) + "&hidden=true&name=StudyBot&connect=true&waitForReady=true"
    salt, data = secrets.token_bytes(8), inner.encode()
    material = evp_bytes_to_key(passphrase.encode(), salt, 48)  # key 32 + iv 16, как OpenSSL 1.0.x
    key, iv = material[:32], material[32:]
    pad = 16 - len(data) % 16
    blob = b"Salted__" + salt + aes_cbc(key, iv, data + bytes([pad]) * pad)
    return "obsidian://setuplivesync?settings=" + quote(json.dumps([base64.b64encode(blob).decode()]))


def aes_cbc(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-CBC: есть cryptography — используем его, нет — чистый Python (см. ниже)."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        return _aes_cbc_py(key, iv, data)
    return Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor().update(data)


# --- минимальный AES (только шифрование, CBC) на случай установки без cryptography ---
_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0b7fd9326363ff7cc34a5e5f171d83115"
    "04c723c31896059a071280e2eb27b27509832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2cd0c13ec5f974417c4a77e3d645d1973"
    "60814fdc222a908846eeb814de5e0bdbe0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9ee1f8981169d98e949b1e87e9ce5528df"
    "8ca1890dbfe6426841992d0fb054bb16")
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36, 0x6C)


def evp_bytes_to_key(passphrase: bytes, salt: bytes, size: int) -> bytes:
    """Ключевой материал по EVP_BytesToKey (MD5, 1 итерация) — как OpenSSL до 3.x.

    Схема «d_i = MD5(d_{i-1} + пароль + соль), d_0 = пусто», склеиваем до size.
    Для AES-256 нужен материал в 48 байт: первые 32 — ключ, последние 16 — iv.
    """
    import hashlib

    out, prev = bytearray(), b""
    while len(out) < size:
        prev = hashlib.md5(prev + passphrase + salt).digest()
        out += prev
    return bytes(out[:size])


def _xtime(a: int) -> int:
    return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else a << 1


def _round_keys(key: bytes) -> tuple[list[bytes], int]:
    nk, nr = len(key) // 4, len(key) // 4 + 6
    w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    for i in range(nk, 4 * (nr + 1)):
        t = list(w[i - 1])
        if i % nk == 0:
            t = [_SBOX[b] for b in t[1:] + t[:1]]
            t[0] ^= _RCON[i // nk - 1]
        elif nk > 6 and i % nk == 4:
            t = [_SBOX[b] for b in t]
        w.append([w[i - nk][j] ^ t[j] for j in range(4)])
    return [bytes(sum(w[4 * r:4 * r + 4], [])) for r in range(nr + 1)], nr


def _encrypt_block(rk: list[bytes], nr: int, block: bytes) -> bytes:
    s = [b ^ k for b, k in zip(block, rk[0])]
    for rnd in range(1, nr + 1):
        s = [_SBOX[b] for b in s]
        s = [s[((c + r) % 4) * 4 + r] for c in range(4) for r in range(4)]  # ShiftRows
        if rnd != nr:
            mixed = []
            for c in range(4):
                a0, a1, a2, a3 = s[4 * c:4 * c + 4]
                t = a0 ^ a1 ^ a2 ^ a3
                mixed += [a0 ^ t ^ _xtime(a0 ^ a1), a1 ^ t ^ _xtime(a1 ^ a2),
                          a2 ^ t ^ _xtime(a2 ^ a3), a3 ^ t ^ _xtime(a3 ^ a0)]  # MixColumns
            s = mixed
        s = [b ^ k for b, k in zip(s, rk[rnd])]
    return bytes(s)


def _aes_cbc_py(key: bytes, iv: bytes, data: bytes) -> bytes:
    rk, nr = _round_keys(key)
    out, prev = bytearray(), iv
    for i in range(0, len(data), 16):
        prev = _encrypt_block(rk, nr, bytes(a ^ b for a, b in zip(data[i:i + 16], prev)))
        out += prev
    return bytes(out)


def ensure_livesync_user(cc: dict, user: str) -> str | None:
    """Заводит отдельного пользователя CouchDB для Obsidian — с доступом только к базе.

    В CouchDB нет API «создать пользователя», поэтому в системную базу кладётся
    документ `org.couchdb.user:<login>`, а в базу Obsidian — `_security`, где он
    админ. Пароль короткий (45 бит), чтобы влезть в QR-код; границы безопасности
    даёт сам пользователь: не админ сервера, а только эта база.
    Возвращает пароль, либо None — пользователь уже есть или CouchDB не пускает.
    """
    import httpx

    url, db = cc["url"].rstrip("/"), cc["database"]
    pw = base64.b32encode(secrets.token_bytes(6)).decode()[:9]
    doc = {"_id": f"org.couchdb.user:{user}", "name": user, "type": "user", "roles": [], "password": pw}
    try:
        with httpx.Client(auth=(cc["username"], cc["password"]), timeout=20) as c:
            if c.put(f"{url}/_users/{quote(doc['_id'], safe='')}", json=doc).status_code not in (201, 202):
                return None  # уже существует: пароль не подменить
            c.put(f"{url}/{quote(db, safe='')}/_security",
                  json={"admins": {"names": [user], "roles": []}, "members": {"names": [user], "roles": []}})
    except Exception:
        return None
    return pw


def show_connect_qr(cc: dict) -> None:
    """QR-код для быстрого подключения Obsidian к базе."""
    console.clear()
    user = cc.get("username") or ""
    if user and user != "admin":
        # install.sh и гайды заводят админа сервера: его пароль (32 hex) в QR не
        # влезает, а писать в базу от админа сервера всё равно не стоит.
        created = ensure_livesync_user(cc, user)
        if created:
            config.update(lambda c: c["couchdb"].update({"username": user, "password": created}))
            cc = config.current()["couchdb"]
            console.print(f"[green]Заведён отдельный пользователь CouchDB «{user}» для Obsidian[/green]")
            console.print("[dim]Доступ только к этой базе; новый пароль сохранён в config.json.[/dim]\n")
    try:
        qr_connect_uri(cc)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        pause()
        return
    try:
        uri, kind = make_connect_link(cc, user), "готовая ссылка подключения"
    except Exception as e:
        console.print(f"[yellow]CouchDB не ответила: {e}[/yellow]")
        console.print("[dim]Показываю ссылку с логином и паролем — грант создастся при подключении.[/dim]")
        uri, kind = qr_connect_uri(cc), "ссылка с логином и паролем"
    console.print(Panel(
        "1. Установите в Obsidian плагин [bold]Self-hosted LiveSync[/bold]\n"
        "2. В палитре команд: [bold]Import settings from QR[/bold] — либо отсканируйте\n"
        "   код камерой телефона, Obsidian откроет ссылку сам\n"
        "[dim]Адрес CouchDB должен быть доступен с телефона по HTTPS.[/dim]",
        title="Как подключиться", border_style="dim"))
    print_qr(uri)
    console.print(f"[dim]{kind}, {len(uri)} символов[/dim]")
    check_livesync_hint(cc)
    hint("[t] вывести ссылку текстом  [u] Setup URI с парольной фразой  [0] назад")
    ch = Prompt.ask("Действие", default="0").lower()
    if ch == "t":
        console.print(uri, markup=False, highlight=False, soft_wrap=True)
        console.print("[dim]Откройте ссылку на телефоне (или вставьте в Obsidian → «Import settings from QR»)."
                      "[/dim]")
        pause()
    elif ch == "u":
        passphrase = Prompt.ask("Парольная фраза для Setup URI (передайте её другим каналом)",
                                default="", show_default=False, password=True)
        if passphrase:
            console.print(setup_uri(cc, passphrase), markup=False, highlight=False, soft_wrap=True)
            console.print("[dim]В Obsidian: «Import / Copy a settings URI» → вставить → ввести фразу. "
                          "Ссылка зашифрована, но длинная — для копирования, а не для QR.[/dim]")
            pause()

# ---------- Obsidian / материалы ----------
def menu_obsidian() -> None:
    while True:
        console.clear()
        cfg = config.current()
        m, cc = cfg["materials"], cfg["couchdb"]
        console.print(Panel(
            f"Сбор материалов: {'вкл' if m['enabled'] else 'выкл'}   разбирать фото: {'да' if m['photos'] else 'нет'}\n"
            f"Лимит файла: {m['max_file_mb']} МБ   локальная копия: {'да' if m['save_local'] else 'нет'} ({config.MATERIALS_DIR})\n\n"
            f"CouchDB (LiveSync): {'[green]вкл[/green]' if cc['enabled'] else '[red]выкл[/red]'}\n"
            f"URL: {cc['url']}   база: {cc['database']}   логин: {cc['username'] or '—'}   пароль: {config.mask(cc['password'])}\n"
            f"Папка в хранилище: {cc['base_folder']}   учёт регистра: {cc['case_sensitive']}",
            title="Материалы и Obsidian"))
        hint("[m] сбор вкл/выкл  [f] фото вкл/выкл  [l] лимит  [b] локальная копия  "
             "[c] CouchDB вкл/выкл  [s] настройки CouchDB  [t] проверить CouchDB  "
             "[q] QR для Obsidian  [0] назад")
        ch = Prompt.ask("Действие", default="0").lower()
        if ch == "0":
            return
        if ch == "q":
            show_connect_qr(cc)
        elif ch == "m":
            config.update(lambda c: c["materials"].__setitem__("enabled", not c["materials"]["enabled"]))
        elif ch == "f":
            config.update(lambda c: c["materials"].__setitem__("photos", not c["materials"]["photos"]))
        elif ch == "b":
            config.update(lambda c: c["materials"].__setitem__("save_local", not c["materials"]["save_local"]))
        elif ch == "l":
            v = IntPrompt.ask("Макс. размер файла, МБ", default=m["max_file_mb"])
            config.update(lambda c: c["materials"].__setitem__("max_file_mb", max(1, v)))
        elif ch == "c":
            config.update(lambda c: c["couchdb"].__setitem__("enabled", not c["couchdb"]["enabled"]))
        elif ch == "s":
            new = dict(cc)
            new["url"] = Prompt.ask("URL CouchDB", default=cc["url"])
            new["database"] = Prompt.ask("Имя базы (как в LiveSync)", default=cc["database"])
            new["username"] = Prompt.ask("Логин", default=cc["username"])
            pw = Prompt.ask("Пароль (Enter — оставить)", default="", show_default=False, password=True)
            if pw:
                new["password"] = pw
            new["base_folder"] = Prompt.ask("Папка в хранилище", default=cc["base_folder"])
            new["case_sensitive"] = Prompt.ask("Учёт регистра в путях", choices=["auto", "true", "false"],
                                               default=str(cc["case_sensitive"]).lower())
            config.update(lambda c: c["couchdb"].update(new))
        elif ch == "t":
            check_couch(config.current()["couchdb"])
            pause()


def check_couch(cc: dict) -> bool:
    async def run():
        ls = LiveSync(cc)
        try:
            info = await ls.detect()
            console.print(f"[green]OK[/green]: документов {info['docs']}, учёт регистра: {info['case_sensitive']}")
            return True
        finally:
            await ls.close()
    try:
        with console.status("Подключаюсь к CouchDB…"):
            return asyncio.run(run())
    except Exception as e:
        console.print(f"[red]Ошибка: {e}[/red]")
        return False


# ---------- Telegram ----------
def menu_telegram() -> None:
    while True:
        console.clear()
        tg = config.current()["telegram"]
        console.print(Panel(
            f"api_id: {tg['api_id'] or '—'}   api_hash: {config.mask(tg['api_hash'])}\n"
            f"Сессия аккаунта: {'есть' if tg['session'] else '[red]нет[/red]'}\n"
            f"Токен бота: {config.mask(tg['bot_token'])}\n"
            f"Получатель сводок (owner_id): {tg['owner_id'] or 'авто (ваш аккаунт)'}", title="Telegram"))
        hint("[l] войти в аккаунт заново  [b] токен бота  [o] owner_id  [k] api_id/api_hash  [0] назад")
        ch = Prompt.ask("Действие", default="0").lower()
        if ch == "0":
            return
        if ch == "l":
            do_login()
            pause()
        elif ch == "b":
            tok = Prompt.ask("Токен от @BotFather", password=True).strip()
            if tok:
                config.update(lambda c: c["telegram"].__setitem__("bot_token", tok))
                control("restart")
        elif ch == "o":
            v = IntPrompt.ask("Telegram ID получателя (0 — авто)", default=int(tg["owner_id"] or 0))
            config.update(lambda c: c["telegram"].__setitem__("owner_id", v))
        elif ch == "k":
            aid = IntPrompt.ask("api_id", default=int(tg["api_id"] or 0))
            ah = Prompt.ask("api_hash", default=tg["api_hash"]).strip()
            config.update(lambda c: c["telegram"].update({"api_id": aid, "api_hash": ah}))
            control("restart")


def do_login() -> list[dict]:
    from .login import interactive_login
    tg = config.current()["telegram"]
    if not tg["api_id"] or not tg["api_hash"]:
        console.print("[red]Сначала задайте api_id и api_hash[/red]")
        return []
    try:
        session, name, dialogs = asyncio.run(interactive_login(int(tg["api_id"]), tg["api_hash"]))
    except Exception as e:
        console.print(f"[red]Вход не удался: {e}[/red]")
        return []
    config.update(lambda c: c["telegram"].__setitem__("session", session))
    control("restart")
    console.print(f"[green]Вошли как {name}. Демон перезапустится с новой сессией.[/green]")
    return dialogs


# ---------- логи / рестарт ----------
def show_logs() -> None:
    console.clear()
    try:
        lines = config.LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-60:]
    except FileNotFoundError:
        lines = ["(лога пока нет)"]
    for line in lines:
        style = "red" if " ERROR " in line else ("yellow" if " WARNING " in line else None)
        console.print(line, style=style, markup=False, highlight=False)
    pause()


def update_program() -> bool:
    """git pull + переустановка. Возвращает True, если код обновился (панель нужно открыть заново)."""
    console.clear()
    console.print("Обновление с GitHub (понадобится пароль sudo, если он настроен)…\n")
    try:
        code = subprocess.call(["sudo", "/usr/local/bin/studybot", "update"])
    except FileNotFoundError:
        console.print("[red]Команда studybot не установлена — запустите deploy/install.sh[/red]")
        pause()
        return False
    pause()
    if code == 0 and Confirm.ask("Если код обновился — панель нужно открыть заново. Выйти сейчас?", default=True):
        return True
    return False


def main() -> None:
    while True:
        console.clear()
        header()
        hint(
            "[1] Чаты\n[2] LLM / API\n[3] Сводка\n[4] Предметы\n[5] Материалы и Obsidian\n"
            "[6] Telegram (аккаунт, бот)\n[7] Логи\n[8] Перезапустить демон\n[9] Обновить программу\n[0] Выход"
        )
        ch = Prompt.ask("Выбор", default="0")
        try:
            if ch == "0":
                return
            {"1": menu_chats, "2": menu_llm, "3": menu_summary, "4": menu_subjects,
             "5": menu_obsidian, "6": menu_telegram, "7": show_logs}.get(ch, lambda: None)()
            if ch == "8":
                control("restart")
                console.print("Демон перезапускается…")
                time.sleep(2)
            elif ch == "9" and update_program():
                return
        except KeyboardInterrupt:
            continue
        except Exception as e:
            console.print(f"[red]Ошибка: {e}[/red]")
            pause()
