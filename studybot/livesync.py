"""Запись файлов прямо в CouchDB в формате плагина Obsidian Self-hosted LiveSync.

Формат (без шифрования):
  * документ файла: {_id: <путь>, path, children: [id чанков], ctime, mtime, size,
                     type: "plain" (текст) | "newnote" (бинарный, чанки в base64), eden: {}}
  * чанк:           {_id: "h:<хеш>", type: "leaf", data: <строка>}

Ограничения: E2E-шифрование и обфускация путей в LiveSync должны быть выключены —
иначе писать напрямую нельзя (бот это обнаружит и будет сохранять только локально).
"""
from __future__ import annotations

import base64
import hashlib
import logging
import time
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

TEXT_EXT = (".md", ".txt", ".canvas")
TEXT_CHUNK = 50_000          # символов на чанк для текста
BINARY_CHUNK = 3 * 34_000    # байт на чанк (кратно 3 — base64 без паддинга посередине)


class LiveSyncError(RuntimeError):
    pass


class LiveSync:
    def __init__(self, cc: dict):
        self.cc = cc
        self.base = cc["url"].rstrip("/") + "/" + quote(cc["database"], safe="")
        auth = (cc["username"], cc["password"]) if cc.get("username") else None
        self.http = httpx.AsyncClient(auth=auth, timeout=120)
        self.case_sensitive: bool | None = None
        self.info: dict = {}

    async def close(self) -> None:
        await self.http.aclose()

    # --- проверка/автоопределение настроек LiveSync ---
    async def detect(self) -> dict:
        r = await self.http.get(self.base)
        if r.status_code == 401:
            raise LiveSyncError("CouchDB: неверный логин/пароль")
        if r.status_code == 404:
            raise LiveSyncError(f"CouchDB: база «{self.cc['database']}» не найдена")
        r.raise_for_status()
        db_info = r.json()

        encrypted = obfuscated = False
        cs_votes: list[bool] = []

        # 1) настройки, которые LiveSync сам кладёт в milestone-документ
        r = await self.http.get(self.base + "/_local/obsydian_livesync_milestone")
        if r.status_code == 200:
            tweaks = r.json().get("tweak_values") or {}
            for v in tweaks.values():
                if not isinstance(v, dict):
                    continue
                encrypted |= bool(v.get("encrypt"))
                obfuscated |= bool(v.get("usePathObfuscation"))
                if "handleFilenameCaseSensitive" in v:
                    cs_votes.append(bool(v["handleFilenameCaseSensitive"]))

        # 2) по самим документам
        r = await self.http.post(self.base + "/_find", json={
            "selector": {"type": {"$in": ["plain", "newnote", "notes"]}},
            "fields": ["_id", "path"], "limit": 300,
        })
        if r.status_code == 200:
            for d in r.json().get("docs", []):
                did, path = d.get("_id", ""), d.get("path")
                if did.startswith("f:"):
                    obfuscated = True
                if not isinstance(path, str) or path == path.lower():
                    continue
                pid = "/" + path if path.startswith("_") else path
                if did == pid:
                    cs_votes.append(True)
                elif did == pid.lower():
                    cs_votes.append(False)
        r = await self.http.post(self.base + "/_find", json={
            "selector": {"type": "leaf"}, "fields": ["_id"], "limit": 20,
        })
        if r.status_code == 200:
            if any(d.get("_id", "").startswith("h:+") for d in r.json().get("docs", [])):
                encrypted = True

        mode = str(self.cc.get("case_sensitive", "auto")).lower()
        if mode in ("true", "1", "yes"):
            self.case_sensitive = True
        elif mode in ("false", "0", "no"):
            self.case_sensitive = False
        elif cs_votes:
            self.case_sensitive = max(set(cs_votes), key=cs_votes.count)
        else:
            self.case_sensitive = False  # дефолт LiveSync: регистр не учитывается (id в нижнем регистре)

        self.info = {
            "docs": db_info.get("doc_count"),
            "encrypted": encrypted,
            "obfuscated": obfuscated,
            "case_sensitive": self.case_sensitive,
        }
        if encrypted or obfuscated:
            raise LiveSyncError(
                "В LiveSync включено E2E-шифрование или обфускация путей — бот не может писать в такую базу. "
                "Отключите их в настройках плагина или пользуйтесь локальным сохранением."
            )
        return self.info

    async def _ensure(self) -> None:
        if self.case_sensitive is None:
            await self.detect()

    def doc_id(self, path: str) -> str:
        x = path if self.case_sensitive else path.lower()
        return "/" + x if x.startswith("_") else x

    def _url(self, path: str) -> str:
        return self.base + "/" + quote(self.doc_id(path), safe="")

    async def _get(self, path: str) -> dict | None:
        r = await self.http.get(self._url(path))
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    async def exists(self, path: str) -> bool:
        await self._ensure()
        doc = await self._get(path)
        return bool(doc) and not doc.get("deleted") and not doc.get("_deleted")

    async def write(self, path: str, data: bytes, overwrite: bool = False) -> None:
        await self._ensure()
        is_text = path.lower().endswith(TEXT_EXT)
        if is_text:
            s = data.decode("utf-8")
            pieces = [s[i:i + TEXT_CHUNK] for i in range(0, len(s), TEXT_CHUNK)]
        else:
            pieces = [base64.b64encode(data[i:i + BINARY_CHUNK]).decode("ascii")
                      for i in range(0, len(data), BINARY_CHUNK)]

        children, leaves = [], {}
        for p in pieces:
            cid = "h:" + hashlib.sha256(p.encode("utf-8")).hexdigest()[:32]
            children.append(cid)
            leaves[cid] = {"_id": cid, "type": "leaf", "data": p}

        # чанки пачками (одинаковый чанк уже может быть в базе — conflict это нормально)
        batch, size = [], 0
        for leaf in leaves.values():
            batch.append(leaf)
            size += len(leaf["data"])
            if size > 4_000_000:
                await self._bulk(batch)
                batch, size = [], 0
        if batch:
            await self._bulk(batch)

        now = int(time.time() * 1000)
        doc = {
            "_id": self.doc_id(path),
            "path": path,
            "children": children,
            "ctime": now,
            "mtime": now,
            "size": len(data),
            "type": "plain" if is_text else "newnote",
            "eden": {},
        }
        existing = await self._get(path)
        if existing:
            if not overwrite and not existing.get("deleted"):
                raise FileExistsError(path)
            doc["_rev"] = existing["_rev"]
            if not existing.get("deleted"):
                doc["ctime"] = existing.get("ctime", now)
        r = await self.http.put(self._url(path), json=doc)
        if r.status_code >= 400:
            raise LiveSyncError(f"CouchDB {r.status_code}: {r.text[:200]}")

    async def _bulk(self, docs: list[dict]) -> None:
        r = await self.http.post(self.base + "/_bulk_docs", json={"docs": docs})
        if r.status_code >= 400:
            raise LiveSyncError(f"CouchDB {r.status_code}: {r.text[:200]}")
        for res in r.json():
            if res.get("error") and res["error"] != "conflict":
                raise LiveSyncError(f"CouchDB: {res}")
