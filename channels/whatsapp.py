"""
WhatsApp через официальный WhatsApp Cloud API (Meta).

Принимаем сообщения на вебхук  /webhook/whatsapp,
отвечаем через  graph.facebook.com/<версия>/<PHONE_NUMBER_ID>/messages.
Настройка шаг за шагом — CHANNELS.md, раздел «WhatsApp».
"""
import asyncio
import logging

import aiohttp
from aiohttp import web

import core
from channels import meta
from config import (
    META_GRAPH_VERSION,
    WHATSAPP_APP_SECRET,
    WHATSAPP_BUSINESS_ACCOUNT_ID,
    WHATSAPP_PHONE_NUMBER_ID,
    WHATSAPP_TOKEN,
)

API = f"https://graph.facebook.com/{META_GRAPH_VERSION}"
MAX_TEXT = 4000
MEDIA_NAMES = {"image": "фото", "audio": "голосовое / аудио", "video": "видео",
               "document": "файл", "sticker": "стикер", "location": "геолокация",
               "contacts": "контакт"}


# ---------- отправка ----------

async def send_text(to: str, text: str) -> None:
    for chunk in core.split_text(text, MAX_TEXT):
        status, data = await meta.graph("POST", f"{API}/{WHATSAPP_PHONE_NUMBER_ID}/messages",
                                        WHATSAPP_TOKEN, payload={
                                            "messaging_product": "whatsapp",
                                            "recipient_type": "individual",
                                            "to": to,
                                            "type": "text",
                                            "text": {"preview_url": False, "body": chunk},
                                        })
        if status != 200:
            raise core.ChannelError(f"WhatsApp не принял сообщение: {meta.error_text(data)}")


async def _mark_read(message_id: str) -> None:
    """Синие галочки у клиента — видно, что сообщение дошло. Не критично."""
    try:
        await meta.graph("POST", f"{API}/{WHATSAPP_PHONE_NUMBER_ID}/messages", WHATSAPP_TOKEN,
                         payload={"messaging_product": "whatsapp", "status": "read",
                                  "message_id": message_id})
    except Exception:
        pass


async def _download_media(media_id: str) -> tuple[bytes, str] | None:
    """Фото/голосовые клиента — скачиваем, чтобы переслать менеджеру в Telegram."""
    try:
        status, info = await meta.graph("GET", f"{API}/{media_id}", WHATSAPP_TOKEN)
        if status != 200 or not info.get("url"):
            return None
        async with aiohttp.ClientSession(timeout=meta.TIMEOUT) as s:
            async with s.get(info["url"], headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}) as r:
                if r.status != 200:
                    return None
                data = await r.read()
        ext = (info.get("mime_type", "").split("/")[-1].split(";")[0] or "bin")
        return data, f"whatsapp_{media_id}.{ext}"
    except Exception as e:
        logging.warning("WhatsApp: не скачал медиа %s: %s", media_id, e)
        return None


# ---------- приём ----------

async def _process(payload: dict) -> None:
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value") or {}
            if value.get("metadata", {}).get("phone_number_id") != WHATSAPP_PHONE_NUMBER_ID:
                continue  # сообщение для другого номера этого же приложения
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name", "")
                     for c in value.get("contacts", [])}

            for msg in value.get("messages", []):
                if core.seen(msg.get("id", "")):
                    continue
                sender = msg.get("from", "")
                client = core.Client("wa", sender, names.get(sender, ""))
                asyncio.create_task(_mark_read(msg.get("id", "")))
                kind = msg.get("type")
                if kind == "text":
                    coro = core.handle_text(client, (msg.get("text") or {}).get("body", ""))
                elif kind == "button":
                    coro = core.handle_text(client, (msg.get("button") or {}).get("text", ""))
                elif kind == "interactive":
                    inter = msg.get("interactive") or {}
                    reply = inter.get("button_reply") or inter.get("list_reply") or {}
                    coro = core.handle_text(client, reply.get("title", ""))
                else:
                    media = msg.get(kind) or {}
                    doc = await _download_media(media["id"]) if media.get("id") else None
                    coro = core.handle_media(client, MEDIA_NAMES.get(kind, kind or "сообщение"),
                                             document=doc)
                await core.safe_handle(client, coro)

            # сообщение не доставлено (например, закрылось окно 24 часа) — говорим менеджеру
            for st in value.get("statuses", []):
                if st.get("status") != "failed":
                    continue
                errors = st.get("errors") or [{}]
                reason = meta.error_text({"error": {"code": errors[0].get("code"),
                                                    "message": errors[0].get("title", "")}})
                key = f"wa:{st.get('recipient_id', '')}"
                await core.notify_admins(key, f"❗️ Сообщение в WhatsApp не доставлено "
                                              f"(+{core.esc(st.get('recipient_id', ''))}): {core.esc(reason)}")


async def webhook_post(request: web.Request) -> web.Response:
    payload = await meta.read_signed(request, WHATSAPP_APP_SECRET, "WhatsApp")
    if payload is None:
        return web.Response(status=403)
    # отвечаем Meta сразу, а сообщение обрабатываем в фоне — иначе Meta решит,
    # что мы не ответили, и пришлёт его ещё раз
    task = asyncio.create_task(_process(payload))
    task.add_done_callback(_log_task_error)
    return web.Response(text="EVENT_RECEIVED")


def _log_task_error(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception():
        asyncio.create_task(core.report_error(task.exception(), "вебхук WhatsApp"))


def add_routes(app: web.Application) -> None:
    app.router.add_get("/webhook/whatsapp", meta.verify_get)
    app.router.add_post("/webhook/whatsapp", webhook_post)


# ---------- запуск и проверка ----------

async def describe() -> tuple[bool, str]:
    """Для отчёта при запуске и check_setup: (всё ок?, текст)."""
    try:
        status, data = await meta.graph(
            "GET", f"{API}/{WHATSAPP_PHONE_NUMBER_ID}", WHATSAPP_TOKEN,
            params={"fields": "display_phone_number,verified_name,quality_rating"})
    except Exception as e:
        return False, f"нет связи с Meta: {e}"
    if status != 200:
        return False, meta.error_text(data)
    return True, f"{data.get('display_phone_number', '?')} ({data.get('verified_name', '')})"


async def subscribe_app() -> str:
    """Подписываем приложение на WhatsApp-аккаунт, чтобы сообщения шли на вебхук.
    Повторный вызов безопасен. Нужен WHATSAPP_BUSINESS_ACCOUNT_ID."""
    if not WHATSAPP_BUSINESS_ACCOUNT_ID:
        return "WHATSAPP_BUSINESS_ACCOUNT_ID не задан — подписку проверь вручную (CHANNELS.md)"
    try:
        status, data = await meta.graph("POST", f"{API}/{WHATSAPP_BUSINESS_ACCOUNT_ID}/subscribed_apps",
                                        WHATSAPP_TOKEN)
    except Exception as e:
        return f"не удалось подписаться: {e}"
    return "подписка на сообщения активна" if status == 200 else f"подписка: {meta.error_text(data)}"


core.SENDERS["wa"] = send_text
