"""Конфиг в data/config.json. Демон перечитывает его на лету (по mtime),
поэтому изменения из панели применяются без перезапуска."""
from __future__ import annotations

import contextlib
import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("STUDYBOT_DATA", ROOT / "data"))
CONFIG_PATH = DATA_DIR / "config.json"
CONTROL_DIR = DATA_DIR / "control"
STATUS_PATH = DATA_DIR / "status.json"
DIALOGS_PATH = DATA_DIR / "dialogs.json"
LOG_PATH = DATA_DIR / "studybot.log"
DB_PATH = DATA_DIR / "studybot.db"
MATERIALS_DIR = DATA_DIR / "materials"

PROVIDER_PRESETS = {
    "deepseek": {"base_url": "https://api.deepseek.com", "model": "deepseek-flash"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "model": "deepseek/deepseek-flash"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-5-mini"},
}

DEFAULTS: dict = {
    "telegram": {
        "api_id": 0,
        "api_hash": "",
        "session": "",       # StringSession вашего аккаунта (= полный доступ к аккаунту!)
        "bot_token": "",
        "owner_id": 0,       # кому бот шлёт сводки; заполняется автоматически = ваш id
    },
    "llm": {
        "active": "deepseek",
        "temperature": 0.3,
        "providers": {
            "deepseek": {**PROVIDER_PRESETS["deepseek"], "api_key": ""},
        },
    },
    "chats": [],              # [{"id": -100..., "title": "...", "enabled": true}]
    "summary": {"enabled": True, "time": "21:00", "timezone": "Europe/Moscow"},
    "subjects": [],           # пополняется автоматически, правится в панели
    "materials": {
        "enabled": True,
        "max_file_mb": 25,
        "save_local": True,   # копия в data/materials (бэкап)
        "photos": True,       # разбирать ли фото (решения на листочках и т.п.)
    },
    "couchdb": {
        "enabled": False,
        "url": "http://127.0.0.1:5984",
        "database": "obsidian",
        "username": "",
        "password": "",
        "base_folder": "Учёба",
        "case_sensitive": "auto",  # auto | true | false — как в LiveSync "Handle files as Case-Sensitive"
    },
}

# ключи, внутрь которых дефолты не подмешиваются (пользователь может удалить элементы)
_NO_MERGE = {"providers"}

_cache: dict | None = None
_cache_mtime: float | None = None


def _merge(defaults: dict, data: dict) -> dict:
    out = copy.deepcopy(defaults)
    for k, v in data.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict) and k not in _NO_MERGE:
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    if not CONFIG_PATH.exists():
        return copy.deepcopy(DEFAULTS)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return _merge(DEFAULTS, json.load(f))


def save(cfg: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG_PATH)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
    global _cache, _cache_mtime
    _cache, _cache_mtime = None, None


@contextlib.contextmanager
def _lock():
    """Межпроцессная блокировка (демон и панель пишут один файл)."""
    try:
        import fcntl
    except ImportError:  # Windows — для локальной разработки
        yield
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATA_DIR / ".config.lock", "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def update(fn: Callable[[dict], None]) -> dict:
    """Атомарно: прочитать свежий конфиг с диска, изменить, сохранить."""
    with _lock():
        cfg = load()
        fn(cfg)
        save(cfg)
        return cfg


def current() -> dict:
    """Кэшированный конфиг; перечитывается, если файл изменился."""
    global _cache, _cache_mtime
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except FileNotFoundError:
        mtime = None
    if _cache is None or mtime != _cache_mtime:
        _cache = load()
        _cache_mtime = mtime
    return _cache


def enabled_chats(cfg: dict | None = None) -> list[dict]:
    cfg = cfg or current()
    return [c for c in cfg["chats"] if c.get("enabled", True)]


def validate(cfg: dict) -> list[str]:
    problems = []
    tg = cfg["telegram"]
    if not tg.get("api_id") or not tg.get("api_hash"):
        problems.append("не заданы telegram.api_id / api_hash")
    if not tg.get("session"):
        problems.append("аккаунт не авторизован (studybot login)")
    if not tg.get("bot_token"):
        problems.append("не задан токен бота")
    prov = cfg["llm"]["providers"].get(cfg["llm"]["active"])
    if not prov:
        problems.append(f"активный LLM-провайдер «{cfg['llm']['active']}» не найден")
    elif not prov.get("api_key"):
        problems.append("не задан API-ключ LLM")
    return problems


def mask(secret: str) -> str:
    if not secret:
        return "—"
    if len(secret) <= 8:
        return "****"
    return secret[:4] + "…" + secret[-4:]
