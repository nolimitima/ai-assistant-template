"""
Telegram: канал для клиентов (если включён в CHANNELS) и панель менеджера.

Панель менеджера работает всегда — сюда приходят заявки и вопросы
из ВСЕХ каналов, отсюда через Reply менеджер отвечает клиентам.
"""
import csv
import datetime as dt
import io
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ErrorEvent,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.chat_action import ChatActionSender

import core
import db
from config import ADMIN_IDS, CHANNELS, HUMAN_TIMEOUT_HOURS, TELEGRAM_CLIENTS, WA_GREENAPI
from core import HUMAN_BUTTON, esc

router = Router()
IS_ADMIN = F.from_user.id.in_(set(ADMIN_IDS))
MAX_TEXT = 4000


def client_of(message: Message) -> core.Client:
    u = message.from_user
    return core.Client("tg", str(message.chat.id), u.full_name or "", u.username or "")


def client_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=HUMAN_BUTTON)]],
                               resize_keyboard=True, is_persistent=True)


def serves(message: Message) -> bool:
    """С клиентами в Telegram общаемся, только если канал включён. Админам — всегда (тесты)."""
    return TELEGRAM_CLIENTS or message.from_user.id in ADMIN_IDS


async def send_text(chat_id: str, text: str) -> None:
    for chunk in core.split_text(text, MAX_TEXT):
        # ответ ИИ — без HTML, чтобы «<» в тексте не ломал отправку
        await core.BOT.send_message(int(chat_id), chunk, parse_mode=None)


core.SENDERS["tg"] = send_text


async def panel_only(message: Message) -> None:
    others = [c for c in CHANNELS if c != "telegram"]
    where = " и ".join({"whatsapp": "WhatsApp", "instagram": "Instagram"}[c] for c in others)
    await message.answer(f"Это служебный бот. Напиши нам, пожалуйста, в {where} 🙂" if where
                         else "Это служебный бот.")


# ---------- клиент: старт и сброс ----------

@router.message(CommandStart())
async def cmd_start(message: Message):
    if not serves(message):
        await panel_only(message)
        return
    c = client_of(message)
    db.upsert_user(c.key, c.channel, c.id, c.name, c.username)
    await message.answer(esc(core.KB.greeting), reply_markup=client_kb())


@router.message(Command("reset"))
async def cmd_reset(message: Message):
    if not serves(message):
        await panel_only(message)
        return
    c = client_of(message)
    db.upsert_user(c.key, c.channel, c.id, c.name, c.username)
    db.reset_history(c.key)
    db.set_mode(c.key, "ai")
    await message.answer("Начнём с чистого листа 🙂 Что тебя интересует?", reply_markup=client_kb())


# ---------- админ: команды ----------

@router.message(Command("admin", "help"), IS_ADMIN)
async def cmd_admin(message: Message):
    timeout = f" (или он вернётся сам через {HUMAN_TIMEOUT_HOURS} ч)" if HUMAN_TIMEOUT_HOURS else ""
    await message.answer(
        "<b>Команды админа</b>\n"
        "/reload — перечитать базу знаний и промпт без перезапуска\n"
        "/stats — клиенты, вопросы и заявки по каналам\n"
        "/leads — все заявки файлом (откроется в Excel)\n"
        "/reset — сбросить свой диалог с ИИ (для тестов)\n"
        + ("/whatsapp — статус WhatsApp и ссылка для привязки номера\n" if WA_GREENAPI else "")
        + "\n"
        "<b>Как отвечать клиенту:</b> на карточку клиента нажми Reply и напиши ответ — "
        "он уйдёт клиенту туда, откуда он пришёл (Telegram, WhatsApp или Instagram). "
        "Пока ты на связи, ИИ молчит. Кнопка «Вернуть клиента ИИ» включает его обратно"
        f"{timeout}.\n\n"
        "⏳ В WhatsApp и Instagram ответить можно только в течение 24 ч после "
        "последнего сообщения клиента — таковы правила Meta.\n\n"
        "Пиши боту сам как обычный клиент — так удобно проверять ответы."
    )


def kb_report(title: str) -> str:
    kb = core.KB
    lines = [f"<b>{title}</b>",
             f"Файлов в базе: {len(kb.files)} ({', '.join(esc(f) for f in kb.files) or '—'})",
             f"Объём: {kb.chars:,} символов".replace(",", " ")]
    lines += [f"❌ {esc(p)}" for p in kb.problems]
    lines += [f"⚠️ {esc(w)}" for w in kb.warnings]
    if not kb.problems:
        lines.append("✅ База знаний на месте")
    return "\n".join(lines)


@router.message(Command("whatsapp"), IS_ADMIN)
async def cmd_whatsapp(message: Message):
    if not WA_GREENAPI:
        await message.answer("WhatsApp через QR не включён (WHATSAPP_VIA=greenapi в .env).")
        return
    from channels import greenapi
    ok, info = await greenapi.describe()
    await message.answer(
        f"{'✅' if ok else '❌'} WhatsApp: {esc(info)}\n\n"
        f"🔗 Ссылка для привязки номера:\n{esc(greenapi.connect_link())}\n\n"
        "Отправь её владельцу номера — он откроет и сам привяжет WhatsApp: с компьютера "
        "по QR-коду, с телефона по коду. Доступ к боту и кабинетам ему не нужен. "
        "Ссылка с localhost открывается только на компьютере, где запущен бот — "
        "чтобы отправить её клиенту, бот должен быть на сервере (PUBLIC_URL).\n"
        "⚠️ Ссылка даёт привязать номер к боту — не публикуй её.",
        disable_web_page_preview=True,
    )


@router.message(Command("reload"), IS_ADMIN)
async def cmd_reload(message: Message):
    core.reload_kb()
    await message.answer(kb_report("🔄 База знаний перечитана"))


@router.message(Command("stats"), IS_ADMIN)
async def cmd_stats(message: Message):
    s = db.stats()
    per = ", ".join(f"{core.CHANNEL_NAMES.get(ch, ch)}: {n}" for ch, n in s["by_channel"].items()) or "—"
    await message.answer(
        "<b>📊 Статистика</b>\n"
        f"Клиентов всего: {s['users']} ({per})\n"
        f"Активных за сутки: {s['users_day']}\n"
        f"Вопросов за сутки: {s['questions_day']}\n"
        f"Заявок всего: {s['leads']}, за неделю: {s['leads_week']}\n"
        f"Сейчас на менеджере: {s['with_manager']}"
    )


@router.message(Command("leads"), IS_ADMIN)
async def cmd_leads(message: Message):
    rows = db.all_leads()
    if not rows:
        await message.answer("Заявок пока нет.")
        return
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["№", "Дата", "Канал", "Имя", "Username / номер", "Заявка"])
    for r in rows:
        when = dt.datetime.fromtimestamp(r["created_at"]).strftime("%d.%m.%Y %H:%M")
        contact = r["username"] or r["client_key"].split(":", 1)[1]
        w.writerow([r["id"], when, core.CHANNEL_NAMES.get(r["channel"], r["channel"]),
                    r["name"], contact, r["details"]])
    data = buf.getvalue().encode("utf-8-sig")  # utf-8-sig — чтобы Excel не ломал кириллицу
    await message.answer_document(BufferedInputFile(data, filename="leads.csv"),
                                  caption=f"Заявок: {len(rows)}")


# ---------- админ: ответ клиенту через Reply ----------

async def reply_to_client(message: Message) -> bool | dict:
    """Фильтр: админ ответил (Reply) на карточку клиента. Отдаёт ключ клиента в хэндлер."""
    if message.from_user.id not in ADMIN_IDS or not message.reply_to_message:
        return False
    key = db.find_relay(message.chat.id, message.reply_to_message.message_id)
    return {"client_key": key} if key and not key.startswith("system:") else False


@router.message(reply_to_client)
async def admin_reply(message: Message, bot: Bot, client_key: str):
    channel = client_key.split(":", 1)[0]
    try:
        if channel == "tg":
            await bot.copy_message(int(client_key.split(":", 1)[1]), message.chat.id, message.message_id)
            if message.text:
                db.add_message(client_key, "assistant", f"(ответил менеджер) {message.text}")
            db.set_mode(client_key, "human")
        else:
            text = message.text or message.caption
            if not text:
                await message.answer(f"В {core.CHANNEL_NAMES[channel]} я пока отправляю только текст.")
                return
            await core.reply_from_admin(client_key, text)
    except core.ChannelError as e:
        await message.answer(f"❌ Не отправлено: {esc(str(e))}")
        return
    except Exception as e:
        await message.answer(f"❌ Не доставлено клиенту: {esc(str(e))}")
        return
    timeout = f" Или сам вернётся к ИИ через {HUMAN_TIMEOUT_HOURS} ч." if HUMAN_TIMEOUT_HOURS else ""
    await message.answer(f"✅ Отправлено. Клиент сейчас на тебе, ИИ молчит.{timeout}",
                         reply_markup=core.back_to_ai_kb(client_key))


@router.callback_query(F.data.startswith("ai:"))
async def back_to_ai(cb: CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Недостаточно прав", show_alert=True)
        return
    client_key = cb.data.split(":", 1)[1]
    db.set_mode(client_key, "ai")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    client = core.client_from_key(client_key)
    await cb.message.answer(f"🤖 Клиент {client.card()} снова общается с ИИ — {esc(cb.from_user.full_name)}")
    await cb.answer("Готово")


# ---------- клиент: позвать человека ----------

@router.message(F.text == HUMAN_BUTTON)
@router.message(Command("operator"))
async def call_human(message: Message):
    if not serves(message):
        await panel_only(message)
        return
    c = client_of(message)
    db.upsert_user(c.key, c.channel, c.id, c.name, c.username)
    await core.call_human(c, "клиент нажал «Позвать человека»")


# ---------- клиент: основной диалог ----------

@router.message(F.text)
async def on_text(message: Message, bot: Bot):
    if not serves(message):
        await panel_only(message)
        return
    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        await core.handle_text(client_of(message), message.text)


@router.message()
async def on_other(message: Message):
    if message.from_user is None:
        return
    if not serves(message):
        await panel_only(message)
        return
    kind = message.content_type.replace("_", " ") if message.content_type else "файл"
    await core.handle_media(client_of(message), kind, forward=message)


# ---------- перехват сбоев ----------

@router.errors()
async def on_unexpected_error(event: ErrorEvent, bot: Bot) -> bool:
    """Любая непойманная ошибка: клиенту — понятное сообщение, админам — подробности."""
    upd = event.update
    src = upd.message or (upd.callback_query.message if upd.callback_query else None)
    user = upd.message.from_user if upd.message else (
        upd.callback_query.from_user if upd.callback_query else None)
    client = client_of(upd.message) if upd.message and user else None

    if src is not None and (user is None or user.id not in ADMIN_IDS):
        try:
            await bot.send_message(src.chat.id, "😔 Что-то пошло не так с моей стороны. "
                                                "Попробуй ещё раз через минуту."
                                   + (f"\n\n{core.SUPPORT_LINE}" if core.SUPPORT_LINE else ""))
        except Exception:
            logging.warning("Не смог даже извиниться перед пользователем")

    await core.report_error(event.exception, "Telegram", client)
    return True
