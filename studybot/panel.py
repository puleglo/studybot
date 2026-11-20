"""Панель управления в терминале. Доступна только через SSH — сетевых портов нет.
Изменения пишутся в config.json, демон подхватывает их на лету; для действий,
которые нужно выполнить в демоне, кладётся файл-флаг в data/control/."""
from __future__ import annotations

import asyncio
import base64
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


# ---------- подключение Obsidian (Self-hosted LiveSync) ----------
# Форматы полностью соответствуют obsidian-livesync / livesync-commonlib 0.2.x:
#   * QR:  obsidian://setuplivesync?settingsQR=<encodeAnyArray(массив настроек)>
#   * URI: obsidian://setuplivesync?settings=<encodeURIComponent("%$" + base64(HKDF-AES-GCM))>
SETUP_URI_BASE = "obsidian://setuplivesync?settings="
SETUP_QR_BASE = "obsidian://setuplivesync?settingsQR="
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


class _Undef:
    """Соответствует undefined в настройках LiveSync (QR кодирует его как 'u')."""
    __slots__ = ()


_UNDEF = _Undef()

# Индекс каждого поля в QR-массиве (setting.const.qr.js из livesync-commonlib).
_KEY_INDEX = {
    "remoteType": 0, "useCustomRequestHandler": 1, "couchDB_URI": 2, "couchDB_USER": 3,
    "couchDB_PASSWORD": 4, "couchDB_DBNAME": 5, "minimumChunkSize": 6, "longLineThreshold": 7,
    "encrypt": 8, "passphrase": 9, "usePathObfuscation": 10, "checkIntegrityOnSave": 11,
    "batch_size": 12, "batches_limit": 13, "useHistory": 14, "disableRequestURI": 15,
    "checkConflictOnlyOnOpen": 16, "showMergeDialogOnlyOnActive": 17, "additionalSuffixOfDatabaseName": 18,
    "ignoreVersionCheck": 19, "deleteMetadataOfDeletedFiles": 20, "customChunkSize": 21,
    "readChunksOnline": 22, "automaticallyDeleteMetadataOfDeletedFiles": 23, "useDynamicIterationCount": 24,
    "permitEmptyPassphrase": 25, "useTimeouts": 26, "doNotPaceReplication": 27, "hashCacheMaxCount": 28,
    "hashCacheMaxAmount": 29, "concurrencyOfReadChunksOnline": 30, "minimumIntervalOfReadChunksOnline": 31,
    "hashAlg": 32, "enableCompression": 33, "accessKey": 34, "bucket": 35, "endpoint": 36, "region": 37,
    "secretKey": 38, "useEden": 39, "maxChunksInEden": 40, "maxTotalLengthInEden": 41, "maxAgeInEden": 42,
    "disableCheckingConfigMismatch": 43, "handleFilenameCaseSensitive": 44, "doNotUseFixedRevisionForChunks": 45,
    "sendChunksBulk": 46, "sendChunksBulkMaxSize": 47, "useSegmenter": 48, "liveSync": 49, "syncOnSave": 50,
    "syncOnStart": 51, "syncOnFileOpen": 52, "syncOnEditorSave": 53, "syncMinimumInterval": 54,
    "showVerboseLog": 55, "lessInformationInLog": 56, "showLongerLogInsideEditor": 57, "showStatusOnEditor": 58,
    "showStatusOnStatusbar": 59, "showOnlyIconsOnEditor": 60, "displayLanguage": 61, "trashInsteadDelete": 62,
    # Как в плагине: batchSaveMinimumDelay занимает тот же индекс 64 и перекрывает batchSave.
    "doNotDeleteFolder": 63, "batchSave": 64, "batchSaveMinimumDelay": 64,
    "batchSaveMaximumDelay": 65, "syncMaxSizeInMB": 66,
    "useIgnoreFiles": 67, "ignoreFiles": 68, "syncOnlyRegEx": 69, "syncIgnoreRegEx": 70, "syncAfterMerge": 71,
    "resolveConflictsByNewerFile": 72, "writeDocumentsIfConflicted": 73, "disableMarkdownAutoMerge": 74,
    "configPassphraseStore": 75, "encryptedPassphrase": 76, "encryptedCouchDBConnection": 77,
    "periodicReplication": 78, "periodicReplicationInterval": 79, "syncInternalFiles": 80,
    "syncInternalFilesBeforeReplication": 81, "syncInternalFilesInterval": 82, "syncInternalFilesIgnorePatterns": 83,
    "watchInternalFileChanges": 84, "suppressNotifyHiddenFilesChange": 85, "usePluginSync": 86,
    "usePluginSettings": 87, "showOwnPlugins": 88, "autoSweepPlugins": 89, "autoSweepPluginsPeriodic": 90,
    "notifyPluginOrSettingUpdated": 91, "deviceAndVaultName": 92, "usePluginSyncV2": 93, "usePluginEtc": 94,
    "pluginSyncExtendedSetting": 95, "useAdvancedMode": 96, "usePowerUserMode": 97, "useEdgeCaseMode": 98,
    "notifyThresholdOfRemoteStorageSize": 99, "disableWorkerForGeneratingChunks": 100,
    "processSmallFilesInUIThread": 101, "enableChunkSplitterV2": 102, "savingDelay": 103, "gcDelay": 104,
    "skipOlderFilesOnSync": 105, "useIndexedDBAdapter": 106, "enableDebugTools": 107, "writeLogToTheFile": 108,
    "settingSyncFile": 109, "writeCredentialsForSettingSync": 110, "notifyAllSettingSyncFile": 111,
    "suspendFileWatching": 112, "suspendParseReplicationResult": 113, "doNotSuspendOnFetching": 114,
    "versionUpFlash": 115, "settingVersion": 116, "isConfigured": 117, "lastReadUpdates": 118,
    "doctorProcessedVersion": 119, "P2P_Enabled": 120, "P2P_relays": 121, "P2P_roomID": 122,
    "P2P_passphrase": 123, "P2P_AutoAccepting": 124, "P2P_AutoStart": 125, "P2P_AutoBroadcast": 126,
    "P2P_AutoSyncPeers": 127, "P2P_AutoWatchPeers": 128, "P2P_SyncOnReplication": 129, "P2P_AppID": 130,
    "P2P_RebuildFrom": 131, "bucketCustomHeaders": 132, "couchDB_CustomHeaders": 133, "useJWT": 134,
    "jwtAlgorithm": 135, "jwtKey": 136, "jwtKid": 137, "jwtSub": 138, "jwtExpDuration": 139,
    "P2P_AutoAcceptingPeers": 140, "P2P_AutoDenyingPeers": 141, "syncInternalFilesTargetPatterns": 142,
    "useRequestAPI": 143, "hideFileWarningNotice": 144, "bucketPrefix": 145, "chunkSplitterVersion": 146,
    "E2EEAlgorithm": 147, "processSizeMismatchedFiles": 148, "forcePathStyle": 149, "P2P_turnServers": 150,
    "P2P_turnUsername": 151, "P2P_turnCredential": 152, "syncInternalFileOverwritePatterns": 153,
    "useOnlyLocalChunk": 154, "maxMTimeForReflectEvents": 155, "networkWarningStyle": 156,
    "remoteConfigurations": 157, "activeConfigurationId": 158, "P2P_ActiveRemoteConfigurationId": 159,
    "autoAcceptCompatibleTweak": 160, "allowSleepDuringSynchronisation": 161,
    "allowSleepDuringSynchronisationOnDesktop": 162, "P2P_maxWirePayloadBytes": 163,
    "P2P_connectionPath": 164, "encryptInternalMetadata": 165, "idDerivationVersion": 166,
    "idDerivationKey": 167, "pathNormalizationVersion": 168,
}

# SETTINGS_SCHEMA_DEFAULTS из livesync-commonlib (defaults для всех полей QR-массива).
_LIVESYNC_DEFAULTS = {
    "remoteType": "", "useCustomRequestHandler": False, "couchDB_URI": "", "couchDB_USER": "",
    "couchDB_PASSWORD": "", "couchDB_DBNAME": "", "liveSync": False, "syncOnSave": False,
    "syncOnStart": False, "savingDelay": 200, "lessInformationInLog": False, "gcDelay": 300,
    "versionUpFlash": "", "minimumChunkSize": 20, "longLineThreshold": 250, "showVerboseLog": False,
    "suspendFileWatching": False, "trashInsteadDelete": True, "periodicReplication": False,
    "periodicReplicationInterval": 60, "syncOnFileOpen": False, "encrypt": False, "passphrase": "",
    "idDerivationVersion": 0, "idDerivationKey": "", "pathNormalizationVersion": 0,
    "usePathObfuscation": False, "encryptInternalMetadata": False, "doNotDeleteFolder": False,
    "resolveConflictsByNewerFile": False, "batchSave": False, "batchSaveMinimumDelay": 5,
    "batchSaveMaximumDelay": 60, "deviceAndVaultName": "", "usePluginSettings": False, "showOwnPlugins": False,
    "showStatusOnEditor": True, "showStatusOnStatusbar": True, "showOnlyIconsOnEditor": False,
    "hideFileWarningNotice": False, "networkWarningStyle": "", "usePluginSync": False, "autoSweepPlugins": False,
    "autoSweepPluginsPeriodic": False, "notifyPluginOrSettingUpdated": False, "checkIntegrityOnSave": False,
    "batch_size": 25, "batches_limit": 25, "useHistory": False, "disableRequestURI": False,
    "skipOlderFilesOnSync": True, "checkConflictOnlyOnOpen": False, "showMergeDialogOnlyOnActive": False,
    "syncInternalFiles": False, "syncInternalFilesBeforeReplication": False,
    "syncInternalFilesIgnorePatterns": "\\/node_modules\\/, \\/\\.git\\/, \\/obsidian-livesync\\/",
    "syncInternalFilesTargetPatterns": "", "syncInternalFilesInterval": 60, "additionalSuffixOfDatabaseName": "",
    "ignoreVersionCheck": False, "lastReadUpdates": 0, "deleteMetadataOfDeletedFiles": False,
    "syncIgnoreRegEx": "", "syncOnlyRegEx": "", "customChunkSize": 0, "readChunksOnline": True,
    "watchInternalFileChanges": True, "automaticallyDeleteMetadataOfDeletedFiles": 0,
    "disableMarkdownAutoMerge": False, "writeDocumentsIfConflicted": False, "useDynamicIterationCount": False,
    "syncAfterMerge": False, "configPassphraseStore": "", "encryptedPassphrase": "",
    "encryptedCouchDBConnection": "", "permitEmptyPassphrase": False, "remoteConfigurations": {},
    "activeConfigurationId": "", "P2P_ActiveRemoteConfigurationId": "", "useIndexedDBAdapter": False,
    "useTimeouts": False, "writeLogToTheFile": False, "doNotPaceReplication": False,
    "hashCacheMaxCount": 300, "hashCacheMaxAmount": 50, "concurrencyOfReadChunksOnline": 40,
    "minimumIntervalOfReadChunksOnline": 50, "hashAlg": "xxhash64", "suspendParseReplicationResult": False,
    "doNotSuspendOnFetching": False, "useIgnoreFiles": False, "ignoreFiles": ".gitignore",
    "syncOnEditorSave": False, "allowSleepDuringSynchronisation": False,
    "allowSleepDuringSynchronisationOnDesktop": True, "pluginSyncExtendedSetting": {}, "syncMaxSizeInMB": 50,
    "settingSyncFile": "", "writeCredentialsForSettingSync": False, "notifyAllSettingSyncFile": False,
    "isConfigured": _UNDEF, "settingVersion": 10, "enableCompression": False, "accessKey": "",
    "bucket": "", "endpoint": "", "region": "auto", "secretKey": "", "useEden": False,
    "maxChunksInEden": 10, "maxTotalLengthInEden": 1024, "maxAgeInEden": 10,
    "disableCheckingConfigMismatch": False, "autoAcceptCompatibleTweak": _UNDEF, "displayLanguage": "",
    "enableChunkSplitterV2": False, "disableWorkerForGeneratingChunks": False,
    "processSmallFilesInUIThread": False, "notifyThresholdOfRemoteStorageSize": -1,
    "usePluginSyncV2": False, "usePluginEtc": False, "handleFilenameCaseSensitive": _UNDEF,
    "doNotUseFixedRevisionForChunks": True, "showLongerLogInsideEditor": False, "sendChunksBulk": False,
    "sendChunksBulkMaxSize": 1, "useSegmenter": False, "useAdvancedMode": False, "usePowerUserMode": False,
    "useEdgeCaseMode": False, "enableDebugTools": False, "suppressNotifyHiddenFilesChange": False,
    "syncMinimumInterval": 2000, "P2P_Enabled": False, "P2P_AutoAccepting": 0,
    "P2P_AppID": "self-hosted-livesync", "P2P_roomID": "", "P2P_passphrase": "",
    "P2P_relays": "wss://exp-relay.vrtmrz.net/", "P2P_AutoBroadcast": False, "P2P_AutoStart": False,
    "P2P_AutoSyncPeers": "", "P2P_AutoWatchPeers": "", "P2P_SyncOnReplication": "", "P2P_RebuildFrom": "",
    "P2P_AutoAcceptingPeers": "", "P2P_AutoDenyingPeers": "", "P2P_turnServers": "", "P2P_turnUsername": "",
    "P2P_turnCredential": "", "P2P_maxWirePayloadBytes": 15360, "P2P_connectionPath": "automatic",
    "doctorProcessedVersion": "", "bucketCustomHeaders": "", "couchDB_CustomHeaders": "", "useJWT": False,
    "jwtAlgorithm": "", "jwtKey": "", "jwtKid": "", "jwtSub": "", "jwtExpDuration": 5,
    "useRequestAPI": False, "bucketPrefix": "", "chunkSplitterVersion": "v3-rabin-karp",
    "E2EEAlgorithm": "v2", "processSizeMismatchedFiles": False, "forcePathStyle": True,
    "syncInternalFileOverwritePatterns": "", "useOnlyLocalChunk": False, "maxMTimeForReflectEvents": 0,
}

# Предпочтения self-hosted (PREFERRED_BASE + PREFERRED_SETTING_SELF_HOSTED) поверх дефолтов.
_LIVESYNC_PREFERRED = {
    "syncMaxSizeInMB": 50, "customChunkSize": 0, "chunkSplitterVersion": "v3-rabin-karp",
    "usePluginSyncV2": True, "handleFilenameCaseSensitive": False, "E2EEAlgorithm": "v2",
    "sendChunksBulkMaxSize": 1, "concurrencyOfReadChunksOnline": 30, "minimumIntervalOfReadChunksOnline": 25,
}

_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"
_OBJ_PREFIX = {"s": {1: "V", 2: "W", 3: "X", 4: "Y", 5: "Z"}, "o": {1: "v", 2: "w", 3: "x", 4: "y", 5: "z"}}
_NUM_PREFIX = {"n": {1: "a", 2: "b", 3: "c", 4: "d", 5: "e"}, "N": {1: "A", 2: "B", 3: "C", 4: "D", 5: "E"}}


def _b36(n: int) -> str:
    if n == 0:
        return "0"
    sign = "-" if n < 0 else ""
    n = abs(n)
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = _DIGITS[r] + out
    return sign + out


def _js_len(s: str) -> int:
    """Длина строки как в JavaScript (UTF-16 code units) — для префикса длины."""
    return len(s.encode("utf-16-le", "surrogatepass")) // 2


def _encode_any_array(values: list) -> str:
    """Порт octagonal-wheels encodeAnyArray: компактная сериализация массива."""
    out = []
    for v in values:
        if v is _UNDEF:
            out.append("u")
        elif v is None:
            out.append("n")
        elif v is False:
            out.append("f")
        elif v is True:
            out.append("t")
        elif isinstance(v, int) and not isinstance(v, bool):
            encoded = _b36(v)
            ln = _b36(len(encoded))
            out.append(_NUM_PREFIX["N"][len(ln)] + ln + encoded)
        else:
            prefix = "s" if isinstance(v, str) else "o"
            s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, separators=(",", ":"))
            ln = _b36(_js_len(s))
            out.append(_OBJ_PREFIX[prefix][len(ln)] + ln + s)
    return "".join(out)


def _case_bool(cc: dict) -> bool:
    return cc.get("case_sensitive") is True


def _livesync_settings(cc: dict) -> dict:
    """Полные настройки LiveSync, совместимые со StudyBot: без шифрования и обфускации."""
    s = dict(_LIVESYNC_DEFAULTS)
    s.update(_LIVESYNC_PREFERRED)
    s.update({
        "remoteType": "",
        "couchDB_URI": cc["url"],
        "couchDB_USER": cc["username"],
        "couchDB_PASSWORD": cc["password"],
        "couchDB_DBNAME": cc["database"],
        "encrypt": False,
        "usePathObfuscation": False,
        "encryptInternalMetadata": False,
        "isConfigured": True,
        "liveSync": True,
        "syncOnStart": True, "syncOnSave": True, "syncOnFileOpen": True, "syncAfterMerge": True,
        "batchSave": True, "periodicReplication": True,
        "handleFilenameCaseSensitive": _case_bool(cc),
        "remoteConfigurations": {}, "activeConfigurationId": "",
    })
    return s


def _json_text(value) -> str | None:
    """JSON-представление значения; None — как JSON.stringify(undefined) для _UNDEF."""
    if value is _UNDEF:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def make_qr_uri(cc: dict) -> str:
    """obsidian://setuplivesync?settingsQR=... — QR без пароля (самый быстрый путь)."""
    if not cc.get("url") or not cc.get("database"):
        raise ValueError("не заданы URL и имя базы CouchDB")
    if not cc.get("username"):
        raise ValueError("нет логина CouchDB")
    settings = _livesync_settings(cc)
    max_idx = max(i for i in _KEY_INDEX.values() if i >= 0)
    arr = [_UNDEF] * (max_idx + 1)
    defaults = [_UNDEF] * (max_idx + 1)
    for key, idx in _KEY_INDEX.items():
        if idx < 0:
            continue
        arr[idx] = settings.get(key, _UNDEF)
        defaults[idx] = _LIVESYNC_DEFAULTS.get(key, _UNDEF)
    # Хвост из значений по умолчанию не кодируем: плагин при импорте подставит
    # свои defaults для индексов за концом массива. Это 594 → 411 символов,
    # то есть QR 69x69 (73 колонки) вместо 85x85 (89 колонок) — влезает в SSH.
    last = max(i for i, v in enumerate(arr) if _json_text(v) != _json_text(defaults[i]))
    return SETUP_QR_BASE + quote(_encode_any_array(arr[:last + 1]), safe="-_.!~*'()")


def _hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    import hashlib
    import hmac as _hmac

    prk = _hmac.new(salt, ikm, hashlib.sha256).digest()
    okm, t, counter = b"", b"", 1
    while len(okm) < length:
        t = _hmac.new(prk, t + info + bytes([counter]), hashlib.sha256).digest()
        okm += t
        counter += 1
    return okm[:length]


def _livesync_encrypt(plaintext: str, passphrase: str) -> str:
    """Шифрование как encryptWithEphemeralSalt в livesync-commonlib: '%$' + base64(...).

    Бинарный макет: pbkdf2Salt(32) || iv(12) || hkdfSalt(32) || AES-256-GCM(данные + тег 16).
    Ключ: PBKDF2-SHA256(passphrase, pbkdf2Salt, 310000) → HKDF-SHA256(salt=hkdfSalt, info='').
    """
    import hashlib

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    pbkdf2_salt = secrets.token_bytes(32)
    iv = secrets.token_bytes(12)
    hkdf_salt = secrets.token_bytes(32)
    master = hashlib.pbkdf2_hmac("sha256", passphrase.encode(), pbkdf2_salt, 310000, dklen=32)
    key = _hkdf_sha256(master, hkdf_salt, b"", 32)
    ct = AESGCM(key).encrypt(iv, plaintext.encode(), None)
    return "%$" + base64.b64encode(pbkdf2_salt + iv + hkdf_salt + ct).decode()


def make_setup_uri(cc: dict, passphrase: str) -> str:
    """obsidian://setuplivesync?settings=... — парольный Setup URI (для «Use a setup URI»)."""
    if not cc.get("url") or not cc.get("database") or not cc.get("username"):
        raise ValueError("не заданы URL, имя базы или логин CouchDB")
    # Как «Copy settings as a new setup URI»: значения по умолчанию не отправляем
    # (получатель подставит свои), иначе ссылка вырастает с ~600 до ~6600 символов.
    settings = {k: v for k, v in _livesync_settings(cc).items()
                if _json_text(v) != _json_text(_LIVESYNC_DEFAULTS.get(k, "*"))}
    payload = json.dumps(settings, ensure_ascii=False, separators=(",", ":"))
    return SETUP_URI_BASE + quote(_livesync_encrypt(payload, passphrase), safe="-_.!~*'()")


def print_qr(text: str, ec: str = "L") -> None:
    """Печатает QR прямо в терминале — экран можно сканировать камерой по SSH."""
    import io

    import qrcode

    q = qrcode.QRCode(border=2, error_correction=getattr(qrcode.constants, f"ERROR_CORRECT_{ec}"))
    q.add_data(text)
    q.make(fit=True)
    buf = io.StringIO()
    q.print_ascii(out=buf, invert=True)
    # print_ascii уже рисует по одному модулю в ширину и два по высоте
    # (полублоки), поэтому строки выводим как есть, без обрезки.
    for line in buf.getvalue().splitlines():
        if line.strip():
            console.print(line.rstrip(), markup=False, highlight=False, soft_wrap=True)


def check_livesync_hint(cc: dict) -> None:
    """Предупреждает, если с телефона к базе не подключиться."""
    if (urlparse(cc.get("url") or "").hostname or "").lower() in _LOOPBACK:
        console.print("[yellow]URL CouchDB локальный — на телефоне такой адрес ничего не найдёт. "
                      "Для подключения с устройств нужен домен с HTTPS.[/yellow]")


def show_connect_qr(cc: dict) -> None:
    """QR-код и ссылки для подключения Obsidian к базе."""
    console.clear()
    try:
        uri = make_qr_uri(cc)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        pause()
        return
    console.print(Panel(
        "1. Установите в Obsidian плагин [bold]Self-hosted LiveSync[/bold]\n"
        "2. Отсканируйте QR камерой телефона — Obsidian откроет ссылку сам, либо\n"
        "   в мастере настройки плагина выберите [bold]Scan QR code[/bold]\n"
        "[dim]Настройки: без E2E-шифрования — иначе StudyBot не сможет писать в базу.[/dim]",
        title="Как подключиться", border_style="dim"))
    print_qr(uri)
    console.print(f"[dim]{len(uri)} символов[/dim]")
    check_livesync_hint(cc)
    hint("[t] вывести ссылку текстом  [u] Setup URI с парольной фразой  [0] назад")
    ch = Prompt.ask("Действие", default="0").lower()
    if ch == "t":
        console.print(uri, markup=False, highlight=False, soft_wrap=True)
        console.print("[dim]Откройте ссылку на телефоне — или в мастере настройки плагина "
                      "выберите «Scan QR code».[/dim]")
        pause()
    elif ch == "u":
        passphrase = Prompt.ask("Парольная фраза для Setup URI (передайте её другим каналом)",
                                default="", show_default=False, password=True)
        if passphrase:
            try:
                enc = make_setup_uri(cc, passphrase)
            except ImportError:
                console.print("[red]Нужен пакет cryptography — он ставится вместе со StudyBot.[/red]")
            else:
                console.print(enc, markup=False, highlight=False, soft_wrap=True)
                console.print("[dim]В Obsidian: мастер настройки → «Use a setup URI» → вставить → ввести фразу.[/dim]")
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
