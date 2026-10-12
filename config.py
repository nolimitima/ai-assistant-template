"""
Конфигурация бота.

Все секреты и настройки читаются из файла .env рядом с этим файлом.
Сам .env в git не попадает (см. .gitignore), шаблон — .env.example.

Содержимое ассистента (кто он, что знает) лежит НЕ здесь, а в папках
prompts/ и knowledge/ — их заполняет Claude Code по итогам интервью.
"""
import hashlib
import os
import re
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# override=True: значения из .env важнее случайных переменных окружения в системе
load_dotenv(BASE_DIR / ".env", override=True)


class ConfigError(RuntimeError):
    """Понятная ошибка конфигурации вместо непонятного трейсбека."""


def _str_env(name: str, default: str | None = None, required: bool = False) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        if required:
            raise ConfigError(
                f"В файле .env не заполнено значение {name}. "
                f"Открой .env и впиши его (образец — в .env.example)."
            )
        return default or ""
    return value


def _int_env(name: str, default: int = 0) -> int:
    raw = _str_env(name)
    if not raw:
        return default
    cleaned = raw.replace(" ", "").replace("\u00a0", "").replace("_", "")
    try:
        return int(cleaned)
    except ValueError:
        raise ConfigError(
            f"Значение {name} в .env должно быть целым числом, а сейчас там: {raw!r}"
        ) from None


def _float_env(name: str, default: float = 0.0) -> float:
    raw = _str_env(name)
    if not raw:
        return default
    try:
        return float(raw.replace(" ", "").replace(",", "."))
    except ValueError:
        raise ConfigError(
            f"Значение {name} в .env должно быть числом, а сейчас там: {raw!r}"
        ) from None


def _int_list_env(name: str, required: bool = False) -> list[int]:
    """Список telegram id через запятую: «111, 222». Порядок сохраняем."""
    raw = _str_env(name, required=required)
    out: list[int] = []
    for chunk in re.split(r"[,;\s]+", raw):
        if not chunk:
            continue
        try:
            out.append(int(chunk))
        except ValueError:
            raise ConfigError(
                f"В {name} должны быть только telegram id (числа) через запятую, "
                f"а сейчас там: {raw!r}"
            ) from None
    if required and not out:
        raise ConfigError(f"В файле .env не заполнено значение {name}.")
    return out


# --- Telegram ---
BOT_TOKEN = _str_env("BOT_TOKEN", required=True)
# Админы = менеджеры: получают заявки и вопросы из ВСЕХ каналов в Telegram
# и отвечают клиентам через бота (в тот же канал, откуда клиент пришёл).
ADMIN_IDS = _int_list_env("ADMIN_ID", required=True)
# Контакт, который бот показывает при сбоях. Можно оставить пустым.
SUPPORT_CONTACT = _str_env("SUPPORT_CONTACT", default="")

# --- Нейросеть ---
# Готовые варианты: достаточно названия, адрес и модель подставятся сами.
#   провайдер:  (формат API, адрес, модель по умолчанию)
PROVIDERS = {
    "deepseek":   ("openai", "https://api.deepseek.com", "deepseek-v4-flash"),
    "anthropic":  ("anthropic", "", "claude-haiku-4-5-20251001"),
    "gemini":     ("openai", "https://generativelanguage.googleapis.com/v1beta/openai",
                   "gemini-2.5-flash-lite"),
    "openai":     ("openai", "https://api.openai.com/v1", ""),
    "openrouter": ("openai", "https://openrouter.ai/api/v1", ""),
}
LLM_PROVIDER = _str_env("LLM_PROVIDER", default="deepseek").lower()
if LLM_PROVIDER not in PROVIDERS:
    raise ConfigError(
        f"LLM_PROVIDER может быть: {', '.join(PROVIDERS)}. А сейчас там: {LLM_PROVIDER!r}"
    )
LLM_API_FORMAT, _default_url, _default_model = PROVIDERS[LLM_PROVIDER]
LLM_API_KEY = _str_env("LLM_API_KEY", required=True)
LLM_MODEL = _str_env("LLM_MODEL", default=_default_model)
LLM_BASE_URL = _str_env("LLM_BASE_URL", default=_default_url).rstrip("/")
LLM_MAX_TOKENS = _int_env("LLM_MAX_TOKENS", default=700)
LLM_TEMPERATURE = _float_env("LLM_TEMPERATURE", default=0.3)

# --- Поведение ---
HISTORY_LIMIT = _int_env("HISTORY_LIMIT", default=12)       # сколько сообщений диалога помнить
DAILY_LIMIT = _int_env("DAILY_LIMIT", default=40)           # вопросов к ИИ на клиента за сутки, 0 = без лимита
HUMAN_TIMEOUT_HOURS = _int_env("HUMAN_TIMEOUT_HOURS", default=12)  # авто-возврат к ИИ, 0 = никогда
REPLY_DELAY_SECONDS = _int_env("REPLY_DELAY_SECONDS", default=0)    # эффект «печатает», 0 = сразу

# --- Выгрузка заявок наружу (CRM, таблица) ---
# Каждая новая заявка уходит POST-запросом с JSON на этот адрес (n8n, Make, Zapier).
LEAD_WEBHOOK_URL = _str_env("LEAD_WEBHOOK_URL", default="")

# --- Каналы, где ассистент общается с клиентами ---
# Telegram-бот нужен всегда: это ещё и панель менеджера (заявки, ответы клиентам).
_CHANNEL_ALIASES = {
    "telegram": "telegram", "tg": "telegram", "телеграм": "telegram",
    "whatsapp": "whatsapp", "wa": "whatsapp", "ватсап": "whatsapp",
    "instagram": "instagram", "ig": "instagram", "insta": "instagram", "инстаграм": "instagram",
}
CHANNELS: list[str] = []
for _part in re.split(r"[,;\s]+", _str_env("CHANNELS", default="telegram").lower()):
    if not _part:
        continue
    if _part not in _CHANNEL_ALIASES:
        raise ConfigError(
            f"В CHANNELS непонятный канал {_part!r}. Можно: telegram, whatsapp, instagram "
            "через запятую."
        )
    if _CHANNEL_ALIASES[_part] not in CHANNELS:
        CHANNELS.append(_CHANNEL_ALIASES[_part])
if not CHANNELS:
    CHANNELS = ["telegram"]

TELEGRAM_CLIENTS = "telegram" in CHANNELS
WHATSAPP_ENABLED = "whatsapp" in CHANNELS
INSTAGRAM_ENABLED = "instagram" in CHANNELS

# Как подключён WhatsApp:
#   greenapi — по QR-коду через Green API: за 5 минут, без Meta и без сервера (неофициально)
#   meta     — официальный WhatsApp Cloud API: надёжнее, но нужен сервер и аккаунт Meta
WHATSAPP_VIA = _str_env("WHATSAPP_VIA", default="meta").lower()
if WHATSAPP_VIA not in ("meta", "greenapi"):
    raise ConfigError(f"WHATSAPP_VIA может быть только greenapi или meta, а сейчас там: {WHATSAPP_VIA!r}")
WA_META = WHATSAPP_ENABLED and WHATSAPP_VIA == "meta"
WA_GREENAPI = WHATSAPP_ENABLED and WHATSAPP_VIA == "greenapi"
# Meta присылает сообщения на адрес сервера — нужен https. Green API забираем сами — не нужен.
WEBHOOKS_NEEDED = WA_META or INSTAGRAM_ENABLED
WEBSERVER_NEEDED = WEBHOOKS_NEEDED or WA_GREENAPI   # для Green API — страница привязки номера


def _need(name: str, channel: str, enabled: bool) -> str:
    value = _str_env(name)
    if enabled and not value:
        raise ConfigError(
            f"Включён канал {channel}, но в .env не заполнено {name}. "
            f"Где его взять — в CHANNELS.md, раздел «{channel}»."
        )
    return value


# --- Веб-сервер для WhatsApp и Instagram (Meta присылает сообщения на него) ---
PORT = _int_env("PORT", default=8080)
PUBLIC_URL = _str_env("PUBLIC_URL").rstrip("/")   # https://адрес-сервера, без / в конце
META_GRAPH_VERSION = _str_env("META_GRAPH_VERSION", default="v25.0")
# Пароль, который вставляется в Meta при настройке вебхука. Не задан — придумываем сами.
META_VERIFY_TOKEN = _str_env("META_VERIFY_TOKEN") or hashlib.sha256(
    ("verify:" + BOT_TOKEN).encode()).hexdigest()[:24]

# --- WhatsApp Cloud API ---
WHATSAPP_TOKEN = _need("WHATSAPP_TOKEN", "WhatsApp", WA_META)
WHATSAPP_PHONE_NUMBER_ID = _need("WHATSAPP_PHONE_NUMBER_ID", "WhatsApp", WA_META)
WHATSAPP_APP_SECRET = _need("WHATSAPP_APP_SECRET", "WhatsApp", WA_META)
WHATSAPP_BUSINESS_ACCOUNT_ID = _str_env("WHATSAPP_BUSINESS_ACCOUNT_ID")  # необязательно, но лучше указать

# --- WhatsApp через Green API (QR-код) ---
GREENAPI_ID_INSTANCE = _need("GREENAPI_ID_INSTANCE", "WhatsApp через QR", WA_GREENAPI)
GREENAPI_API_TOKEN = _need("GREENAPI_API_TOKEN", "WhatsApp через QR", WA_GREENAPI)
GREENAPI_API_URL = _str_env("GREENAPI_API_URL", default="https://api.green-api.com").rstrip("/")
# Ответил клиенту сам с телефона — бот замолкает в этом чате (как при «Позвать человека»)
WHATSAPP_PAUSE_ON_PHONE_REPLY = _str_env("WHATSAPP_PAUSE_ON_PHONE_REPLY", default="1") not in ("0", "no", "false", "нет")
# Секрет в ссылке на страницу привязки номера. Пусто — бот придумает сам
CONNECT_SECRET = _str_env("CONNECT_SECRET") or hashlib.sha256(
    ("connect:" + BOT_TOKEN).encode()).hexdigest()[:20]

# --- Instagram (API with Instagram Login) ---
INSTAGRAM_TOKEN = _need("INSTAGRAM_TOKEN", "Instagram", INSTAGRAM_ENABLED)
INSTAGRAM_APP_SECRET = _need("INSTAGRAM_APP_SECRET", "Instagram", INSTAGRAM_ENABLED)

# --- Пути ---
KNOWLEDGE_DIR = BASE_DIR / "knowledge"
PROMPTS_DIR = BASE_DIR / "prompts"
DB_PATH = _str_env("DB_PATH", default=str(BASE_DIR / "assistant.db"))

# --- Проверки на старте ---
# ключ от одного сервиса вставили в другой — частая ошибка, говорим сразу
_KEY_HINTS = [("sk-ant-", "anthropic", "Anthropic"), ("sk-or-", "openrouter", "OpenRouter"),
              ("AIza", "gemini", "Google Gemini")]
for _prefix, _provider, _title in _KEY_HINTS:
    if LLM_API_KEY.startswith(_prefix) and LLM_PROVIDER != _provider:
        raise ConfigError(
            f"LLM_API_KEY похож на ключ {_title}, а LLM_PROVIDER={LLM_PROVIDER}. "
            f"Поставь в .env LLM_PROVIDER={_provider}."
        )
if not LLM_MODEL:
    raise ConfigError(
        f"Для LLM_PROVIDER={LLM_PROVIDER} нужно указать LLM_MODEL — точное имя модели "
        "со страницы провайдера (названия моделей там часто меняются)."
    )
if PUBLIC_URL and not PUBLIC_URL.startswith("https://"):
    raise ConfigError("PUBLIC_URL должен начинаться с https:// — Meta не работает с обычным http.")
if WA_GREENAPI and not GREENAPI_ID_INSTANCE.isdigit():
    raise ConfigError("GREENAPI_ID_INSTANCE — это число (idInstance из кабинета Green API), только цифры.")
if WA_META and not WHATSAPP_PHONE_NUMBER_ID.isdigit():
    raise ConfigError(
        "WHATSAPP_PHONE_NUMBER_ID — это не номер телефона, а числовой ID номера "
        "из раздела WhatsApp → API Setup. Только цифры."
    )
if LEAD_WEBHOOK_URL and not LEAD_WEBHOOK_URL.startswith(("https://", "http://")):
    raise ConfigError(
        "LEAD_WEBHOOK_URL должен быть полным адресом, начинаться с https:// "
        f"(скопируй его из n8n / Make / Zapier целиком). Сейчас там: {LEAD_WEBHOOK_URL!r}"
    )
if REPLY_DELAY_SECONDS < 0 or REPLY_DELAY_SECONDS > 60:
    raise ConfigError("REPLY_DELAY_SECONDS — число секунд от 0 до 60.")
if HISTORY_LIMIT < 2:
    raise ConfigError("HISTORY_LIMIT должен быть не меньше 2, иначе бот забудет вопрос клиента.")
