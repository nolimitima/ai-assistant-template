"""
Instagram Direct через Instagram API with Instagram Login (Meta).

Принимаем сообщения на вебхук  /webhook/instagram,
отвечаем через  graph.instagram.com/<версия>/me/messages.
Настройка шаг за шагом — CHANNELS.md, раздел «Instagram».

Токен Instagram живёт 60 дней. Бот сам продлевает его раз в неделю и хранит
свежий в базе — менять .env каждые два месяца не нужно.
"""
import asyncio
import hashlib
import logging
import time

from aiohttp import web

import core
import db
from channels import meta
from config import INSTAGRAM_APP_SECRET, INSTAGRAM_TOKEN, META_GRAPH_VERSION

API = f"https://graph.instagram.com/{META_GRAPH_VERSION}"
MAX_BYTES = 1000            # лимит Instagram на одно сообщение
REFRESH_EVERY = 7 * 86400   # продлеваем токен раз в неделю
MEDIA_NAMES = {"image": "фото", "audio": "голосовое", "video": "видео", "file": "файл",
               "share": "репост", "story_mention": "упоминание в сторис", "ig_reel": "рилс",
               "reel": "рилс"}

_profile_cache: dict[str, tuple[str, str]] = {}
_last_refresh_alert = 0.0


# ---------- токен ----------

def _env_fingerprint() -> str:
    return hashlib.sha256(INSTAGRAM_TOKEN.encode()).hexdigest()[:16]


def token() -> str:
    """Актуальный токен: продлённый из базы, а если в .env вписали новый — новый."""
    saved = db.kv_get("ig_token")
    source = db.kv_get("ig_token_source")
    if saved and source and source[0] == _env_fingerprint():
        return saved[0]
    # первый запуск или токен в .env заменили — начинаем отсчёт заново
    db.kv_set("ig_token", INSTAGRAM_TOKEN)
    db.kv_set("ig_token_source", _env_fingerprint())
    return INSTAGRAM_TOKEN


async def refresh_token_if_due() -> None:
    global _last_refresh_alert
    token()  # синхронизируем с .env
    saved = db.kv_get("ig_token")
    if not saved or time.time() - saved[1] < REFRESH_EVERY:
        return
    try:
        status, data = await meta.graph(
            "GET", "https://graph.instagram.com/refresh_access_token", saved[0],
            params={"grant_type": "ig_refresh_token", "access_token": saved[0]})
    except Exception as e:
        status, data = 0, {"error": {"message": str(e)}}
    if status == 200 and data.get("access_token"):
        db.kv_set("ig_token", data["access_token"])
        logging.info("Instagram: токен продлён ещё на 60 дней")
        return
    logging.error("Instagram: не удалось продлить токен: %s", data)
    if time.time() - _last_refresh_alert > 86400:   # не чаще раза в сутки
        _last_refresh_alert = time.time()
        await core.notify_admins(
            "system:instagram",
            f"⚠️ <b>Instagram: не удалось продлить токен</b>\n{core.esc(meta.error_text(data))}\n\n"
            "Пока старый токен жив, бот работает. Если он истечёт — Instagram замолчит. "
            "Сгенерируй новый токен (CHANNELS.md → Instagram → шаг «Токен»), "
            "впиши в INSTAGRAM_TOKEN и перезапусти бота.")


async def refresh_loop() -> None:
    while True:
        try:
            await refresh_token_if_due()
        except Exception as e:
            logging.warning("Instagram: ошибка в продлении токена: %s", e)
        await asyncio.sleep(12 * 3600)


# ---------- отправка ----------

async def send_text(recipient_id: str, text: str) -> None:
    for chunk in core.split_text(text, MAX_BYTES, by_bytes=True):
        status, data = await meta.graph("POST", f"{API}/me/messages", token(), payload={
            "recipient": {"id": recipient_id},
            "message": {"text": chunk},
        })
        if status != 200:
            raise core.ChannelError(f"Instagram не принял сообщение: {meta.error_text(data)}")


async def _profile(igsid: str) -> tuple[str, str]:
    """Имя и username клиента для карточки менеджеру. Не получилось — не страшно."""
    if igsid in _profile_cache:
        return _profile_cache[igsid]
    name = username = ""
    try:
        status, data = await meta.graph("GET", f"{API}/{igsid}", token(),
                                        params={"fields": "name,username"})
        if status == 200:
            name, username = data.get("name", ""), data.get("username", "")
    except Exception:
        pass
    _profile_cache[igsid] = (name, username)
    return name, username


# ---------- приём ----------

async def _process(payload: dict) -> None:
    for entry in payload.get("entry", []):
        account_id = str(entry.get("id", ""))
        for ev in entry.get("messaging", []):
            msg = ev.get("message")
            if not msg or msg.get("is_echo") or msg.get("is_deleted"):
                continue  # наши же ответы, реакции, «прочитано» и т.п.
            sender = str((ev.get("sender") or {}).get("id", ""))
            if not sender or sender == account_id or core.seen(msg.get("mid", "")):
                continue
            name, username = await _profile(sender)
            client = core.Client("ig", sender, name, username)
            if msg.get("text"):
                coro = core.handle_text(client, msg["text"])
            else:
                att = (msg.get("attachments") or [{}])[0]
                kind = MEDIA_NAMES.get(att.get("type", ""), att.get("type") or "вложение")
                url = (att.get("payload") or {}).get("url", "")
                coro = core.handle_media(client, kind, url=url)
            await core.safe_handle(client, coro)


async def webhook_post(request: web.Request) -> web.Response:
    payload = await meta.read_signed(request, INSTAGRAM_APP_SECRET, "Instagram")
    if payload is None:
        return web.Response(status=403)
    task = asyncio.create_task(_process(payload))
    task.add_done_callback(_log_task_error)
    return web.Response(text="EVENT_RECEIVED")


def _log_task_error(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception():
        asyncio.create_task(core.report_error(task.exception(), "вебхук Instagram"))


def add_routes(app: web.Application) -> None:
    app.router.add_get("/webhook/instagram", meta.verify_get)
    app.router.add_post("/webhook/instagram", webhook_post)


# ---------- запуск и проверка ----------

async def describe(tok: str | None = None) -> tuple[bool, str]:
    try:
        status, data = await meta.graph("GET", f"{API}/me", tok or token(),
                                        params={"fields": "user_id,username"})
    except Exception as e:
        return False, f"нет связи с Meta: {e}"
    if status != 200:
        return False, meta.error_text(data)
    if isinstance(data.get("data"), list) and data["data"]:
        data = data["data"][0]
    return True, f"@{data.get('username', '?')}"


async def subscribe_app() -> str:
    """Подписываем аккаунт на сообщения — без этого вебхук молчит. Повтор безопасен."""
    try:
        status, data = await meta.graph("POST", f"{API}/me/subscribed_apps", token(),
                                        params={"subscribed_fields": "messages"})
    except Exception as e:
        return f"не удалось подписаться: {e}"
    return "подписка на сообщения активна" if status == 200 else f"подписка: {meta.error_text(data)}"


core.SENDERS["ig"] = send_text
