"""
Общая логика ассистента — одинаковая для Telegram, WhatsApp и Instagram.

Каналы (tg.py, channels/whatsapp.py, channels/instagram.py) только принимают
сообщения и умеют отправлять текст. Всё остальное — здесь:
ответ ИИ, заявки, передача менеджеру, лимиты.

Менеджеры для всех каналов сидят в Telegram: туда приходят карточки,
оттуда через Reply уходят ответы клиентам — в тот канал, откуда клиент пришёл.
"""
import asyncio
import html
import logging
import re
import time
import traceback
from collections import OrderedDict
from dataclasses import dataclass
from typing import Awaitable, Callable

import aiohttp
from aiogram import Bot
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

import db
import knowledge
import llm
from config import (
    ADMIN_IDS,
    DAILY_LIMIT,
    HISTORY_LIMIT,
    LEAD_WEBHOOK_URL,
    REPLY_DELAY_SECONDS,
    SUPPORT_CONTACT,
    WA_META,
)

CHANNEL_NAMES = {"tg": "Telegram", "wa": "WhatsApp", "ig": "Instagram"}
CHANNEL_ICONS = {"tg": "✈️", "wa": "🟢", "ig": "📸"}
WINDOW_HOURS = 24          # Meta: писать клиенту можно 24 ч после его сообщения
# Где действует окно 24 часа: Instagram всегда, WhatsApp — только официальный (Meta).
# WhatsApp через Green API — обычный аккаунт, окна нет.
WINDOW_CHANNELS = {"ig"} | ({"wa"} if WA_META else set())
MAX_USER_TEXT = 3000
HUMAN_BUTTON = "👤 Позвать человека"   # кнопка в Telegram

# Метки, которыми ИИ сообщает о заявке и о передаче человеку (см. knowledge.SERVICE_RULES)
MARKER = re.compile(r"\[\[(ЗАЯВКА|ОПЕРАТОР)\]\][ \t]*([^\n]*)")
MARKER_NEXT_LINE = re.compile(r"\[\[(ЗАЯВКА|ОПЕРАТОР)\]\][ \t]*\n[ \t]*([^\n\[]+)")
# Похоже на телефон: 10–15 цифр с пробелами, дефисами, скобками («8 701 123-45-67», «+77011234567»).
# Цены вроде «150 000 ₸» сюда не попадают — в них меньше цифр.
PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{8,18}\d")
MISSED_LEAD_CHECK_MINUTES = 10   # через сколько минут сказать менеджеру о телефоне без заявки
# В WhatsApp и Instagram кнопки нет — клиент может просто написать одно из этих слов
HUMAN_WORDS = re.compile(
    r"^\s*(позови(те)?\s+)?(менеджер[а-я]*|оператор[а-я]*|человек[а-я]*|живого человека)\s*[!.?]*\s*$",
    re.IGNORECASE,
)

BOT: Bot | None = None                  # Telegram-бот, ставится в bot.py при запуске
KB = knowledge.load()                   # перечитывается командой /reload
# канал → функция отправки текста клиенту: async (id, text) -> None
SENDERS: dict[str, Callable[[str, str], Awaitable[None]]] = {}

SUPPORT_LINE = (f"Если что-то пошло не так — напиши: {SUPPORT_CONTACT}" if SUPPORT_CONTACT else "")


# Ссылки на фоновые задачи: без них asyncio может удалить задачу, не дождавшись конца
_BACKGROUND: set[asyncio.Task] = set()


def run_in_background(coro) -> None:
    """Запуск задачи в фоне. Её ошибки не ломают диалог, но попадают в лог."""
    task = asyncio.create_task(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_background_done)


def _background_done(task: asyncio.Task) -> None:
    _BACKGROUND.discard(task)
    if not task.cancelled() and task.exception():
        logging.error("Фоновая задача упала", exc_info=task.exception())


class ChannelError(RuntimeError):
    """Сообщение не ушло клиенту. Текст — человеческий, его видит менеджер."""


@dataclass
class Client:
    channel: str            # tg | wa | ig
    id: str                 # chat id / номер телефона / Instagram-scoped id
    name: str = ""
    username: str = ""

    @property
    def key(self) -> str:
        return f"{self.channel}:{self.id}"

    @property
    def is_admin(self) -> bool:
        return self.channel == "tg" and self.id.lstrip("-").isdigit() and int(self.id) in ADMIN_IDS

    def card(self) -> str:
        """Подпись клиента для карточки менеджеру (HTML)."""
        parts = [f"{CHANNEL_ICONS.get(self.channel, '')} {CHANNEL_NAMES.get(self.channel, self.channel)}"]
        if self.name:
            parts.append(esc(self.name))
        if self.channel == "wa":
            parts.append(f"+{esc(self.id)}")
        elif self.username:
            parts.append(f"@{esc(self.username)}")
        if self.channel != "wa":
            parts.append(f"id {esc(self.id)}")
        return " · ".join(parts)

    def human_hint(self) -> str:
        return f"нажми «{HUMAN_BUTTON}»" if self.channel == "tg" else "напиши «менеджер»"


def client_from_key(key: str) -> Client:
    channel, ext_id = key.split(":", 1)
    row = db.get_user(key)
    return Client(channel, ext_id, (row["name"] or "") if row else "", (row["username"] or "") if row else "")


def esc(text: str) -> str:
    return html.escape(text or "")


# ---------- текст ----------

def parse_reply(raw: str) -> tuple[str, str | None, str | None]:
    """Ответ ИИ → (текст для клиента, заявка | None, причина позвать человека | None)."""
    lead = operator = None
    # ИИ иногда пишет данные не после метки, а следующей строкой — забираем и её
    raw = MARKER_NEXT_LINE.sub(lambda m: f"[[{m.group(1)}]] {m.group(2).strip()}", raw)
    for m in MARKER.finditer(raw):
        payload = m.group(2).strip()
        if m.group(1) == "ЗАЯВКА":
            lead = payload or "без деталей"
        else:
            operator = payload or "без причины"
    clean = MARKER.sub("", raw)
    clean = clean.replace("**", "")
    clean = re.sub(r"^#{1,6}\s*", "", clean, flags=re.MULTILINE)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean, lead, operator


def split_text(text: str, limit: int, by_bytes: bool = False) -> list[str]:
    """Режем длинный текст на сообщения: Telegram ~4000 символов, WhatsApp 4096,
    Instagram — 1000 байт (кириллица весит 2 байта, это ~500 букв)."""
    size = (lambda s: len(s.encode("utf-8"))) if by_bytes else len
    chunks: list[str] = []
    text = text.strip()
    while text and size(text) > limit:
        lo, hi = 1, len(text)            # самый длинный префикс, который влезает
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if size(text[:mid]) <= limit:
                lo = mid
            else:
                hi = mid - 1
        cut = lo
        for sep in ("\n", ". ", " "):    # режем по абзацу, предложению или слову
            pos = text.rfind(sep, 0, lo)
            if pos > lo // 2:
                cut = pos + len(sep)
                break
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return [c for c in chunks if c]


# ---------- отправка ----------

async def send(client: Client, text: str) -> None:
    sender = SENDERS.get(client.channel)
    if sender is None:
        raise ChannelError(f"канал {client.channel} выключен")
    await sender(client.id, text)


def window_open(client_key: str) -> bool:
    last = db.last_inbound_at(client_key)
    return last is not None and time.time() - last < WINDOW_HOURS * 3600


def back_to_ai_kb(client_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🤖 Вернуть клиента ИИ", callback_data=f"ai:{client_key}"),
    ]])


async def notify_admins(client_key: str, text: str, kb: InlineKeyboardMarkup | None = None,
                        forward: Message | None = None,
                        document: tuple[bytes, str] | None = None) -> int:
    """Карточка всем админам в Telegram. Каждую запоминаем, чтобы Reply ушёл клиенту.
    Админ, не нажавший «Старт» у бота, пропускается — остальные получат."""
    delivered = 0
    for admin_id in ADMIN_IDS:
        try:
            if forward is not None:
                fwd = await BOT.forward_message(admin_id, forward.chat.id, forward.message_id)
                db.save_relay(fwd.chat.id, fwd.message_id, client_key)
            if document is not None:
                doc = await BOT.send_document(admin_id, BufferedInputFile(document[0], document[1]))
                db.save_relay(doc.chat.id, doc.message_id, client_key)
            msg = await BOT.send_message(admin_id, text, reply_markup=kb, disable_web_page_preview=True)
            db.save_relay(msg.chat.id, msg.message_id, client_key)
            delivered += 1
        except Exception as e:
            logging.warning("Не доставил карточку админу %s: %s", admin_id, e)
    if not delivered:
        logging.error("Карточка по клиенту %s не доставлена НИ ОДНОМУ админу!", client_key)
    return delivered


def recent_dialog(client_key: str, n: int = 6) -> str:
    lines = []
    for m in db.get_history(client_key, n):
        icon = "🧑" if m["role"] == "user" else "🤖"
        text = MARKER.sub("", m["content"]).strip()
        if len(text) > 300:
            text = text[:300] + "…"
        lines.append(f"{icon} {esc(text)}")
    return "\n".join(lines) or "—"


async def handoff(client: Client, reason: str) -> None:
    """Передаём клиента менеджеру: ИИ замолкает, сообщения клиента идут админам."""
    db.set_mode(client.key, "human")
    window = ""
    if client.channel in WINDOW_CHANNELS:
        window = (f"\n⏳ {CHANNEL_NAMES[client.channel]}: ответить можно в течение "
                  f"{WINDOW_HOURS} ч после последнего сообщения клиента.")
    await notify_admins(
        client.key,
        f"👤 <b>Нужен человек</b>\n"
        f"Клиент: {client.card()}\n"
        f"Причина: {esc(reason)}\n\n"
        f"<b>Последние сообщения:</b>\n{recent_dialog(client.key)}\n\n"
        f"↩️ Ответь на это сообщение через Reply — ответ уйдёт клиенту.{window}",
        kb=back_to_ai_kb(client.key),
    )


async def call_human(client: Client, reason: str) -> None:
    if db.get_mode(client.key) == "human":
        await send(client, "Менеджер уже в курсе и ответит здесь 🙂 Можешь писать подробности.")
        return
    await handoff(client, reason)
    await send(client, "Позвал менеджера — он ответит прямо здесь. "
                       "Пока можешь описать вопрос подробнее, я всё передам.")


# ---------- главный сценарий ----------

async def handle_text(client: Client, text: str) -> None:
    db.upsert_user(client.key, client.channel, client.id, client.name, client.username)
    text = (text or "").strip()[:MAX_USER_TEXT]
    if not text:
        return

    # с клиентом сейчас говорит менеджер — просто пересылаем
    if db.get_mode(client.key) == "human":
        db.add_message(client.key, "user", text)
        await notify_admins(client.key, f"💬 <b>{client.card()}</b>:\n{esc(text)}\n\n↩️ Reply — ответить",
                            kb=back_to_ai_kb(client.key))
        return

    if client.channel != "tg" and HUMAN_WORDS.match(text):
        db.add_message(client.key, "user", text)
        await call_human(client, "клиент попросил менеджера")
        return

    if DAILY_LIMIT and not client.is_admin and db.user_questions_last_24h(client.key) >= DAILY_LIMIT:
        await send(client, f"Сегодня ты задал много вопросов 🙂 Чтобы продолжить, "
                           f"{client.human_hint()} — подключится менеджер.")
        return

    db.add_message(client.key, "user", text)
    asked_at = time.monotonic()
    try:
        raw = await llm.ask(KB.system_prompt, db.get_history(client.key, HISTORY_LIMIT))
    except llm.LLMError as e:
        logging.error("ИИ не ответил клиенту %s: %s", client.key, e)
        await send(client, "Секунду, у меня технический сбой 😔 Я уже передал твой вопрос "
                           "менеджеру — он ответит здесь.")
        await handoff(client, f"ИИ не смог ответить: {e}")
        return

    db.add_message(client.key, "assistant", raw)  # с метками — чтобы ИИ помнил, что заявку уже отправил
    clean, lead, operator = parse_reply(raw)
    if not clean:
        clean = ("Передал заявку, с тобой скоро свяжутся 🙂" if lead
                 else "Подключаю менеджера, он ответит здесь." if operator
                 else "Уточню и вернусь с ответом 🙂")

    # эффект «печатает, как человек»: ответ не раньше REPLY_DELAY_SECONDS после вопроса
    if REPLY_DELAY_SECONDS:
        left = REPLY_DELAY_SECONDS - (time.monotonic() - asked_at)
        if left > 0:
            await asyncio.sleep(left)
    await send(client, clean)

    if lead and db.add_lead(client.key, client.channel, client.name, client.username, lead):
        fields = parse_lead_fields(lead, client)
        extra = ""
        if not fields["lead_parsed_ok"]:
            extra = (f"\n\n⚠️ ИИ оформил заявку неполно — проверь по переписке:\n"
                     f"{recent_dialog(client.key)}")
        await notify_admins(client.key,
                            f"📝 <b>Новая заявка</b>\nКлиент: {client.card()}\n\n{esc(lead)}"
                            f"{extra}\n\n↩️ Reply — написать клиенту")
        run_in_background(export_lead(client, lead))
    if operator:
        await handoff(client, operator)

    # страховка: клиент оставил телефон, а ИИ не оформил заявку и не позвал человека
    if not lead and not operator and has_phone(text):
        logging.warning("Похоже, клиент %s оставил телефон, но заявки нет. Сообщение: %r",
                        client.key, text[:200])
        run_in_background(check_missed_lead(client, text, int(time.time())))


def has_phone(text: str) -> bool:
    return bool(find_phone(text))


async def check_missed_lead(client: Client, text: str, since: int) -> None:
    """Даём ИИ время дособрать заявку (часто он сначала уточняет имя).
    Если через MISSED_LEAD_CHECK_MINUTES заявки так и нет — говорим менеджеру."""
    await asyncio.sleep(MISSED_LEAD_CHECK_MINUTES * 60)
    if db.lead_exists_since(client.key, since) or db.get_mode(client.key) == "human":
        return
    await notify_admins(
        client.key,
        f"⚠️ <b>Возможно, пропущена заявка</b>\n"
        f"Клиент: {client.card()}\n"
        f"Он оставил телефон, но ИИ не оформил заявку:\n«{esc(text[:300])}»\n\n"
        f"<b>Последние сообщения:</b>\n{recent_dialog(client.key)}\n\n"
        f"↩️ Reply — написать клиенту. Если такое повторяется — проверь базу знаний "
        f"и прогони test_bot.py.",
        kb=back_to_ai_kb(client.key),
    )


# подписи полей в заявке → поле. ИИ просят писать по-русски, но понимаем и варианты
LEAD_LABELS = {
    "имя": "name", "клиент": "name", "аты": "name",
    "телефон": "phone", "тел": "phone", "номер": "phone", "телефоны": "phone",
    "что нужно": "request", "услуга": "request", "запрос": "request", "қызмет": "request",
    "время": "time", "когда": "time", "дата": "time", "уақыты": "time",
}
LABELED_PART = re.compile(r"^\s*([^:=]{2,20}?)\.?\s*[:=]\s*(.*)$")


# Коды стран СНГ и соседей: клиенты часто пишут их без «+» (998901234567)
CIS_CODES = ("998", "996", "992", "993", "994", "995", "374", "375", "373", "380")


def normalize_phone(raw: str) -> str:
    """Приводим номер к виду +XXXXXXXXXXX, только когда формат опознан наверняка.
    Не уверены — возвращаем ровно как написал клиент: лучше «некрасивый» номер,
    чем испорченный (+998 не должен превратиться в +7).

      +998 90 123 45 67  → +998901234567   код указан явно — верим
      8 701 123 45 67    → +77011234567    Казахстан / Россия через 8
      7 701 123 45 67    → +77011234567
      701 123 45 67      → +77011234567    казахстанский мобильный без 8
      998901234567       → +998901234567   код СНГ без «+»
      8 029 123 45 67    → как есть        местный формат Беларуси, это не +7
      0555 123 456       → как есть        местный формат Кыргызстана
      90 123 45 67       → как есть        местный формат Узбекистана
    """
    text = (raw or "").strip()
    digits = re.sub(r"\D", "", text)
    if not digits:
        return ""
    if text.startswith("+"):
        return "+" + digits
    if len(digits) == 11 and digits[0] == "8" and digits[1] != "0":
        return "+7" + digits[1:]
    if len(digits) == 11 and digits[0] == "7":
        return "+" + digits
    if len(digits) == 10 and digits[0] == "7":
        return "+7" + digits
    if len(digits) in (11, 12) and digits.startswith(CIS_CODES):
        return "+" + digits
    return text


def find_phone(text: str) -> str:
    for m in PHONE_RE.finditer(text or ""):
        if 10 <= sum(ch.isdigit() for ch in m.group()) <= 15:
            return m.group()
    return ""


def parse_lead_fields(lead_text: str, client: "Client") -> dict:
    """Заявка по полям. Основной формат — с подписями: «имя: …; телефон: …; что нужно: …».
    Тогда порядок не важен. Без подписей понимаем только «имя; телефон; …» строго
    в этом порядке. Всё остальное помечаем lead_parsed_ok=False — «проверь руками»:
    отличить имя от названия услуги по тексту нельзя, гадать хуже, чем признаться."""
    sep = ";" if ";" in lead_text else "|" if "|" in lead_text else ","
    parts = [p.strip() for p in lead_text.split(sep) if p.strip()]
    fields = {"name": "", "phone": "", "request": "", "time": ""}
    labeled, leftovers = False, []
    for part in parts:
        m = LABELED_PART.match(part)
        key = LEAD_LABELS.get(m.group(1).strip().lower()) if m else None
        if key:
            labeled = True
            fields[key] = (fields[key] + "; " if fields[key] else "") + m.group(2).strip()
        else:
            leftovers.append(part)

    if labeled:
        if leftovers:
            fields["request"] = "; ".join(filter(None, [fields["request"], *leftovers]))
        # ИИ подписал «телефон:» — берём значение, даже если это короткий местный номер
        phone_raw = (find_phone(fields["phone"])
                     or (fields["phone"] if sum(ch.isdigit() for ch in fields["phone"]) >= 7 else "")
                     or find_phone(lead_text))
        ok = bool(fields["name"] and phone_raw)
    else:
        # старый формат без подписей: верим, только если порядок ровно «имя; телефон; …»
        phone_raw = find_phone(lead_text)
        strict = (len(parts) >= 2 and phone_raw and find_phone(parts[1]) == parts[1].strip()
                  and any(ch.isalpha() for ch in parts[0]) and not find_phone(parts[0]))
        if strict:
            fields.update(name=parts[0], request=parts[2] if len(parts) > 2 else "",
                          time="; ".join(parts[3:]))
        else:
            fields["request"] = lead_text   # не угадываем — отдаём целиком
        ok = bool(strict)

    phone = normalize_phone(phone_raw)
    if not phone and client.channel == "wa":
        phone = "+" + client.id          # в WhatsApp номер клиента известен и так
    return {
        "lead_name": fields["name"] or client.name,
        "lead_phone": phone,
        "lead_request": fields["request"],
        "lead_time": fields["time"],
        # False — заявка оформлена не по формату или без имени/телефона.
        # В n8n/Make по этому полю удобно отправлять заявку на ручную проверку
        "lead_parsed_ok": ok,
        "lead_labeled": labeled,   # False — модель забыла подписи (хуже следует формату)
    }


async def export_lead(client: Client, lead_text: str) -> None:
    """Заявка во внешнюю систему (CRM, таблица) через LEAD_WEBHOOK_URL.
    Работает в фоне: медленная или упавшая CRM не задерживает и не ломает диалог."""
    if not LEAD_WEBHOOK_URL:
        return
    payload = {
        "channel": client.channel,                      # tg | wa | ig
        "channel_name": CHANNEL_NAMES.get(client.channel, client.channel),
        "id": client.id,                                # для WhatsApp — это номер
        "name": client.name,                            # имя из профиля мессенджера
        "username": client.username,
        "lead": lead_text,                              # заявка целиком, как её записал ИИ — всегда
        **parse_lead_fields(lead_text, client),         # то же по полям, с проверкой
        "created_at": int(time.time()),
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.post(LEAD_WEBHOOK_URL, json=payload) as resp:
                if resp.status >= 400:
                    logging.error("Выгрузка заявки: %s ответил кодом %s: %s",
                                  LEAD_WEBHOOK_URL, resp.status, (await resp.text())[:200])
    except Exception as e:
        logging.error("Выгрузка заявки на LEAD_WEBHOOK_URL не удалась: %s", e)


async def handle_media(client: Client, kind: str, *, forward: Message | None = None,
                       document: tuple[bytes, str] | None = None, url: str = "") -> None:
    """Фото, голосовое, файл. ИИ их не понимает: в режиме ИИ просим текст,
    в режиме менеджера — пересылаем менеджеру."""
    db.upsert_user(client.key, client.channel, client.id, client.name, client.username)
    if db.get_mode(client.key) == "human":
        db.add_message(client.key, "user", f"[{kind}]")
        link = f"\n{esc(url)}" if url else ""
        await notify_admins(client.key, f"📎 <b>{client.card()}</b> прислал: {esc(kind)}{link}\n"
                                        f"↩️ Reply — ответить",
                            kb=back_to_ai_kb(client.key), forward=forward, document=document)
        return
    await send(client, f"Пока я понимаю только текст 🙂 Напиши вопрос словами — или "
                       f"{client.human_hint()}, и менеджер посмотрит.")


async def reply_from_admin(client_key: str, text: str) -> None:
    """Менеджер ответил клиенту из Telegram (для WhatsApp и Instagram — только текст)."""
    client = client_from_key(client_key)
    if client.channel in WINDOW_CHANNELS and not window_open(client_key):
        raise ChannelError(
            f"прошло больше {WINDOW_HOURS} ч с последнего сообщения клиента — "
            f"{CHANNEL_NAMES[client.channel]} не даёт написать первым. "
            f"Свяжись с клиентом по телефону или подожди, пока он напишет сам."
        )
    await send(client, text)
    db.add_message(client_key, "assistant", f"(ответил менеджер) {text}")
    db.set_mode(client_key, "human")


# ---------- сбои ----------

async def report_error(exc: BaseException, where: str, client: Client | None = None) -> None:
    """Подробности сбоя админам в Telegram. Клиенту извиняемся отдельно, если можем."""
    logging.error("Сбой (%s)", where, exc_info=exc)
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-2500:]
    lines = [f"⚠️ <b>Сбой в боте</b> ({esc(where)})"]
    if client:
        lines.append(f"Клиент: {client.card()}")
    lines.append(f"Ошибка: <b>{esc(type(exc).__name__)}</b>")
    lines.append(f"<pre>{esc(str(exc)[:500])}</pre>")
    lines.append(f"<pre>{esc(tb)}</pre>")
    for admin_id in ADMIN_IDS:
        try:
            await BOT.send_message(admin_id, "\n".join(lines), disable_web_page_preview=True)
        except Exception:
            logging.warning("Не смог сообщить о сбое админу %s", admin_id)


async def safe_handle(client: Client, coro) -> None:
    """Обёртка для каналов WhatsApp/Instagram: любой сбой — извинение клиенту и отчёт админам."""
    try:
        await coro
    except Exception as e:
        await report_error(e, f"обработка сообщения из {CHANNEL_NAMES.get(client.channel)}", client)
        try:
            await send(client, "😔 Что-то пошло не так с моей стороны. Попробуй ещё раз через минуту."
                       + (f"\n\n{SUPPORT_LINE}" if SUPPORT_LINE else ""))
        except Exception:
            logging.warning("Не смог даже извиниться перед клиентом %s", client.key)


# ---------- защита от повторной доставки ----------

_SEEN: OrderedDict[str, None] = OrderedDict()


def seen(message_id: str) -> bool:
    """Meta иногда присылает одно и то же сообщение дважды. True — уже обрабатывали."""
    if not message_id:
        return False
    if message_id in _SEEN:
        return True
    _SEEN[message_id] = None
    if len(_SEEN) > 5000:
        _SEEN.popitem(last=False)
    return False


def reload_kb() -> knowledge.Knowledge:
    global KB
    KB = knowledge.load()
    return KB
