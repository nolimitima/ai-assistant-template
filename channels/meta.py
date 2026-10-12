"""Общее для WhatsApp и Instagram: оба приходят от Meta одинаковым способом.

Как это работает:
  1. При настройке Meta один раз дёргает GET на наш адрес с паролем (verify token)
     и ждёт в ответ «challenge» — так проверяет, что адрес наш.
  2. Дальше каждое сообщение клиента приходит POST-запросом, подписанным
     секретом приложения (заголовок X-Hub-Signature-256). Чужие запросы отбрасываем.
"""
import hashlib
import hmac
import json
import logging

import aiohttp
from aiohttp import web

from config import META_VERIFY_TOKEN

TIMEOUT = aiohttp.ClientTimeout(total=30)


def verify_get(request: web.Request) -> web.Response:
    """Проверка адреса вебхука при настройке в Meta."""
    q = request.query
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == META_VERIFY_TOKEN:
        logging.info("Meta подтвердила вебхук %s", request.path)
        return web.Response(text=q.get("hub.challenge", ""))
    logging.warning("Неудачная проверка вебхука %s: неверный verify token", request.path)
    return web.Response(status=403, text="wrong verify token")


def signature_ok(secret: str, body: bytes, header: str) -> bool:
    if not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):])


async def read_signed(request: web.Request, secret: str, channel: str) -> dict | None:
    """Тело POST-запроса, если подпись верная. Иначе None."""
    body = await request.read()
    if not signature_ok(secret, body, request.headers.get("X-Hub-Signature-256", "")):
        logging.warning(
            "%s: подпись запроса не совпала — запрос отброшен. Если так со всеми сообщениями, "
            "в .env указан не тот секрет приложения (см. CHANNELS.md).", channel)
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


async def graph(method: str, url: str, token: str, *, payload: dict | None = None,
                params: dict | None = None) -> tuple[int, dict]:
    """Запрос к Graph API. Возвращает (код ответа, тело)."""
    headers = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async with session.request(method, url, headers=headers, json=payload, params=params) as resp:
            text = await resp.text()
            try:
                data = json.loads(text) if text else {}
            except ValueError:
                data = {"raw": text[:300]}
            return resp.status, data


def error_text(data: dict) -> str:
    """Ошибка Meta человеческими словами, насколько получится."""
    err = data.get("error") or {}
    code = err.get("code")
    msg = err.get("error_user_msg") or err.get("message") or str(data)[:300]
    if code == 190:
        return "токен доступа недействителен или истёк — нужен новый (см. CHANNELS.md)"
    if code in (10, 200) or "permission" in str(msg).lower():
        return f"у токена нет нужного разрешения: {msg}"
    if code == 131047:
        return "прошло больше 24 часов с последнего сообщения клиента"
    if code == 131030:
        return "этого номера нет в списке разрешённых для тестового номера WhatsApp"
    return f"код {code}: {msg}"
