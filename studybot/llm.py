"""Клиент для любого OpenAI-совместимого API (DeepSeek, OpenRouter, OpenAI, локальные...)."""
from __future__ import annotations

import asyncio
import json
import logging
import re

import httpx

from . import config

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


def active_provider(cfg: dict | None = None) -> tuple[str, dict]:
    cfg = cfg or config.current()
    name = cfg["llm"]["active"]
    prov = cfg["llm"]["providers"].get(name)
    if not prov:
        raise LLMError(f"Провайдер «{name}» не найден в настройках")
    return name, prov


async def chat(
    messages: list[dict],
    *,
    provider: dict | None = None,
    json_mode: bool = False,
    max_tokens: int | None = None,
    temperature: float | None = None,
    timeout: float = 240,
) -> str:
    cfg = config.current()
    if provider is None:
        _, provider = active_provider(cfg)
    if not provider.get("api_key"):
        raise LLMError("Не задан API-ключ LLM")

    url = provider["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {provider['api_key']}"}
    payload: dict = {
        "model": provider["model"],
        "messages": messages,
        "temperature": cfg["llm"].get("temperature", 0.3) if temperature is None else temperature,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    last_err: str = ""
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(5):
            try:
                r = await client.post(url, headers=headers, json=payload)
            except httpx.HTTPError as e:
                last_err = f"{type(e).__name__}: {e}"
                await asyncio.sleep(3 * 2**attempt)
                continue
            if r.status_code == 400 and "response_format" in payload:
                # провайдер не умеет JSON-режим — просим JSON обычным текстом
                payload.pop("response_format")
                continue
            if r.status_code in (408, 429, 500, 502, 503, 504):
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                await asyncio.sleep(3 * 2**attempt)
                continue
            if r.status_code >= 400:
                raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
            try:
                data = r.json()
                content = data["choices"][0]["message"].get("content") or ""
            except (ValueError, KeyError, IndexError) as e:
                raise LLMError(f"Неожиданный ответ API: {r.text[:300]}") from e
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
            return content.strip()
    raise LLMError(f"API недоступно: {last_err}")


async def chat_json(messages: list[dict], **kw) -> dict:
    text = await chat(messages, json_mode=True, **kw)
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    raise LLMError(f"Модель вернула не JSON: {text[:200]}")


async def list_models(provider: dict) -> list[str]:
    url = provider["base_url"].rstrip("/") + "/models"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers={"Authorization": f"Bearer {provider['api_key']}"})
        r.raise_for_status()
        return sorted(m["id"] for m in r.json().get("data", []))
