"""
ИИ-ассистент: консультант, куратор, поддержка — кем сделаешь.
Каналы для клиентов: Telegram, WhatsApp, Instagram — какие включишь в CHANNELS.

Запуск:  python bot.py

Что запускается:
  * Telegram-бот (всегда) — канал для клиентов и панель менеджера
  * веб-сервер (только если включены WhatsApp или Instagram) — на него Meta
    присылает сообщения клиентов
"""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiohttp import web

import core
import db
import tg
from config import (
    ADMIN_IDS,
    BOT_TOKEN,
    INSTAGRAM_ENABLED,
    PORT,
    PUBLIC_URL,
    TELEGRAM_CLIENTS,
    WA_GREENAPI,
    WA_META,
    WEBHOOKS_NEEDED,
    WEBSERVER_NEEDED,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


async def health(_: web.Request) -> web.Response:
    return web.Response(text="ok")


async def start_webserver() -> list[str]:
    """Веб-сервер для вебхуков Meta. Возвращает строки для отчёта админу."""
    app = web.Application()
    app.router.add_get("/", health)
    report: list[str] = []

    if WA_GREENAPI:
        from channels import greenapi
        greenapi.add_routes(app)
        ok, info = await greenapi.describe()
        report.append(f"{'✅' if ok else '❌'} WhatsApp: {core.esc(info)}")
        report.append(f"🔗 Привязать номер: {core.esc(greenapi.connect_link())}")
        asyncio.create_task(greenapi.poll_loop())

    if WA_META:
        from channels import whatsapp
        whatsapp.add_routes(app)
        ok, info = await whatsapp.describe()
        sub = await whatsapp.subscribe_app() if ok else ""
        report.append(f"{'✅' if ok else '❌'} WhatsApp: {core.esc(info)}" + (f" · {core.esc(sub)}" if sub else ""))

    if INSTAGRAM_ENABLED:
        from channels import instagram
        instagram.add_routes(app)
        ok, info = await instagram.describe()
        sub = await instagram.subscribe_app() if ok else ""
        report.append(f"{'✅' if ok else '❌'} Instagram: {core.esc(info)}" + (f" · {core.esc(sub)}" if sub else ""))
        asyncio.create_task(instagram.refresh_loop())

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logging.info("Веб-сервер для WhatsApp/Instagram слушает порт %s", PORT)
    if PUBLIC_URL:
        report.append(f"🌐 Адрес сервера: {core.esc(PUBLIC_URL)}")
    elif WEBHOOKS_NEEDED:
        report.append("⚠️ PUBLIC_URL не задан — Meta не сможет присылать сообщения, "
                      "пока бот не на сервере с https-адресом (CHANNELS.md)")
    return report



async def main():
    db.init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    core.BOT = bot
    dp = Dispatcher()
    dp.include_router(tg.router)

    lines = [tg.kb_report("✅ Бот запущен"), "",
             f"{'✅' if TELEGRAM_CLIENTS else '➖'} Telegram для клиентов: "
             f"{'включён' if TELEGRAM_CLIENTS else 'выключен (бот — только панель менеджера)'}"]
    if WEBSERVER_NEEDED:
        lines += await start_webserver()
    lines += ["", "/admin — команды"]

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, "\n".join(lines), disable_web_page_preview=True)
        except Exception:
            logging.warning("Админ %s не нажал «Старт» у бота — не смог сообщить о запуске", admin_id)
    for p in core.KB.problems:
        logging.warning("База знаний: %s", p)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
