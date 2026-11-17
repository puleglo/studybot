"""Основной демон: юзербот + бот + планировщик сводок + управление из панели."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

from . import config, llm, summary
from .botui import Bot
from .collector import Collector, display_name
from .materials import Materials, Sink
from .storage import Storage

log = logging.getLogger("studybot")

CONTROL_COMMANDS = ("restart", "summary_now", "refresh_dialogs", "chats_changed")


def setup_logging() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(config.LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.handlers[:] = [fh, sh]
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


class App:
    def __init__(self, store: Storage):
        self.store = store
        self.started = time.time()
        self.stop = asyncio.Event()
        self.summary_lock = asyncio.Lock()
        self.errors: list[str] = []
        self.sink = Sink(self.notify)
        self.materials = Materials(store, self.sink, self)
        self.collector = Collector(store, self.materials.handle)
        self.bot = Bot(self)
        self._summary_failures = 0
        self._summary_retry_at = 0.0

    # --- утилиты ---
    def tz(self) -> ZoneInfo:
        return ZoneInfo(config.current()["summary"]["timezone"])

    def midnight_ts(self) -> float:
        return datetime.now(self.tz()).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def record_error(self, text: str) -> None:
        self.errors = (self.errors + [f"{datetime.now():%d.%m %H:%M} {text}"])[-10:]

    async def notify(self, text: str) -> None:
        try:
            await self.bot.send(text)
        except Exception:
            log.exception("Не удалось отправить уведомление")

    def status_text(self) -> str:
        cfg = config.current()
        name, prov = llm.active_provider(cfg)
        last = self.store.kv_get("last_summary_ts")
        last_s = datetime.fromtimestamp(float(last), self.tz()).strftime("%d.%m %H:%M") if last else "—"
        chats = config.enabled_chats(cfg)
        lines = [
            "📊 <b>Статус</b>",
            f"Работаю {int((time.time() - self.started) // 3600)} ч",
            f"Аккаунт: {html.escape(display_name(self.collector.me))}",
            f"Чаты ({len(chats)}): " + html.escape(", ".join(c.get("title") or str(c["id"]) for c in chats) or "—"),
            f"Сообщений сегодня: {self.store.count_messages_since(self.midnight_ts())}",
            f"Материалов сегодня: {len(self.store.materials_since(self.midnight_ts()))}",
            f"LLM: {html.escape(name)} / {html.escape(prov['model'])}",
            f"Сводка: {'в ' + cfg['summary']['time'] if cfg['summary']['enabled'] else 'выключена'}, прошлая — {last_s}",
            f"CouchDB: {'вкл' if cfg['couchdb']['enabled'] else 'выкл'}",
        ]
        if self.errors:
            lines.append("\n⚠️ Последние ошибки:\n" + html.escape("\n".join(self.errors[-3:])))
        return "\n".join(lines)

    # --- сводка ---
    async def send_summary(self, mode: str = "scheduled") -> None:
        """mode: scheduled — вечерняя (сдвигает точку отсчёта), manual — с прошлой вечерней,
        today — с полуночи."""
        async with self.summary_lock:
            now = time.time()
            if mode == "today":
                ts_from = self.midnight_ts()
            else:
                last = self.store.kv_get("last_summary_ts")
                ts_from = float(last) if last else self.midnight_ts()
                ts_from = max(ts_from, now - 48 * 3600)
            titles = {cid: info["title"] for cid, info in self.collector.chat_info.items()}
            text = await summary.build(self.store, titles, ts_from, now)
            await self.bot.send(text)
            if mode == "scheduled":
                self.store.kv_set("last_summary_ts", now)
                self.store.kv_set("last_summary_date", datetime.now(self.tz()).date().isoformat())
            log.info("Сводка (%s) отправлена", mode)

    async def scheduler(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                cfg = config.current()["summary"]
                if not cfg["enabled"]:
                    continue
                now = datetime.now(ZoneInfo(cfg["timezone"]))
                hh, mm = (int(x) for x in cfg["time"].split(":"))
                today = now.date().isoformat()
                if (now.hour, now.minute) < (hh, mm) or self.store.kv_get("last_summary_date") == today:
                    continue
                if time.time() < self._summary_retry_at:
                    continue
                try:
                    await self.send_summary("scheduled")
                    self._summary_failures = 0
                except Exception as e:
                    self._summary_failures += 1
                    log.exception("Вечерняя сводка не удалась (попытка %d)", self._summary_failures)
                    self.record_error(f"сводка: {e}")
                    if self._summary_failures >= 3:
                        self.store.kv_set("last_summary_date", today)
                        self._summary_failures = 0
                        await self.notify(f"⚠️ Вечерняя сводка не получилась после 3 попыток: {html.escape(str(e))}")
                    else:
                        self._summary_retry_at = time.time() + 600
            except Exception:
                log.exception("Ошибка планировщика")

    # --- управление из панели (файлы-флаги в data/control) ---
    async def control_loop(self) -> None:
        config.CONTROL_DIR.mkdir(parents=True, exist_ok=True)
        while True:
            await asyncio.sleep(3)
            for cmd in CONTROL_COMMANDS:
                flag = config.CONTROL_DIR / cmd
                if not flag.exists():
                    continue
                try:
                    flag.unlink()
                except FileNotFoundError:
                    continue
                log.info("Команда из панели: %s", cmd)
                try:
                    if cmd == "restart":
                        self.stop.set()
                    elif cmd == "summary_now":
                        asyncio.create_task(self._safe(self.send_summary("manual")))
                    elif cmd == "refresh_dialogs":
                        await self.collector.dump_dialogs()
                    elif cmd == "chats_changed":
                        await self.collector.dump_dialogs()
                        await self.collector.catch_up()
                except Exception as e:
                    log.exception("Команда %s не выполнена", cmd)
                    self.record_error(f"{cmd}: {e}")

    async def _safe(self, coro) -> None:
        try:
            await coro
        except Exception as e:
            log.exception("Ошибка фоновой задачи")
            self.record_error(str(e))
            await self.notify(f"⚠️ {html.escape(str(e))}")

    async def heartbeat(self) -> None:
        while True:
            try:
                status = {
                    "pid": os.getpid(),
                    "started": self.started,
                    "heartbeat": time.time(),
                    "account": display_name(self.collector.me),
                    "bot": getattr(self.bot.me, "username", None),
                    "errors": self.errors[-5:],
                }
                config.STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
            except Exception:
                log.exception("heartbeat")
            await asyncio.sleep(15)

    # --- запуск ---
    async def run(self) -> None:
        await self.collector.start()
        if not config.current()["telegram"].get("owner_id"):
            me_id = self.collector.me.id
            config.update(lambda c: c["telegram"].__setitem__("owner_id", me_id))
        await self.bot.start()
        await self.collector.dump_dialogs()

        tasks = [
            asyncio.create_task(self.scheduler()),
            asyncio.create_task(self.control_loop()),
            asyncio.create_task(self.heartbeat()),
            asyncio.ensure_future(asyncio.shield(self.collector.client.disconnected)),
            asyncio.ensure_future(asyncio.shield(self.bot.client.disconnected)),
            asyncio.create_task(self.stop.wait()),
        ]
        await self.collector.catch_up()
        log.info("Запущен. Слежу за %d чатами.", len(config.enabled_chats()))
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if not t.cancelled() and t.exception():
                    log.error("Задача завершилась с ошибкой: %r", t.exception())
        finally:
            for t in tasks:
                t.cancel()
            await self.sink.close()
            for client in (self.collector.client, self.bot.client):
                try:
                    await client.disconnect()
                except Exception:
                    pass
        log.info("Остановлен (systemd перезапустит)")


async def main() -> None:
    setup_logging()
    while True:
        problems = config.validate(config.current())
        if not problems:
            break
        log.error("Бот не настроен: %s. Запустите `studybot setup`. Жду…", "; ".join(problems))
        await asyncio.sleep(30)
    # флаги, оставшиеся с прошлого раза (например, restart после setup), не нужны
    if config.CONTROL_DIR.exists():
        for flag in config.CONTROL_DIR.iterdir():
            flag.unlink(missing_ok=True)
    store = Storage(config.DB_PATH)
    await App(store).run()
