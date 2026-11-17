"""Панель управления в терминале. Доступна только через SSH — сетевых портов нет.
Изменения пишутся в config.json, демон подхватывает их на лету; для действий,
которые нужно выполнить в демоне, кладётся файл-флаг в data/control/."""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from . import config, llm
from .livesync import LiveSync

console = Console()


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
        console.print("[a] добавить из списка  [m] добавить по ID  [t] вкл/выкл  [d] удалить  [0] назад")
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
        console.print("[s] сделать активным  [e] изменить  [a] добавить  [d] удалить  [c] проверить  [k] температура  [0] назад")
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
        console.print("[e] вкл/выкл  [t] время  [z] часовой пояс  [n] прислать сейчас  [v] показать последнюю  [0] назад")
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
                import re
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
        console.print("[a] добавить  [r] переименовать  [d] удалить  [0] назад")
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
        console.print("[m] сбор вкл/выкл  [f] фото вкл/выкл  [l] лимит  [b] локальная копия  "
                      "[c] CouchDB вкл/выкл  [s] настройки CouchDB  [t] проверить CouchDB  [0] назад")
        ch = Prompt.ask("Действие", default="0").lower()
        if ch == "0":
            return
        if ch == "m":
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
        console.print("[l] войти в аккаунт заново  [b] токен бота  [o] owner_id  [k] api_id/api_hash  [0] назад")
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
        console.print(
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
