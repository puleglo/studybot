"""studybot run | panel | setup | login"""
from __future__ import annotations

import argparse
import asyncio
import sys


def setup_wizard() -> None:
    from rich.prompt import Confirm, IntPrompt, Prompt
    from rich.table import Table

    from . import config, panel
    from .panel import console

    def dflt(value) -> dict:
        """default= только если значение есть (иначе rich вернёт None на пустой ввод)."""
        return {"default": value} if value else {}

    console.rule("[bold cyan]Первичная настройка StudyBot")
    tg = config.current()["telegram"]

    console.print("\n[bold]1. Telegram API[/bold] — получите на https://my.telegram.org → API development tools")
    aid = IntPrompt.ask("api_id", **dflt(int(tg["api_id"] or 0)))
    ah = Prompt.ask("api_hash", **dflt(tg["api_hash"])).strip()
    config.update(lambda c: c["telegram"].update({"api_id": aid, "api_hash": ah}))

    dialogs = []
    if not tg["session"] or Confirm.ask("Сессия аккаунта уже есть. Войти заново?", default=False):
        console.print("\n[bold]2. Вход в ваш аккаунт[/bold] (бот будет только читать чаты)")
        dialogs = panel.do_login()
    elif not config.current()["chats"] or Confirm.ask("Чаты уже выбраны. Изменить список?", default=False):
        from .login import list_dialogs
        tg = config.current()["telegram"]
        try:
            with console.status("Загружаю список ваших групп…"):
                dialogs = asyncio.run(list_dialogs(int(tg["api_id"]), tg["api_hash"], tg["session"]))
        except Exception as e:
            console.print(f"[red]Не удалось получить список чатов: {e}[/red]")
    if dialogs and Confirm.ask("\nВыбрать чаты для отслеживания сейчас?", default=True):
        t = Table()
        t.add_column("#", justify="right")
        t.add_column("Название")
        t.add_column("Тип", style="dim")
        for i, d in enumerate(dialogs, 1):
            t.add_row(str(i), d["title"], d["type"])
        console.print(t)
        picked = [dialogs[i] for i in panel.parse_indexes(Prompt.ask("Номера через запятую"), len(dialogs))]
        have = {int(c["id"]) for c in config.current()["chats"]}
        config.update(lambda c: c["chats"].extend(
            {"id": d["id"], "title": d["title"], "enabled": True} for d in picked if d["id"] not in have))

    console.print("\n[bold]3. Бот для сводок[/bold] — создайте в @BotFather и потом нажмите у него /start")
    tok = Prompt.ask("Токен бота", **dflt(config.current()["telegram"]["bot_token"]), password=True).strip()
    config.update(lambda c: c["telegram"].__setitem__("bot_token", tok))

    console.print("\n[bold]4. LLM[/bold] — ключ DeepSeek: https://platform.deepseek.com/api_keys")
    cfg = config.current()
    name = cfg["llm"]["active"]
    p = dict(cfg["llm"]["providers"].get(name) or {**config.PROVIDER_PRESETS["deepseek"], "api_key": ""})
    p = panel.edit_provider(p)
    config.update(lambda c: c["llm"]["providers"].__setitem__(name, p))
    if Confirm.ask("Проверить API?", default=True):
        panel.test_provider(p)

    console.print("\n[bold]5. Сводка[/bold]")
    s = config.current()["summary"]
    tz = Prompt.ask("Часовой пояс", default=s["timezone"])
    tm = Prompt.ask("Время вечерней сводки ЧЧ:ММ", default=s["time"])
    config.update(lambda c: c["summary"].update({"timezone": tz, "time": tm}))

    subs = Prompt.ask("\n[bold]6. Предметы[/bold] через запятую (можно пусто — бот заведёт сам)", default="")
    names = [x.strip() for x in subs.split(",") if x.strip()]
    if names:
        config.update(lambda c: c["subjects"].extend(n for n in names if n not in c["subjects"]))

    console.print("\n[bold]7. Obsidian через CouchDB (Self-hosted LiveSync)[/bold]")
    cc_now = config.current()["couchdb"]
    if cc_now["enabled"] and cc_now["password"]:
        console.print(f"Уже настроено установщиком: {cc_now['url']}, база «{cc_now['database']}».")
        panel.check_couch(cc_now)
        ask_couch = Confirm.ask("Изменить настройки CouchDB?", default=False)
    else:
        ask_couch = Confirm.ask("Настроить сейчас? (базы нет — бот создаст её сам)", default=True)
    if ask_couch:
        cc = dict(config.current()["couchdb"])
        cc["url"] = Prompt.ask("URL CouchDB", default=cc["url"])
        cc["database"] = Prompt.ask("Имя базы (как в LiveSync)", default=cc["database"])
        cc["username"] = Prompt.ask("Логин", **dflt(cc["username"]))
        cc["password"] = Prompt.ask("Пароль", password=True)
        cc["base_folder"] = Prompt.ask("Папка в хранилище", default=cc["base_folder"])
        cc["enabled"] = panel.check_couch(cc) or Confirm.ask("Проверка не прошла. Всё равно включить?", default=False)
        config.update(lambda c: c["couchdb"].update(cc))

    problems = config.validate(config.current())
    console.rule()
    if problems:
        console.print("[yellow]Осталось: " + "; ".join(problems))
    else:
        console.print("[green]Готово![/green] Запуск:  [bold]sudo systemctl enable --now studybot[/bold]\n"
                      "Панель:  [bold]studybot[/bold]   (по SSH: ssh vps -t studybot)")


def main() -> None:
    ap = argparse.ArgumentParser(prog="studybot")
    ap.add_argument("command", nargs="?", default="panel",
                    choices=["run", "panel", "setup", "login", "update", "qr"])
    args, _ = ap.parse_known_args()
    if args.command == "update":
        # обновление делает обёртка /usr/local/bin/studybot (git pull + install.sh от root)
        print("Используйте команду:  studybot update")
        return
    if args.command == "run":
        from .app import main as run_main
        try:
            asyncio.run(run_main())
        except KeyboardInterrupt:
            pass
    elif args.command == "panel":
        from .panel import main as panel_main
        try:
            panel_main()
        except (KeyboardInterrupt, EOFError):
            print()
    elif args.command == "setup":
        setup_wizard()
    elif args.command == "login":
        from .panel import do_login
        do_login()
    elif args.command == "qr":
        from .panel import show_connect_qr
        from . import config
        try:
            show_connect_qr(config.current()["couchdb"])
        except (KeyboardInterrupt, EOFError):
            print()


if __name__ == "__main__":
    sys.exit(main())
