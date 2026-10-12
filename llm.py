"""
Запрос к нейросети.

Один вход — ask(system, history) — и два формата API:
  * anthropic — Claude через официальный API
  * openai    — всё остальное: DeepSeek, Gemini, OpenAI, OpenRouter
                (у них одинаковый «OpenAI-совместимый» формат, отличается только адрес)
Какой выбран — решает LLM_PROVIDER в .env, см. config.PROVIDERS.

Без SDK, обычными HTTP-запросами: меньше зависимостей, проще чинить.
"""
import asyncio
import json
import logging

import aiohttp

from config import (
    LLM_API_FORMAT,
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MAX_TOKENS,
    LLM_MODEL,
    LLM_PROVIDER,
    LLM_TEMPERATURE,
)

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
RETRY_STATUSES = {429, 500, 502, 503, 504, 529}
TIMEOUT = aiohttp.ClientTimeout(total=60)

# Расход токенов последнего ответа — для test_bot.py: {"input", "cached", "output"}
LAST_USAGE: dict[str, int] = {}


class LLMError(RuntimeError):
    """Нейросеть не ответила. Текст — человеческий, его можно показать админу."""


class _Retryable(LLMError):
    """Временный сбой: перегрузка, лимит запросов, сеть. Пробуем ещё раз."""


async def ask(system: str, history: list[dict]) -> str:
    """history: [{"role": "user" | "assistant", "content": str}, ...], последним — клиент."""
    messages = _normalize(history)
    if not messages:
        raise LLMError("пустая история диалога")
    for attempt in range(2):
        try:
            if LLM_API_FORMAT == "anthropic":
                return await _anthropic(system, messages)
            return await _openai(system, messages)
        except _Retryable as e:
            logging.warning("Нейросеть временно недоступна (попытка %s): %s", attempt + 1, e)
            if attempt == 1:
                raise LLMError(f"временный сбой провайдера: {e}") from None
            await asyncio.sleep(3)
    raise LLMError("не удалось получить ответ")  # сюда не доходим, но пусть будет


def _normalize(history: list[dict]) -> list[dict]:
    """Диалог должен начинаться с клиента, а реплики — чередоваться.
    Подряд идущие сообщения одной стороны склеиваем."""
    out: list[dict] = []
    for m in history:
        role, content = m.get("role"), (m.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        if not out and role != "user":
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n\n" + content
        else:
            out.append({"role": role, "content": content})
    return out


async def _post(url: str, headers: dict, payload: dict) -> dict:
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                body = await resp.text()
                status = resp.status
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise _Retryable(f"нет связи: {e.__class__.__name__}") from None

    if status == 200:
        try:
            return json.loads(body)
        except ValueError:
            raise LLMError("провайдер прислал непонятный ответ") from None
    if status in RETRY_STATUSES:
        raise _Retryable(f"код {status}")
    raise LLMError(_human_error(status, body))


def _human_error(status: int, body: str) -> str:
    low = body.lower()
    if status in (401, 403):
        return f"ключ LLM_API_KEY не подходит к {LLM_PROVIDER} (неверный, отозван или от другого сервиса)"
    if "credit" in low or "balance" in low or "billing" in low or status == 402:
        return f"на балансе {LLM_PROVIDER} закончились деньги — пополни в кабинете провайдера"
    if status == 404 or "model_not_found" in low or "not_found_error" in low:
        return f"модель «{LLM_MODEL}» не найдена — проверь LLM_MODEL"
    return f"ошибка провайдера, код {status}: {body[:300]}"


async def _anthropic(system: str, messages: list[dict]) -> str:
    data = await _post(
        ANTHROPIC_URL,
        headers={
            "x-api-key": LLM_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        payload={
            "model": LLM_MODEL,
            "max_tokens": LLM_MAX_TOKENS,
            "temperature": LLM_TEMPERATURE,
            # cache_control: база знаний одинаковая в каждом запросе — кэш удешевляет повторы
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
        },
    )
    u = data.get("usage") or {}
    LAST_USAGE.clear()
    LAST_USAGE.update(input=u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                      + u.get("cache_creation_input_tokens", 0),
                      cached=u.get("cache_read_input_tokens", 0), output=u.get("output_tokens", 0))
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    if not text.strip():
        raise LLMError("нейросеть вернула пустой ответ")
    return text


async def _openai(system: str, messages: list[dict]) -> str:
    data = await _post(
        f"{LLM_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"},
        payload={
            "model": LLM_MODEL,
            "max_tokens": LLM_MAX_TOKENS,
            "temperature": LLM_TEMPERATURE,
            "messages": [{"role": "system", "content": system}, *messages],
        },
    )
    u = data.get("usage") or {}
    cached = (u.get("prompt_cache_hit_tokens")                                   # DeepSeek
              or (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)  # OpenAI, Gemini
    LAST_USAGE.clear()
    LAST_USAGE.update(input=u.get("prompt_tokens", 0), cached=cached, output=u.get("completion_tokens", 0))
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise LLMError("провайдер прислал ответ в неожиданном формате") from None
    if not text.strip():
        raise LLMError("нейросеть вернула пустой ответ")
    return text
