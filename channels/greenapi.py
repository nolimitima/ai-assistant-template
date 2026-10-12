"""
WhatsApp через Green API — подключение номера по QR-коду или коду привязки.

Как это работает: номер привязывается к Green API как «связанное устройство»
(как WhatsApp Web). Бот сам забирает новые сообщения из очереди Green API
(receiveNotification) — вебхуки и сервер с https для этого не нужны,
работает даже с ноутбука.

Это НЕОФИЦИАЛЬНЫЙ способ: WhatsApp может заблокировать номер. Подробно о рисках
и как их снизить — CHANNELS.md, раздел «WhatsApp через QR-код».

Документация Green API: green-api.com/docs
"""
import asyncio
import html
import json
import logging
import time

import aiohttp
from aiohttp import web

import core
import db
from config import (
    CONNECT_SECRET,
    PORT,
    PUBLIC_URL,
    GREENAPI_API_TOKEN,
    GREENAPI_API_URL,
    GREENAPI_ID_INSTANCE,
    WHATSAPP_PAUSE_ON_PHONE_REPLY,
)

BASE = f"{GREENAPI_API_URL}/waInstance{GREENAPI_ID_INSTANCE}"
MAX_TEXT = 4000
POLL_TIMEOUT = 20          # сколько секунд Green API ждёт новое сообщение (5–60)
MEDIA_NAMES = {"imageMessage": "фото", "audioMessage": "голосовое", "videoMessage": "видео",
               "documentMessage": "файл", "stickerMessage": "стикер",
               "locationMessage": "геолокация", "contactMessage": "контакт"}

_last_state_alert = 0.0


def _url(method: str, *tail: str) -> str:
    return "/".join([f"{BASE}/{method}", GREENAPI_API_TOKEN, *tail])


async def _call(http_method: str, method: str, *tail: str, payload: dict | None = None,
                params: dict | None = None, timeout: int = 30) -> tuple[int, object]:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
        async with s.request(http_method, _url(method, *tail), json=payload, params=params) as r:
            text = await r.text()
            try:
                return r.status, (json.loads(text) if text else None)
            except ValueError:
                return r.status, {"raw": text[:300]}


def _error_text(status: int, data) -> str:
    if status in (401, 403):
        return "неверный GREENAPI_ID_INSTANCE или GREENAPI_API_TOKEN (проверь в кабинете Green API)"
    if status == 466:
        return "закончился лимит тарифа Green API (бесплатный тариф — только 3 чата)"
    msg = data.get("message") if isinstance(data, dict) else str(data)
    return f"код {status}: {str(msg)[:200]}"


def chat_id_of(phone: str) -> str:
    return f"{phone}@c.us"


# ---------- отправка ----------

async def send_text(phone: str, text: str) -> None:
    for chunk in core.split_text(text, MAX_TEXT):
        try:
            status, data = await _call("POST", "sendMessage",
                                       payload={"chatId": chat_id_of(phone), "message": chunk})
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise core.ChannelError(f"нет связи с Green API: {e.__class__.__name__}") from None
        if status != 200:
            raise core.ChannelError(f"WhatsApp (Green API) не принял сообщение: {_error_text(status, data)}")


async def _download(url: str, name: str) -> tuple[bytes, str] | None:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
            async with s.get(url) as r:
                if r.status == 200:
                    return await r.read(), name or "file"
    except Exception as e:
        logging.warning("Green API: не скачал файл: %s", e)
    return None


# ---------- приём ----------

def _text_of(md: dict) -> str | None:
    kind = md.get("typeMessage")
    if kind == "textMessage":
        return (md.get("textMessageData") or {}).get("textMessage", "")
    if kind in ("extendedTextMessage", "quotedMessage"):
        return (md.get("extendedTextMessageData") or {}).get("text", "")
    if kind in ("buttonsResponseMessage", "templateButtonsReplyMessage", "listResponseMessage"):
        for key in ("buttonsResponseMessage", "templateButtonReplyMessage", "listResponseMessage"):
            data = md.get(key) or {}
            if data:
                return data.get("selectedButtonText") or data.get("selectedDisplayText") or data.get("title", "")
    return None


async def handle_notification(body: dict) -> None:
    kind = body.get("typeWebhook")

    if kind == "stateInstanceChanged":
        state = body.get("stateInstance", "")
        if state != "authorized":
            await _alert_state(state)
        return

    if kind not in ("incomingMessageReceived", "outgoingMessageReceived"):
        return  # статусы доставки, наши же сообщения через API (outgoingAPIMessageReceived) и т.п.

    sender = body.get("senderData") or {}
    chat = sender.get("chatId", "")
    if not chat.endswith("@c.us"):
        return  # группы, каналы, статусы — бот в них не отвечает
    if core.seen(body.get("idMessage", "")):
        return
    phone = chat.split("@", 1)[0]
    md = body.get("messageData") or {}
    text = _text_of(md)

    # Менеджер (или владелец) сам ответил клиенту с телефона — бот уступает
    if kind == "outgoingMessageReceived":
        key = f"wa:{phone}"
        db.upsert_user(key, "wa", phone)
        db.add_message(key, "assistant", f"(ответил менеджер с телефона) {text or '[файл]'}")
        if WHATSAPP_PAUSE_ON_PHONE_REPLY and db.get_mode(key) != "human":
            db.set_mode(key, "human")
            await core.notify_admins(
                key, f"📱 Ты ответил клиенту +{core.esc(phone)} с телефона — бот в этом чате "
                     f"на паузе, чтобы не перебивать.", kb=core.back_to_ai_kb(key))
        return

    client = core.Client("wa", phone, sender.get("senderName") or sender.get("senderContactName") or "")
    if text is not None:
        await core.safe_handle(client, core.handle_text(client, text))
        return
    file_data = md.get("fileMessageData") or {}
    doc = None
    if file_data.get("downloadUrl") and db.get_mode(client.key) == "human":
        doc = await _download(file_data["downloadUrl"], file_data.get("fileName", ""))
    kind_name = MEDIA_NAMES.get(md.get("typeMessage", ""), "сообщение")
    await core.safe_handle(client, core.handle_media(client, kind_name, document=doc))


async def _alert_state(state: str) -> None:
    global _last_state_alert
    if time.time() - _last_state_alert < 3600:
        return
    _last_state_alert = time.time()
    await core.notify_admins(
        "system:greenapi",
        f"⚠️ <b>WhatsApp отключился</b> (статус: {core.esc(state)}).\n"
        f"Клиенты пишут, но бот их не видит. Привяжи номер заново — команда /whatsapp.")


async def poll_loop() -> None:
    """Забираем сообщения из очереди Green API по одному, без вебхуков."""
    backoff = 5
    while True:
        try:
            status, data = await _call("GET", "receiveNotification",
                                       params={"receiveTimeout": POLL_TIMEOUT}, timeout=POLL_TIMEOUT + 15)
            if status != 200:
                msg = _error_text(status, data)
                if isinstance(data, dict) and "webhook" in str(data.get("message", "")).lower():
                    msg = ("в кабинете Green API указан адрес вебхука — очисти поле Webhook URL "
                           "у инстанса, иначе бот не может забирать сообщения")
                logging.error("Green API: %s", msg)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)
                continue
            backoff = 5
            if not data:
                continue  # за POLL_TIMEOUT секунд ничего не пришло
            receipt = data.get("receiptId")
            # сначала подтверждаем получение, потом обрабатываем: долгий ответ ИИ
            # не должен задерживать сообщения других клиентов
            if receipt is not None:
                await _call("DELETE", "deleteNotification", str(receipt))
            core.run_in_background(_safe(data.get("body") or {}))
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logging.warning("Green API: нет связи (%s), пробую снова", e.__class__.__name__)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)
        except Exception as e:
            await core.report_error(e, "приём сообщений Green API")
            await asyncio.sleep(10)


async def _safe(body: dict) -> None:
    try:
        await handle_notification(body)
    except Exception as e:
        await core.report_error(e, "обработка сообщения Green API")


# ---------- статус и привязка номера ----------

async def state() -> str:
    """authorized — номер привязан; notAuthorized — нужно отсканировать QR; и т.п."""
    status, data = await _call("GET", "getStateInstance", timeout=15)
    if status != 200:
        raise core.ChannelError(_error_text(status, data))
    return (data or {}).get("stateInstance", "unknown")


async def describe() -> tuple[bool, str]:
    try:
        st = await state()
    except Exception as e:
        return False, str(e)
    if st == "authorized":
        return True, "номер привязан (Green API, QR)"
    return False, f"номер НЕ привязан (статус {st}) — привяжи по ссылке из /whatsapp"


def connect_path() -> str:
    return f"/connect/{CONNECT_SECRET}"


def connect_link() -> str:
    """Страница привязки номера. На сервере — ссылка для владельца номера,
    на своём компьютере — открыть самому в браузере."""
    return (PUBLIC_URL or f"http://localhost:{PORT}") + connect_path()


async def connect_page(request: web.Request) -> web.Response:
    if request.match_info.get("secret") != CONNECT_SECRET:
        raise web.HTTPNotFound()
    return web.Response(text=CONNECT_HTML.replace("{{BASE}}", html.escape(connect_path())),
                        content_type="text/html")


async def connect_qr(request: web.Request) -> web.Response:
    if request.match_info.get("secret") != CONNECT_SECRET:
        raise web.HTTPNotFound()
    try:
        st = await state()
        if st == "authorized":
            return web.json_response({"state": "authorized"})
        status, data = await _call("GET", "qr", timeout=20)
        data = data if isinstance(data, dict) else {}
        if data.get("type") == "qrCode":
            return web.json_response({"state": st, "qr": data.get("message", "")})
        if data.get("type") == "alreadyLogged":
            return web.json_response({"state": "authorized"})
        return web.json_response({"state": st, "error": data.get("message") or "QR пока недоступен"})
    except Exception as e:
        return web.json_response({"state": "error", "error": str(e)})


async def connect_code(request: web.Request) -> web.Response:
    """Код привязки по номеру телефона — удобно, если QR сканировать нечем."""
    if request.match_info.get("secret") != CONNECT_SECRET:
        raise web.HTTPNotFound()
    form = await request.post()
    digits = "".join(ch for ch in str(form.get("phone", "")) if ch.isdigit())
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if not 10 <= len(digits) <= 15:
        return web.json_response({"error": "Номер нужен с кодом страны, например 77011234567"})
    try:
        status, data = await _call("POST", "getAuthorizationCode",
                                   payload={"phoneNumber": int(digits)}, timeout=30)
    except Exception as e:
        return web.json_response({"error": str(e)})
    data = data if isinstance(data, dict) else {}
    if status == 200 and data.get("code"):
        return web.json_response({"code": data["code"]})
    return web.json_response({"error": _error_text(status, data)})


def add_routes(app: web.Application) -> None:
    app.router.add_get("/connect/{secret}", connect_page)
    app.router.add_get("/connect/{secret}/qr", connect_qr)
    app.router.add_post("/connect/{secret}/code", connect_code)


core.SENDERS["wa"] = send_text


CONNECT_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Подключение WhatsApp</title>
<style>
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:520px;margin:32px auto;padding:0 16px;color:#1d1d1f}
 h1{font-size:22px} .box{border:1px solid #ddd;border-radius:12px;padding:16px;margin:16px 0}
 img{width:260px;height:260px;display:block;margin:8px auto} .ok{color:#1a7f37;font-weight:600}
 .muted{color:#666;font-size:14px} input{font-size:16px;padding:8px;width:60%} button{font-size:16px;padding:8px 14px}
 .code{font-size:28px;letter-spacing:4px;font-weight:700;text-align:center;margin:12px 0}
</style></head><body>
<h1>Подключение WhatsApp к ассистенту</h1>
<div id="status" class="muted">Проверяю статус…</div>
<div class="box" id="qrbox" hidden>
 <b>С компьютера — по QR-коду</b>
 <p class="muted">На телефоне: WhatsApp → Настройки → Связанные устройства → Привязка устройства.
 Наведи камеру на код. Код обновляется сам.</p>
 <img id="qr" alt="QR-код">
</div>
<div class="box" id="codebox" hidden>
 <b>С телефона — по коду</b>
 <p class="muted">Введи номер WhatsApp с кодом страны. Потом на телефоне: Связанные устройства →
 Привязка устройства → «Связать по номеру телефона» и введи код.</p>
 <input id="phone" placeholder="77011234567"> <button onclick="getCode()">Получить код</button>
 <div id="code" class="code"></div>
</div>
<p class="muted">Если в WhatsApp Business включены автоответы (приветствие, «нет на месте») — выключи их:
они уходят с твоего номера, и бот решит, что ответил менеджер, и замолчит в этом чате.</p>
<script>
const base = "{{BASE}}";
async function refresh(){
  try{
    const r = await fetch(base + "/qr"); const d = await r.json();
    const st = document.getElementById("status");
    if(d.state === "authorized"){
      st.innerHTML = '<span class="ok">✅ WhatsApp подключён.</span> Эту страницу можно закрыть.';
      document.getElementById("qrbox").hidden = true; document.getElementById("codebox").hidden = true; return;
    }
    document.getElementById("codebox").hidden = false;
    if(d.qr){ document.getElementById("qrbox").hidden = false;
      document.getElementById("qr").src = "data:image/png;base64," + d.qr; st.textContent = "Номер не подключён"; }
    else { st.textContent = d.error || "Жду QR-код…"; }
  }catch(e){ document.getElementById("status").textContent = "Нет связи с ботом, пробую снова…"; }
  setTimeout(refresh, 5000);
}
async function getCode(){
  const f = new FormData(); f.append("phone", document.getElementById("phone").value);
  const r = await fetch(base + "/code", {method:"POST", body:f}); const d = await r.json();
  document.getElementById("code").textContent = d.code || ("⚠️ " + (d.error || "не получилось"));
}
refresh();
</script></body></html>"""
