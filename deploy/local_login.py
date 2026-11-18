"""Вход в Telegram с вашего компьютера (домашний IP), если код не доходит при входе с VPS.

    python deploy/local_login.py data/login_api.json data/session.txt

login_api.json: {"api_id": ..., "api_hash": "..."}. Готовая сессия пишется в файл
(на экран не выводится); затем её нужно перенести в config.json на сервере.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from studybot.login import interactive_login  # noqa: E402


def main() -> None:
    api_path, out_path = sys.argv[1], sys.argv[2]
    with open(api_path, encoding="utf-8") as f:
        api = json.load(f)
    session, name, dialogs = asyncio.run(interactive_login(int(api["api_id"]), api["api_hash"]))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(session)
    print(f"\nГотово: вошли как {name}. Групп/каналов: {len(dialogs)}.")
    print("Можно закрыть окно и написать в чат «вошёл».")


if __name__ == "__main__":
    main()
