"""
Проверка настроек перед запуском.

    python check_setup.py

Ничего не меняет и клиентам не пишет. Проверяет: токен бота, админов,
ключ нейросети, базу знаний и подключённые каналы (WhatsApp, Instagram).
Тратит один крошечный запрос к нейросети (доли цента).
"""
import asyncio
import json
import sys

OK, WARN, BAD = "✅", "⚠️ ", "❌"
problems: list[str] = []


def say(mark: str, text: str) -> None:
    print(f"{mark} {text}")


def fail(text: str) -> None:
    say(BAD, text)
    problems.append(text)


async def main() -> None:
    print("Проверяю настройки...\n")

    # --- 1. .env ---
    try:
        import config
    except Exception as e:
        print(f"{BAD} {e}\n")
        print("Открой файл .env и исправь (образец — .env.example).")
        sys.exit(1)
    say(OK, "Файл .env прочитан, обязательные поля на месте")
    names = {"telegram": "Telegram", "whatsapp": "WhatsApp", "instagram": "Instagram"}
    say(OK, "Каналы для клиентов: " + ", ".join(names[c] for c in config.CHANNELS))

    import aiohttp

    # --- 2. Telegram: токен и админы ---
    print("\n— Telegram (канал и панель менеджера)")
    api = f"https://api.telegram.org/bot{config.BOT_TOKEN}"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:

        async def tg(method: str, **params):
            async with session.get(f"{api}/{method}", params=params) as resp:
                body = await resp.text()
            try:
                return json.loads(body)
            except Exception:
                return {"ok": False, "description": f"Telegram ответил непонятно (код {resp.status})"}

        try:
            me = await tg("getMe")
        except Exception as e:
            fail(f"Нет связи с Telegram: {e}")
            return summary()
        if not me.get("ok"):
            fail(f"BOT_TOKEN не работает: {me.get('description')}. Возьми токен заново у @BotFather.")
            return summary()
        bot_name = me["result"]["username"]
        say(OK, f"Бот на связи: @{bot_name}")
        for admin_id in config.ADMIN_IDS:
            r = await tg("getChat", chat_id=admin_id)
            if r.get("ok"):
                name = r["result"].get("username") or r["result"].get("first_name", "")
                say(OK, f"Админ {admin_id} ({name}) будет получать заявки")
            else:
                say(WARN, f"Админ {admin_id}: бот пока не может ему написать. "
                          f"Пусть откроет @{bot_name} и нажмёт «Старт».")

    # --- 3. Нейросеть ---
    print("\n— Нейросеть")
    import llm
    try:
        answer = await llm.ask("Ты проверка связи. Ответь одним словом: ок.",
                               [{"role": "user", "content": "Проверка"}])
        say(OK, f"Отвечает ({config.LLM_PROVIDER}, {config.LLM_MODEL}): «{answer.strip()[:40]}»")
    except llm.LLMError as e:
        fail(f"Нейросеть не отвечает: {e}")

    # --- 4. База знаний ---
    print("\n— База знаний")
    import knowledge
    kb = knowledge.load()
    if kb.files:
        say(OK, f"{len(kb.files)} файл(ов), {kb.chars:,} символов".replace(",", " "))
        for f in kb.files:
            print(f"     • knowledge/{f}")
    for p in kb.problems:
        fail(p)
    for w in kb.warnings:
        say(WARN, w)
    if not kb.problems:
        say(OK, "Промпт и приветствие заполнены")

    # --- 5. WhatsApp и Instagram ---
    if config.WA_GREENAPI:
        print("\n— WhatsApp (по QR-коду, Green API)")
        from channels import greenapi
        ok, info = await greenapi.describe()
        if ok:
            say(OK, info)
        elif "неверный" in info or "нет связи" in info.lower():
            fail(f"Green API: {info}")
        else:
            say(WARN, f"{info}. Запусти бота и открой ссылку из команды /whatsapp.")
        say(WARN, "Неофициальный способ: WhatsApp может заблокировать номер. "
                  "Как снизить риск — CHANNELS.md → «WhatsApp через QR-код».")
    if config.WA_META:
        print("\n— WhatsApp")
        from channels import whatsapp
        ok, info = await whatsapp.describe()
        (say(OK, f"Номер: {info}") if ok else fail(f"WhatsApp: {info}"))
        if not config.WHATSAPP_BUSINESS_ACCOUNT_ID:
            say(WARN, "WHATSAPP_BUSINESS_ACCOUNT_ID пуст — бот не сможет сам подписаться на сообщения. "
                      "Возьми ID в WhatsApp → API Setup.")
    if config.INSTAGRAM_ENABLED:
        print("\n— Instagram")
        from channels import instagram
        ok, info = await instagram.describe(config.INSTAGRAM_TOKEN)
        (say(OK, f"Аккаунт: {info}") if ok else fail(f"Instagram: {info}"))

    # --- 6. Дополнительно ---
    print("\n— Дополнительно")
    say(OK if config.REPLY_DELAY_SECONDS else "➖", f"Пауза перед ответом: "
        f"{str(config.REPLY_DELAY_SECONDS) + ' с' if config.REPLY_DELAY_SECONDS else 'выключена'}")
    if config.LEAD_WEBHOOK_URL:
        say(OK, "Выгрузка заявок включена (LEAD_WEBHOOK_URL). Проверь её тестовой заявкой — "
                "check_setup ничего туда не отправляет, чтобы не мусорить в CRM.")
    else:
        say("➖", "Выгрузка заявок в CRM выключена (LEAD_WEBHOOK_URL пуст)")

    if config.WEBHOOKS_NEEDED:
        print("\n— Вебхуки (вставить в настройках Meta)")
        if not config.PUBLIC_URL:
            say(WARN, "PUBLIC_URL пуст. WhatsApp и Instagram заработают, когда бот будет на сервере "
                      "с https-адресом — впиши его в PUBLIC_URL (CHANNELS.md → «Сервер»).")
            base = "https://АДРЕС-СЕРВЕРА"
        else:
            base = config.PUBLIC_URL
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                    async with s.get(f"{base}/") as r:
                        alive = r.status == 200 and (await r.text()).strip() == "ok"
            except Exception:
                alive = False
            (say(OK, f"Сервер отвечает: {base}") if alive else
             say(WARN, f"{base} не отвечает. Нормально, если бот ещё не запущен на сервере."))
        if config.WA_META:
            print(f"     WhatsApp  Callback URL:  {base}/webhook/whatsapp")
        if config.INSTAGRAM_ENABLED:
            print(f"     Instagram Callback URL:  {base}/webhook/instagram")
        print(f"     Verify token:            {config.META_VERIFY_TOKEN}")

    summary()


def summary() -> None:
    print()
    if problems:
        print(f"Нашёл проблем: {len(problems)}. Исправь их и запусти проверку ещё раз.")
        sys.exit(1)
    print("Всё готово. Запускай бота:  python bot.py")


if __name__ == "__main__":
    asyncio.run(main())
