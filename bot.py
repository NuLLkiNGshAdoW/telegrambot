import asyncio
import html
import hashlib
import json
import logging
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web
from aiogram.types import (
    CallbackQuery,
    BotCommand,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)
from dotenv import load_dotenv
from google import genai
from google.genai import types
from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession


load_dotenv()


def required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Не задана переменная окружения {name}")
    return value


BOT_TOKEN = required("BOT_TOKEN")
GEMINI_API_KEY = required("GEMINI_API_KEY")
ADMIN_USER_ID = int(required("ADMIN_USER_ID"))
ADMIN_USER_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_USER_IDS", str(ADMIN_USER_ID)).split(",")
    if value.strip()
}
TARGET_CHANNEL = required("TARGET_CHANNEL")
SOURCE_CHANNEL = required("SOURCE_CHANNEL")
TELEGRAM_API_ID = int(required("TELEGRAM_API_ID"))
TELEGRAM_API_HASH = required("TELEGRAM_API_HASH")
TELETHON_SESSION = os.getenv("TELETHON_SESSION", "data/userbot")
TELETHON_SESSION_STRING = os.getenv("TELETHON_SESSION_STRING", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
DB_PATH = Path(os.getenv("DB_PATH", "data/bot.sqlite3"))
LOG_PATH = Path(os.getenv("LOG_PATH", "data/bot.log"))
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("PORT", os.getenv("WEB_PORT", "8080")))
TIMEZONE = os.getenv("TIMEZONE", "UTC")
BLOCKED_KEYWORDS = {
    value.strip().lower()
    for value in os.getenv("BLOCKED_KEYWORDS", "").split(",")
    if value.strip()
}
TEST_MODE = os.getenv("TEST_MODE", "0").lower() in {"1", "true", "yes"}
MAX_MEDIA_MB = float(os.getenv("MAX_MEDIA_MB", "50"))
BLOCKED_LINK_DOMAINS = {
    value.strip().lower()
    for value in os.getenv("BLOCKED_LINK_DOMAINS", "").split(",")
    if value.strip()
}
REQUIRED_HASHTAGS = {
    value.strip().lower().lstrip("#")
    for value in os.getenv("REQUIRED_HASHTAGS", "").split(",")
    if value.strip()
}
BLOCKED_AUTHOR_IDS = {
    value.strip()
    for value in os.getenv("BLOCKED_AUTHOR_IDS", "").split(",")
    if value.strip()
}
BLOCKED_EXTENSIONS = {
    value.strip().lower().lstrip(".")
    for value in os.getenv("BLOCKED_EXTENSIONS", "exe,bat,cmd,com,scr").split(",")
    if value.strip()
}

SYSTEM_PROMPT = """
Ты — редактор игрового Telegram-медиа «0x00 SPACE», в первую очередь раздела
Minecraft-модов, аддонов, карт и скин-паков. Преврати исходное сообщение в
готовый красиво оформленный пост для Telegram.

Правила:
- начинай с заголовка в стиле <b>🌀🌀🌀 — Название</b>;
- если в исходнике есть название дополнения, оставь его в заголовке, без лишней строки «Мод»;
- используй небольшие абзацы и эмодзи-маркеры, чтобы текст легко читался с телефона;
- начинай основные абзацы с уместного маркера 🌀;
- добавляй отдельный блок <b>🌀 ПОДРОБНЕЕ О ДОПОЛНЕНИИ</b>;
- если в исходнике есть ссылка, обязательно сохрани её как:
  <a href="URL">нажми, чтобы посмотреть</a>;
- если в исходнике указаны версии, сохрани их отдельной строкой:
  <b>Версии:</b> ...;
- сохрани существующие хэштеги и приведи их к строке:
  <b>Теги:</b> #mod #Minecraft;
- не придумывай версии, ссылки, теги, функции или характеристики;
- убирай рекламу, призывы подписаться и упоминания сторонних каналов;
- верни только готовый текст без пояснений;
- используй только Telegram HTML: <b>жирный</b>, <i>курсив</i>, <code>код</code>, <a href="...">ссылка</a>;
- не используй Markdown, таблицы и другие HTML-теги.

Пример структуры (не копируй содержание):
<b>🧩 Скин-пак</b>
<i>Cyber Samurais</i>

💜 8 неоновых скинов в футуристическом стиле. Яркие цвета и уникальный дизайн.

<b>🔗 ПОДРОБНЕЕ О ДОПОЛНЕНИИ</b>
<a href="https://example.com">нажми, чтобы посмотреть</a>

<b>Версии:</b> 1.21.20 и выше
<b>Теги:</b> #Skinpack #Minecraft
""".strip()

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
gemini = genai.Client(api_key=GEMINI_API_KEY)
user_client = TelegramClient(
    StringSession(TELETHON_SESSION_STRING) if TELETHON_SESSION_STRING else TELETHON_SESSION,
    TELEGRAM_API_ID,
    TELEGRAM_API_HASH,
)
processing_lock = asyncio.Lock()
gemini_semaphore = asyncio.Semaphore(1)


class SourceSettingsState(StatesGroup):
    waiting_addsource = State()
    waiting_keywords = State()
    waiting_prompt = State()
    waiting_edit = State()


def db_connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with closing(db_connect()) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                media_type TEXT,
                media_id TEXT,
                original_text TEXT,
                source_id INTEGER,
                source_message_id INTEGER,
                scheduled_at TIMESTAMP,
                target_channel TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                keywords TEXT NOT NULL DEFAULT '',
                prompt TEXT NOT NULL DEFAULT '',
                auto_publish INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_messages (
                source_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (source_id, message_id)
            )
            """
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS content_hashes (
                content_hash TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        existing_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(drafts)").fetchall()
        }
        for name, definition in {
            "original_text": "TEXT",
            "source_id": "INTEGER",
            "source_message_id": "INTEGER",
            "scheduled_at": "TIMESTAMP",
            "target_channel": "TEXT",
        }.items():
            if name not in existing_columns:
                db.execute(f"ALTER TABLE drafts ADD COLUMN {name} {definition}")
        source_columns = {row["name"] for row in db.execute("PRAGMA table_info(sources)").fetchall()}
        for name, definition in {
            "keywords": "TEXT NOT NULL DEFAULT ''",
            "prompt": "TEXT NOT NULL DEFAULT ''",
            "auto_publish": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in source_columns:
                db.execute(f"ALTER TABLE sources ADD COLUMN {name} {definition}")
        db.execute(
            "INSERT OR IGNORE INTO sources(channel, title) VALUES (?, ?)",
            (SOURCE_CHANNEL.strip(), SOURCE_CHANNEL.strip()),
        )
        db.commit()


def claim_message(source_id: int, message_id: int) -> bool:
    with closing(db_connect()) as db:
        cursor = db.execute(
            "INSERT OR IGNORE INTO processed_messages(source_id, message_id) VALUES (?, ?)",
            (source_id, message_id),
        )
        db.commit()
        return cursor.rowcount > 0


def release_message(source_id: int, message_id: int) -> None:
    with closing(db_connect()) as db:
        db.execute(
            "DELETE FROM processed_messages WHERE source_id = ? AND message_id = ?",
            (source_id, message_id),
        )
        db.commit()


def claim_content(text: str) -> str | None:
    content_hash = hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()
    with closing(db_connect()) as db:
        cursor = db.execute(
            "INSERT OR IGNORE INTO content_hashes(content_hash) VALUES (?)", (content_hash,)
        )
        db.commit()
        return content_hash if cursor.rowcount > 0 else None


def release_content(content_hash: str) -> None:
    with closing(db_connect()) as db:
        db.execute("DELETE FROM content_hashes WHERE content_hash = ?", (content_hash,))
        db.commit()


def source_permalink(source, message_id: int) -> str:
    channel = source["channel"].strip()
    if channel.startswith("@"):
        return f"https://t.me/{channel[1:]}/{message_id}"
    if channel.startswith("-100"):
        return f"https://t.me/c/{channel[4:]}/{message_id}"
    return ""


def format_generated_post(text: str, source_text: str, source_link: str) -> str:
    """Гарантирует обязательные элементы оформления независимо от ответа Gemini."""
    result = text.strip()
    source_tags = re.findall(r"#[^\s,.;:!?]+", source_text)
    source_versions = re.search(r"верси[ия][:\s]+([^\n]+)", source_text, re.IGNORECASE)
    lines = result.splitlines()
    for index, line in enumerate(lines):
        if re.match(r"\s*верси[ия][:\s]*", line, re.IGNORECASE):
            value = line.split(":", 1)[1].strip() if ":" in line else line
            if source_versions:
                value = source_versions.group(1).strip()
            lines[index] = f"<b>Версии:</b> {html.escape(value)}"
        elif re.match(r"\s*теги[и]?[:\s]", line, re.IGNORECASE):
            tags = " ".join(source_tags) if source_tags else line.split(":", 1)[-1].strip()
            lines[index] = f"<b>Теги:</b> {tags}"
        elif "ПОДРОБНЕЕ О ДОПОЛНЕНИИ" in line.upper():
            lines[index] = "<b>🌀 ПОДРОБНЕЕ О ДОПОЛНЕНИИ</b>"
    result = "\n".join(lines)
    if source_link:
        link_html = f'<a href="{source_link}">нажми, чтобы посмотреть</a>'
        if "нажми, чтобы посмотреть" in result:
            result = re.sub(r"нажми, чтобы посмотреть", link_html, result, count=1, flags=re.IGNORECASE)
        elif source_link not in result:
            result += f"\n\n<b>🌀 ПОДРОБНЕЕ О ДОПОЛНЕНИИ</b>\n{link_html}"
    return result


def list_sources(include_disabled: bool = True):
    with closing(db_connect()) as db:
        query = "SELECT * FROM sources"
        if not include_disabled:
            query += " WHERE enabled = 1"
        return db.execute(query + " ORDER BY id").fetchall()


def get_source(source_id: int):
    with closing(db_connect()) as db:
        return db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()


def find_enabled_source(chat_id: int, username: str):
    candidates = {str(chat_id), username.lower(), f"@{username.lower()}"}
    with closing(db_connect()) as db:
        rows = db.execute("SELECT * FROM sources WHERE enabled = 1").fetchall()
    return next(
        (row for row in rows if row["channel"].lower() in candidates),
        None,
    )


def add_source(channel: str, title: str) -> bool:
    with closing(db_connect()) as db:
        cursor = db.execute(
            "INSERT OR IGNORE INTO sources(channel, title) VALUES (?, ?)",
            (channel, title),
        )
        db.commit()
        return cursor.rowcount > 0


def set_source_enabled(source_id: int, enabled: bool) -> None:
    with closing(db_connect()) as db:
        db.execute("UPDATE sources SET enabled = ? WHERE id = ?", (int(enabled), source_id))
        db.commit()


def delete_source(source_id: int) -> None:
    with closing(db_connect()) as db:
        db.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        db.commit()


def update_source_settings(source_id: int, **values) -> None:
    allowed = {"keywords", "prompt", "auto_publish"}
    values = {key: value for key, value in values.items() if key in allowed}
    if not values:
        return
    assignments = ", ".join(f"{key} = ?" for key in values)
    with closing(db_connect()) as db:
        db.execute(
            f"UPDATE sources SET {assignments} WHERE id = ?",
            (*values.values(), source_id),
        )
        db.commit()


def update_source_title(source_id: int, title: str) -> None:
    with closing(db_connect()) as db:
        db.execute("UPDATE sources SET title = ? WHERE id = ?", (title, source_id))
        db.commit()


def save_draft(
    text: str,
    media_type: str | None,
    media_id: str | None,
    original_text: str | None = None,
    source_id: int | None = None,
    source_message_id: int | None = None,
    target_channel: str | None = None,
) -> int:
    with closing(db_connect()) as db:
        cursor = db.execute(
            """INSERT INTO drafts(
                text, media_type, media_id, original_text, source_id, source_message_id, target_channel
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (text, media_type, media_id, original_text, source_id, source_message_id, target_channel or TARGET_CHANNEL),
        )
        db.commit()
        return int(cursor.lastrowid)


def get_draft(draft_id: int):
    with closing(db_connect()) as db:
        return db.execute(
            "SELECT * FROM drafts WHERE id = ? AND status = 'pending'", (draft_id,)
        ).fetchone()


def get_any_draft(draft_id: int):
    with closing(db_connect()) as db:
        return db.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()


def set_status(draft_id: int, status: str) -> None:
    with closing(db_connect()) as db:
        db.execute("UPDATE drafts SET status = ? WHERE id = ?", (status, draft_id))
        db.commit()


def update_draft_text(draft_id: int, text: str) -> None:
    with closing(db_connect()) as db:
        db.execute("UPDATE drafts SET text = ? WHERE id = ?", (text, draft_id))
        db.commit()


def recent_pending_draft(source_id: int, message_id: int):
    with closing(db_connect()) as db:
        return db.execute(
            """SELECT * FROM drafts WHERE source_id = ? AND status = 'pending'
               AND source_message_id BETWEEN ? AND ? ORDER BY id DESC LIMIT 1""",
            (source_id, max(0, message_id - 10), message_id - 1),
        ).fetchone()


def update_draft_media(draft_id: int, media_type: str, media_id: str) -> None:
    with closing(db_connect()) as db:
        db.execute(
            "UPDATE drafts SET media_type = ?, media_id = ? WHERE id = ?",
            (media_type, media_id, draft_id),
        )
        db.commit()


def schedule_draft(draft_id: int, scheduled_at: str) -> None:
    with closing(db_connect()) as db:
        db.execute(
            "UPDATE drafts SET scheduled_at = ? WHERE id = ? AND status = 'pending'",
            (scheduled_at, draft_id),
        )
        db.commit()


def due_drafts():
    with closing(db_connect()) as db:
        return db.execute(
            """SELECT * FROM drafts WHERE status = 'pending'
               AND scheduled_at IS NOT NULL AND scheduled_at <= datetime('now')"""
        ).fetchall()


def stats():
    with closing(db_connect()) as db:
        return db.execute(
            "SELECT status, COUNT(*) AS count FROM drafts GROUP BY status"
        ).fetchall()


def stats_since(days: int):
    with closing(db_connect()) as db:
        return db.execute(
            "SELECT status, COUNT(*) AS count FROM drafts "
            "WHERE created_at >= datetime('now', ?) GROUP BY status",
            (f"-{days} days",),
        ).fetchall()


def analytics_by_source(days: int | None = None):
    time_filter = ""
    params = []
    if days:
        time_filter = " AND d.created_at >= datetime('now', ?)"
        params.append(f"-{days} days")
    with closing(db_connect()) as db:
        return db.execute(
            """SELECT COALESCE(s.title, 'Неизвестный источник') AS source_title,
                      d.status, COUNT(*) AS count
               FROM drafts d LEFT JOIN sources s ON s.id = d.source_id
               WHERE 1=1""" + time_filter +
            " GROUP BY source_title, d.status ORDER BY source_title",
            params,
        ).fetchall()


def history(limit: int = 10):
    with closing(db_connect()) as db:
        return db.execute(
            "SELECT * FROM drafts ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def search_history(query: str, limit: int = 10):
    with closing(db_connect()) as db:
        value = f"%{query}%"
        return db.execute(
            "SELECT * FROM drafts WHERE text LIKE ? OR original_text LIKE ? "
            "ORDER BY id DESC LIMIT ?",
            (value, value, limit),
        ).fetchall()


def cancel_scheduled(draft_id: int) -> None:
    with closing(db_connect()) as db:
        db.execute("UPDATE drafts SET scheduled_at = NULL WHERE id = ?", (draft_id,))
        db.commit()


def set_draft_target(draft_id: int, target: str) -> None:
    with closing(db_connect()) as db:
        db.execute("UPDATE drafts SET target_channel = ? WHERE id = ?", (target, draft_id))
        db.commit()


def cleanup_old(days: int) -> int:
    with closing(db_connect()) as db:
        cursor = db.execute(
            "DELETE FROM drafts WHERE status IN ('published', 'rejected') "
            "AND created_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        db.commit()
        return cursor.rowcount


async def dashboard_page(request: web.Request) -> web.Response:
    return web.Response(
        content_type="text/html",
        text="""<!doctype html><meta charset='utf-8'>
        <title>0x00 SPACE Dashboard</title>
        <style>body{font:16px system-ui;max-width:900px;margin:30px auto}pre{background:#f3f3f3;padding:15px;border-radius:8px}</style>
        <h1>0x00 SPACE Dashboard</h1><p>Данные обновляются каждые 10 секунд.</p>
        <pre id='data'>Загрузка...</pre>
        <script>async function load(){let r=await fetch('/api/overview');document.getElementById('data').textContent=JSON.stringify(await r.json(),null,2)}load();setInterval(load,10000)</script>""",
    )


async def dashboard_overview(request: web.Request) -> web.Response:
    source_data = [dict(row) for row in list_sources()]
    draft_data = [dict(row) for row in history(30)]
    status_data = {row["status"]: row["count"] for row in stats()}
    return web.json_response({
        "status": status_data,
        "sources": source_data,
        "drafts": draft_data,
        "test_mode": TEST_MODE,
    })


async def dashboard_server() -> None:
    app = web.Application()
    app.router.add_get("/", dashboard_page)
    app.router.add_get("/api/overview", dashboard_overview)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_HOST, WEB_PORT)
    await site.start()
    logging.info("Локальная панель: http://%s:%s", WEB_HOST, WEB_PORT)
    await asyncio.Event().wait()


async def adapt_text(
    source_text: str,
    source_prompt: str = "",
    source_link: str = "",
) -> str:
    async with gemini_semaphore:
        for attempt in range(3):
            try:
                response = await asyncio.to_thread(
                    gemini.models.generate_content,
                    model=GEMINI_MODEL,
                    contents=source_text,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT
                        + (f"\n\nДополнительные правила источника:\n{source_prompt}" if source_prompt else "")
                        + (f"\n\nОбязательная ссылка на оригинал: {source_link}" if source_link else ""),
                        temperature=0.7,
                    ),
                )
                break
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(2 ** attempt)
    result = (response.text or "").strip()
    if not result:
        raise RuntimeError("Gemini вернул пустой ответ")
    return result


def is_admin(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in ADMIN_USER_IDS)


def keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Опубликовать", callback_data=f"publish:{draft_id}")],
            [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{draft_id}")],
            [InlineKeyboardButton(text="✏️ Как изменить", callback_data=f"edit:{draft_id}"),
             InlineKeyboardButton(text="🕒 Как запланировать", callback_data=f"schedule:{draft_id}")],
            [InlineKeyboardButton(text="🔁 Повторить обработку", callback_data=f"retry:{draft_id}")],
        ]
    )


def republish_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔁 Опубликовать снова", callback_data=f"republish:{draft_id}")]
    ])


async def edit_moderation_message(message: Message, text: str, reply_markup=None) -> None:
    if message.photo or message.video or message.document or message.audio \
            or message.voice or message.animation:
        await message.edit_caption(caption=text, reply_markup=reply_markup)
    else:
        await message.edit_text(text, reply_markup=reply_markup)


def sources_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    rows = []
    all_sources = list_sources()
    page_size = 5
    sources = all_sources[page * page_size : (page + 1) * page_size]
    for source in sources:
        source_id = source["id"]
        state = "✅" if source["enabled"] else "⏸"
        title = source["title"][:28]
        rows.append([InlineKeyboardButton(
            text=f"{state} {title}", callback_data=f"source:last5:{source_id}"
        )])
        rows.append([
            InlineKeyboardButton(text="📥 5 последних", callback_data=f"source:last5:{source_id}"),
            InlineKeyboardButton(text="⚙️", callback_data=f"source:settings:{source_id}"),
            InlineKeyboardButton(
                text="⏸ Выкл." if source["enabled"] else "▶ Вкл.",
                callback_data=f"source:toggle:{source_id}",
            ),
            InlineKeyboardButton(text="🗑", callback_data=f"source:delete:{source_id}"),
        ])
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="⬅️", callback_data=f"source:page:{page - 1}"))
    if (page + 1) * page_size < len(all_sources):
        navigation.append(InlineKeyboardButton(text="➡️", callback_data=f"source:page:{page + 1}"))
    if navigation:
        rows.append(navigation)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def source_settings_keyboard(source_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Ключевые слова", callback_data=f"source:settings_keywords:{source_id}")],
        [InlineKeyboardButton(text="🧠 Дополнительный промпт", callback_data=f"source:settings_prompt:{source_id}")],
        [InlineKeyboardButton(text="⚡ Автопубликация", callback_data=f"source:settings_auto:{source_id}")],
        [InlineKeyboardButton(text="⬅️ Назад к источникам", callback_data="source:back:0")],
    ])


def source_count_keyboard(source_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1 пост", callback_data=f"source:count1:{source_id}"),
            InlineKeyboardButton(text="3 поста", callback_data=f"source:count3:{source_id}"),
            InlineKeyboardButton(text="5 постов", callback_data=f"source:count5:{source_id}"),
        ],
        [InlineKeyboardButton(text="⬅️ Назад к источникам", callback_data="source:back:0")],
    ])


async def show_sources(message: Message) -> None:
    sources = list_sources()
    if not sources:
        text = "Источников пока нет.\nДобавьте канал командой /addsource @channel"
    else:
        text = (
            "<b>Источники</b>\n"
            "✅ — сообщения обрабатываются автоматически\n"
            "⏸ — источник выключен\n\n"
        "Нажмите «📥 5 последних», затем выберите 1, 3 или 5 постов."
        )
    await message.answer(text, reply_markup=sources_keyboard())


async def send_preview(
    draft_id: int,
    text: str,
    media_type: str | None = None,
    media_id: str | None = None,
    temporary_message_id: int | None = None,
) -> None:
    if temporary_message_id:
        try:
            await bot.delete_message(ADMIN_USER_ID, temporary_message_id)
        except Exception:
            logging.debug("Не удалось удалить временное медиа", exc_info=True)
    if media_type and media_id and media_type != "album":
        caption = f"<b>Новый черновик #{draft_id}</b>\n\n{text}"
        if len(caption) <= 1024:
            try:
                await send_media_preview(media_type, media_id, caption, keyboard(draft_id))
                return
            except Exception:
                logging.exception("Не удалось отправить медиапредпросмотр")
        await send_media_preview(media_type, media_id, None, keyboard(draft_id))
        await send_text_chunks(ADMIN_USER_ID, f"<b>Новый черновик #{draft_id}</b>\n\n{text}")
        return
    # Если Gemini случайно вернул невалидный HTML, черновик всё равно будет доставлен.
    try:
        await bot.send_message(
            ADMIN_USER_ID,
            f"<b>Новый черновик #{draft_id}</b>\n\n{text}",
            reply_markup=keyboard(draft_id),
        )
    except Exception:
        await bot.send_message(
            ADMIN_USER_ID,
            f"Новый черновик #{draft_id}\n\n{html.escape(text)}",
            reply_markup=keyboard(draft_id),
            parse_mode=ParseMode.HTML,
        )


async def send_media_preview(media_type, media_id, caption, reply_markup):
    kwargs = {"caption": caption, "reply_markup": reply_markup} if caption else {"reply_markup": reply_markup}
    if media_type == "photo":
        return await bot.send_photo(ADMIN_USER_ID, media_id, **kwargs)
    if media_type == "video":
        return await bot.send_video(ADMIN_USER_ID, media_id, **kwargs)
    if media_type == "voice":
        return await bot.send_voice(ADMIN_USER_ID, media_id, **kwargs)
    if media_type == "audio":
        return await bot.send_audio(ADMIN_USER_ID, media_id, **kwargs)
    if media_type == "animation":
        return await bot.send_animation(ADMIN_USER_ID, media_id, **kwargs)
    return await bot.send_document(ADMIN_USER_ID, media_id, **kwargs)


async def upload_source_media(message) -> tuple[str | None, str | None, int | None]:
    """Передаёт медиа от UserBot боту и возвращает bot API file_id.

    File ID, полученный ботом, можно использовать позже при публикации.
    Это необходимо, поскольку file_id Telethon и Bot API несовместимы.
    """
    if message.photo:
        media_type = "photo"
    elif message.video:
        media_type = "video"
    elif message.voice:
        media_type = "voice"
    elif message.audio:
        media_type = "audio"
    elif message.gif:
        media_type = "animation"
    elif message.document:
        media_type = "document"
    else:
        return None, None, None
    file_size = getattr(getattr(message, "file", None), "size", None)
    if file_size and file_size > MAX_MEDIA_MB * 1024 * 1024:
        raise RuntimeError(f"Медиафайл больше установленного лимита {MAX_MEDIA_MB:g} MB")
    filename = (getattr(getattr(message, "file", None), "name", None) or "").lower()
    extension = filename.rsplit(".", 1)[-1] if "." in filename else ""
    if extension in BLOCKED_EXTENSIONS:
        raise RuntimeError(f"Расширение .{extension} запрещено")

    with tempfile.TemporaryDirectory() as temp_dir:
        media_path = await message.download_media(file=temp_dir)
        if not media_path:
            raise RuntimeError("Не удалось скачать медиа из исходного канала")
        uploaded = FSInputFile(media_path)
        if media_type == "photo":
            result = await bot.send_photo(ADMIN_USER_ID, uploaded)
            return media_type, result.photo[-1].file_id, result.message_id
        if media_type == "video":
            result = await bot.send_video(ADMIN_USER_ID, uploaded)
            return media_type, result.video.file_id, result.message_id
        if media_type == "voice":
            result = await bot.send_voice(ADMIN_USER_ID, uploaded)
            return media_type, result.voice.file_id, result.message_id
        if media_type == "audio":
            result = await bot.send_audio(ADMIN_USER_ID, uploaded)
            return media_type, result.audio.file_id, result.message_id
        if media_type == "animation":
            result = await bot.send_animation(ADMIN_USER_ID, uploaded)
            return media_type, result.animation.file_id, result.message_id
        result = await bot.send_document(ADMIN_USER_ID, uploaded)
        return media_type, result.document.file_id, result.message_id


async def send_text_chunks(chat_id: str, text: str, parse_mode=ParseMode.HTML) -> None:
    for offset in range(0, len(text), 4000):
        await bot.send_message(chat_id, text[offset : offset + 4000], parse_mode=parse_mode)


async def publish_draft(draft) -> None:
    media_type = draft["media_type"]
    media_id = draft["media_id"]
    text = draft["text"]
    target = str(ADMIN_USER_ID) if TEST_MODE else (draft["target_channel"] or TARGET_CHANNEL)

    # Telegram ограничивает подпись к фото/видео 1024 символами.
    if media_type and len(text) > 1024:
        if media_type == "album":
            await publish_album(media_id, "", target)
            await send_text_chunks(target, text)
            return
        if media_type == "bundle":
            await publish_bundle(media_id, "", target)
            await send_text_chunks(target, text)
            return
        if media_type == "photo":
            await bot.send_photo(target, media_id)
        elif media_type == "video":
            await bot.send_video(target, media_id)
        elif media_type == "voice":
            await bot.send_voice(target, media_id)
        elif media_type == "audio":
            await bot.send_audio(target, media_id)
        elif media_type == "animation":
            await bot.send_animation(target, media_id)
        elif media_type == "document":
            await bot.send_document(target, media_id)
        await send_text_chunks(target, text)
        return

    try:
        if media_type == "photo":
            await bot.send_photo(target, media_id, caption=text)
        elif media_type == "video":
            await bot.send_video(target, media_id, caption=text)
        elif media_type == "voice":
            await bot.send_voice(target, media_id, caption=text)
        elif media_type == "audio":
            await bot.send_audio(target, media_id, caption=text)
        elif media_type == "animation":
            await bot.send_animation(target, media_id, caption=text)
        elif media_type == "document":
            await bot.send_document(target, media_id, caption=text)
        elif media_type == "album":
            await publish_album(media_id, text, target)
        elif media_type == "bundle":
            await publish_bundle(media_id, text, target)
        else:
            await bot.send_message(target, text)
    except Exception:
        # Не теряем публикацию, если модель вернула символ, который ломает HTML.
        plain_text = html.unescape(text)
        if media_type == "photo":
            await bot.send_photo(target, media_id, caption=plain_text, parse_mode=None)
        elif media_type == "video":
            await bot.send_video(target, media_id, caption=plain_text, parse_mode=None)
        elif media_type == "voice":
            await bot.send_voice(target, media_id, caption=plain_text, parse_mode=None)
        elif media_type == "audio":
            await bot.send_audio(target, media_id, caption=plain_text, parse_mode=None)
        elif media_type == "animation":
            await bot.send_animation(target, media_id, caption=plain_text, parse_mode=None)
        elif media_type == "document":
            await bot.send_document(target, media_id, caption=plain_text, parse_mode=None)
        elif media_type == "album":
            await publish_album(media_id, plain_text, target)
        elif media_type == "bundle":
            await publish_bundle(media_id, plain_text, target)
        else:
            await send_text_chunks(target, plain_text, parse_mode=None)


async def publish_album(media_id: str, caption: str, target: str = TARGET_CHANNEL) -> None:
    items = json.loads(media_id)
    media = []
    for index, item in enumerate(items):
        item_caption = caption if index == 0 else None
        if item["type"] == "photo":
            media.append(InputMediaPhoto(media=item["id"], caption=item_caption))
        elif item["type"] == "video":
            media.append(InputMediaVideo(media=item["id"], caption=item_caption))
    if media:
        await bot.send_media_group(target, media)


async def publish_bundle(media_id: str, caption: str, target: str) -> None:
    """Публикует связанные медиа последовательно, сохраняя общий текст."""
    items = json.loads(media_id)
    first = True
    for item in items:
        item_caption = caption if first else None
        first = False
        if item["type"] == "photo":
            await bot.send_photo(target, item["id"], caption=item_caption)
        elif item["type"] == "video":
            await bot.send_video(target, item["id"], caption=item_caption)
        elif item["type"] == "document":
            await bot.send_document(
                target,
                item["id"],
                caption=item_caption or "📎 Скачать файл мода",
            )


@dp.message(CommandStart())
async def start(message: Message) -> None:
    if not is_admin(message):
        await message.answer(
            f"Ваш Telegram ID: <code>{message.from_user.id}</code>\n"
            "Добавьте его в ADMIN_USER_ID в файле .env."
        )
        return
    await message.answer(
        "✅ Бот работает. Я возьму новый пост из исходного канала, "
        "адаптирую его через Gemini и пришлю сюда кнопку публикации."
    )


@dp.message(Command("id"))
async def user_id(message: Message) -> None:
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


@dp.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(
        "<b>Команды:</b>\n"
        "/start — проверить работу бота\n"
        "/id — узнать свой Telegram ID\n"
        "/sources — управление источниками\n"
        "/refreshsources — обновить названия каналов\n"
        "/addsource @channel — добавить источник\n"
        "/last5 — выбрать источник и обработать 5 последних постов\n"
        "/stats — статистика\n"
        "/history — история публикаций\n"
        "/cancel ID — отменить расписание\n"
        "/target ID @channel — канал назначения\n"
        "/cleanup дней — очистить старые записи\n"
        "/setkeywords ID слова — фильтр источника\n"
        "/setprompt ID текст — правила источника\n"
        "/autopublish ID on|off — автопубликация\n"
        "/edit ID текст — изменить черновик\n"
        "/retry ID — повторить обработку\n"
        "/schedule ID YYYY-MM-DD HH:MM — запланировать"
    )


@dp.message(Command("sources"))
async def sources_command(message: Message) -> None:
    if is_admin(message):
        await show_sources(message)


@dp.message(Command("refreshsources"))
async def refresh_sources_command(message: Message) -> None:
    if not is_admin(message):
        return
    updated = 0
    for source in list_sources():
        try:
            entity = await user_client.get_entity(source["channel"])
            title = getattr(entity, "title", None)
            if title and title != source["title"]:
                update_source_title(source["id"], title)
                updated += 1
        except Exception:
            logging.warning("Не удалось обновить название источника %s", source["channel"])
    await message.answer(f"✅ Обновлено названий каналов: {updated}.")


@dp.message(Command("addsource"))
async def add_source_command(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer(
            "Пришлите username, ссылку или ID канала следующим сообщением."
        )
        await state.set_state(SourceSettingsState.waiting_addsource)
        return
    await register_source(message, parts[1].strip())


async def register_source(message: Message, channel: str) -> None:
    try:
        entity = await user_client.get_entity(channel)
        if not getattr(entity, "broadcast", False):
            await message.answer("❌ Указанный объект не является Telegram-каналом.")
            return
        canonical = (
            f"@{entity.username}"
            if getattr(entity, "username", None)
            else str(utils.get_peer_id(entity))
        )
        title = getattr(entity, "title", None) or canonical
        if not add_source(canonical, title):
            await message.answer("ℹ️ Этот источник уже добавлен.")
            return
        await message.answer(
            f"✅ Источник добавлен: <b>{html.escape(title)}</b>\n"
            "Новые посты из включённых источников будут обрабатываться автоматически."
        )
    except Exception:
        logging.exception("Не удалось добавить источник %s", channel)
        await message.answer(
            "❌ Не удалось найти канал. Проверьте username/ID и убедитесь, "
            "что личный аккаунт подписан на него."
        )


@dp.message(SourceSettingsState.waiting_addsource)
async def add_source_input(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        await state.clear()
        return
    await state.clear()
    await register_source(message, (message.text or "").strip())


@dp.message(Command("setkeywords"))
async def set_keywords_command(message: Message) -> None:
    if not is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: <code>/setkeywords ID слово1,слово2</code>")
        return
    update_source_settings(int(parts[1]), keywords=parts[2].strip() if len(parts) == 3 else "")
    await message.answer("✅ Фильтр ключевых слов обновлён. Пустое значение отключает фильтр.")


@dp.message(Command("setprompt"))
async def set_prompt_command(message: Message) -> None:
    if not is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: <code>/setprompt ID дополнительные правила</code>")
        return
    update_source_settings(int(parts[1]), prompt=parts[2].strip() if len(parts) == 3 else "")
    await message.answer("✅ Дополнительный промпт источника обновлён.")


@dp.message(Command("autopublish"))
async def autopublish_command(message: Message) -> None:
    if not is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in {"on", "off"}:
        await message.answer("Использование: <code>/autopublish ID on|off</code>")
        return
    update_source_settings(int(parts[1]), auto_publish=parts[2] == "on")
    await message.answer("✅ Автопубликация обновлена.")


@dp.message(SourceSettingsState.waiting_keywords)
async def source_keywords_input(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        await state.clear()
        return
    data = await state.get_data()
    update_source_settings(data["source_id"], keywords=(message.text or "").strip())
    await state.clear()
    await message.answer("✅ Ключевые слова сохранены. Пустой текст отключает фильтр.")


@dp.message(SourceSettingsState.waiting_prompt)
async def source_prompt_input(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        await state.clear()
        return
    data = await state.get_data()
    update_source_settings(data["source_id"], prompt=(message.text or "").strip())
    await state.clear()
    await message.answer("✅ Дополнительный промпт сохранён.")


@dp.message(SourceSettingsState.waiting_edit)
async def draft_edit_input(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        await state.clear()
        return
    data = await state.get_data()
    draft_id = data["draft_id"]
    if not get_draft(draft_id):
        await state.clear()
        await message.answer("Черновик уже обработан или не найден.")
        return
    update_draft_text(draft_id, (message.text or "").strip())
    await state.clear()
    await message.answer(f"✅ Текст черновика #{draft_id} обновлён.", reply_markup=keyboard(draft_id))


@dp.message(Command("stats"))
async def stats_command(message: Message) -> None:
    if not is_admin(message):
        return
    values = {row["status"]: row["count"] for row in stats()}
    today = {row["status"]: row["count"] for row in stats_since(1)}
    week = {row["status"]: row["count"] for row in stats_since(7)}
    source_counts = {}
    for row in analytics_by_source():
        bucket = source_counts.setdefault(row["source_title"], {})
        bucket[row["status"]] = row["count"]
    source_lines = []
    for title, counts in source_counts.items():
        total = sum(counts.values())
        published = counts.get("published", 0)
        rate = round(published * 100 / total) if total else 0
        source_lines.append(
            f"• {html.escape(title)}: {total}, опубликовано {published} ({rate}%)"
        )
    await message.answer(
        "<b>Статистика</b>\n"
        f"⏳ На модерации: {values.get('pending', 0)}\n"
        f"✅ Опубликовано: {values.get('published', 0)}\n"
        f"❌ Отклонено: {values.get('rejected', 0)}\n\n"
        f"За сегодня: {sum(today.values())}; за неделю: {sum(week.values())}\n"
        f"Эффективность публикаций: {values.get('published', 0) * 100 // max(sum(values.values()), 1)}%\n\n"
        "<b>По источникам:</b>\n" + ("\n".join(source_lines) or "нет данных")
    )


@dp.message(Command("history"))
async def history_command(message: Message) -> None:
    if not is_admin(message):
        return
    rows = history(15)
    if not rows:
        await message.answer("История пока пуста.")
        return
    for row in rows:
        icon = {"pending": "⏳", "published": "✅", "rejected": "❌"}.get(row["status"], "•")
        await message.answer(
            f"{icon} <b>#{row['id']}</b> — {row['status']} — {row['created_at']}",
            reply_markup=republish_keyboard(row["id"]),
        )


@dp.message(Command("search"))
async def search_command(message: Message) -> None:
    if not is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Использование: <code>/search слово</code>")
        return
    rows = search_history(parts[1].strip())
    if not rows:
        await message.answer("Ничего не найдено.")
        return
    for row in rows:
        await message.answer(
            f"#{row['id']} — {row['status']}\n{html.escape(row['text'][:500])}",
            reply_markup=republish_keyboard(row["id"]),
        )


@dp.message(Command("force"))
async def force_command(message: Message) -> None:
    if not is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: <code>/force ID</code>")
        return
    draft = get_any_draft(int(parts[1]))
    if not draft or not draft["source_id"] or not draft["source_message_id"]:
        await message.answer("У записи нет привязки к исходному сообщению.")
        return
    source = get_source(draft["source_id"])
    if not source:
        await message.answer("Источник записи удалён.")
        return
    try:
        source_message = await user_client.get_messages(
            source["channel"], ids=draft["source_message_id"]
        )
        if not source_message:
            await message.answer("Исходное сообщение не найдено.")
            return
        await process_source_message(source_message, source, force=True)
        await message.answer("✅ Пост принудительно отправлен на обработку.")
    except Exception:
        logging.exception("Ошибка принудительной обработки")
        await message.answer("❌ Не удалось повторно загрузить исходный пост.")


@dp.message(Command("cancel"))
async def cancel_command(message: Message) -> None:
    if not is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: <code>/cancel ID</code>")
        return
    cancel_scheduled(int(parts[1]))
    await message.answer(f"✅ Расписание черновика #{parts[1]} отменено.")


@dp.message(Command("target"))
async def target_command(message: Message) -> None:
    if not is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) != 3 or not parts[1].isdigit():
        await message.answer("Использование: <code>/target ID @channel</code>")
        return
    target = parts[2].strip()
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(target, me.id)
        if member.status not in {"administrator", "creator"}:
            await message.answer("❌ Бот не является администратором канала назначения.")
            return
        set_draft_target(int(parts[1]), target)
        await message.answer(f"✅ Для черновика #{parts[1]} выбран канал {html.escape(target)}.")
    except Exception:
        await message.answer("❌ Не удалось проверить канал. Укажите @username и добавьте туда бота.")


@dp.message(Command("cleanup"))
async def cleanup_command(message: Message) -> None:
    if not is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: <code>/cleanup 30</code>")
        return
    deleted = cleanup_old(int(parts[1]))
    await message.answer(f"🧹 Удалено старых записей: {deleted}.")


@dp.message(Command("edit"))
async def edit_command(message: Message) -> None:
    if not is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.answer("Использование: <code>/edit ID новый текст</code>")
        return
    draft = get_draft(int(parts[1]))
    if not draft:
        await message.answer("Черновик не найден или уже обработан.")
        return
    update_draft_text(int(parts[1]), parts[2])
    await message.answer(f"✅ Текст черновика #{parts[1]} обновлён.", reply_markup=keyboard(int(parts[1])))


@dp.message(Command("retry"))
async def retry_command(message: Message) -> None:
    if not is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: <code>/retry ID</code>")
        return
    draft = get_draft(int(parts[1]))
    if not draft or not draft["original_text"]:
        await message.answer("Нет исходного текста для повторной обработки.")
        return
    try:
        source = get_source(draft["source_id"]) if draft["source_id"] else None
        link = source_permalink(source, draft["source_message_id"]) if source else ""
        adapted = format_generated_post(
            await adapt_text(draft["original_text"], source["prompt"] if source else "", link),
            draft["original_text"],
            link,
        )
        update_draft_text(int(parts[1]), adapted)
        await message.answer(f"✅ Черновик #{parts[1]} обработан заново.", reply_markup=keyboard(int(parts[1])))
    except Exception:
        logging.exception("Ошибка повторной обработки черновика")
        await message.answer("❌ Gemini не смог обработать текст.")


@dp.message(Command("schedule"))
async def schedule_command(message: Message) -> None:
    if not is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) != 3 or not parts[1].isdigit():
        await message.answer("Использование: <code>/schedule ID 2026-09-16 18:30</code>\nВремя указывается в UTC.")
        return
    try:
        local_time = datetime.strptime(parts[2], "%Y-%m-%d %H:%M").replace(
            tzinfo=ZoneInfo(TIMEZONE)
        )
        scheduled_at = local_time.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        schedule_draft(int(parts[1]), scheduled_at)
        await message.answer(f"🕒 Черновик #{parts[1]} запланирован на {parts[2]} ({TIMEZONE}).")
    except ValueError:
        await message.answer("Неверный формат даты. Пример: <code>2026-09-16 18:30</code>")


async def process_source_message(message, source=None, force: bool = False) -> bool:
    source_text = message.raw_text or ""
    has_media = any(
        getattr(message, attribute, None)
        for attribute in ("photo", "video", "voice", "audio", "gif", "document")
    )
    if not source_text and not has_media:
        return False
    if not source_text and source and message.document and not force:
        # Файлы модов часто идут отдельным сообщением сразу после описания.
        related = recent_pending_draft(source["id"], message.id)
        if related and claim_message(source["id"], message.id):
            try:
                media_type, media_id, _ = await upload_source_media(message)
                items = []
                if related["media_type"] == "bundle":
                    items = json.loads(related["media_id"])
                elif related["media_type"] and related["media_id"]:
                    items.append({"type": related["media_type"], "id": related["media_id"]})
                if media_type and media_id:
                    items.append({"type": media_type, "id": media_id})
                    update_draft_media(related["id"], "bundle", json.dumps(items))
                    await bot.send_message(
                        ADMIN_USER_ID,
                        f"📎 Файл прикреплён к черновику #{related['id']}."
                    )
                    return True
            except Exception:
                release_message(source["id"], message.id)
                logging.exception("Не удалось прикрепить файл к черновику")
                return False
    if not source_text:
        filename = getattr(getattr(message, "file", None), "name", None)
        source_text = (
            f"В исходном канале опубликован файл {filename or 'без названия'}. "
            "Подготовь короткое описание публикации, не выдумывая характеристики файла."
        )

    if not source:
        return False
    if not force and not claim_message(source["id"], message.id):
        logging.info("Пропускаю уже обработанный пост %s/%s", source["id"], message.id)
        return False
    keywords = [item.strip().lower() for item in source["keywords"].split(",") if item.strip()]
    if keywords and not any(keyword in source_text.lower() for keyword in keywords):
        return False
    if BLOCKED_KEYWORDS and any(word in source_text.lower() for word in BLOCKED_KEYWORDS):
        logging.info("Пост %s отфильтрован по BLOCKED_KEYWORDS", message.id)
        return False
    hashtags = {item.lower().lstrip("#") for item in re.findall(r"#[\w_]+", source_text)}
    if REQUIRED_HASHTAGS and not REQUIRED_HASHTAGS.intersection(hashtags):
        return False
    if BLOCKED_LINK_DOMAINS and any(
        domain in source_text.lower() for domain in BLOCKED_LINK_DOMAINS
    ):
        return False
    if str(getattr(message, "sender_id", "")) in BLOCKED_AUTHOR_IDS:
        return False
    content_hash = None if force else claim_content(source_text)
    if not force and not content_hash:
        logging.info("Пост %s пропущен: такой текст уже обрабатывался", message.id)
        return False
    try:
        async with processing_lock:
            source_link = source_permalink(source, message.id)
            adapted = format_generated_post(
                await adapt_text(source_text, source["prompt"], source_link),
                source_text,
                source_link,
            )
            media_type, media_id, temporary_message_id = await upload_source_media(message)
            draft_id = save_draft(
                adapted,
                media_type,
                media_id,
                original_text=source_text,
                source_id=source["id"],
                source_message_id=message.id,
            )
            if source["auto_publish"]:
                await publish_draft(get_draft(draft_id))
                set_status(draft_id, "published")
                await bot.send_message(ADMIN_USER_ID, f"✅ Пост #{draft_id} опубликован автоматически.")
            else:
                await send_preview(draft_id, adapted, media_type, media_id, temporary_message_id)
        return True
    except Exception:
        if not force:
            release_message(source["id"], message.id)
            if content_hash:
                release_content(content_hash)
        logging.exception("Не удалось обработать пост из источника")
        try:
            await bot.send_message(
                ADMIN_USER_ID,
                f"⚠️ Не удалось обработать пост из источника «{html.escape(source['title'])}». "
                "Подробности записаны в консоль.",
            )
        except Exception:
            logging.exception("Не удалось отправить уведомление об ошибке")
        return False


async def process_source_album(messages, source) -> bool:
    """Сохраняет фото/видео-альбом как одну публикацию."""
    if not messages:
        return False
    first = messages[0]
    if not claim_message(source["id"], first.id):
        return False
    source_text = next((item.raw_text for item in messages if item.raw_text), "")
    if not source_text:
        release_message(source["id"], first.id)
        return False
    try:
        async with processing_lock:
            source_link = source_permalink(source, first.id)
            adapted = format_generated_post(
                await adapt_text(source_text, source["prompt"], source_link),
                source_text,
                source_link,
            )
            media = []
            for item in messages:
                media_type, media_id, _ = await upload_source_media(item)
                if media_type in {"photo", "video"} and media_id:
                    media.append({"type": media_type, "id": media_id})
            if not media:
                release_message(source["id"], first.id)
                return False
            draft_id = save_draft(
                adapted,
                "album",
                json.dumps(media),
                original_text=source_text,
                source_id=source["id"],
                source_message_id=first.id,
            )
            if source["auto_publish"]:
                await publish_draft(get_draft(draft_id))
                set_status(draft_id, "published")
                await bot.send_message(ADMIN_USER_ID, f"✅ Альбом #{draft_id} опубликован автоматически.")
            else:
                await send_preview(draft_id, adapted)
        return True
    except Exception:
        release_message(source["id"], first.id)
        logging.exception("Не удалось обработать альбом из источника")
        return False


@user_client.on(events.NewMessage)
async def source_post(event) -> None:
    """Читает канал от имени личного аккаунта, без прав администратора."""
    chat = await event.get_chat()
    username = (getattr(chat, "username", None) or "").lower()
    source = find_enabled_source(event.chat_id, username)
    if not source:
        return
    if event.message.grouped_id:
        # Альбом будет обработан единым событием events.Album.
        return
    await process_source_message(event.message, source)


@user_client.on(events.Album)
async def source_album(event) -> None:
    chat = await event.get_chat()
    username = (getattr(chat, "username", None) or "").lower()
    source = find_enabled_source(event.chat_id, username)
    if source:
        await process_source_album(event.messages, source)


async def process_last_posts(source, status_message: Message, limit: int) -> None:
    recent_messages = [
        item async for item in user_client.iter_messages(source["channel"], limit=limit)
    ]
    processed = 0
    total = len(recent_messages)
    for index, source_message in enumerate(reversed(recent_messages), start=1):
        if await process_source_message(source_message, source):
            processed += 1
        await status_message.edit_text(
            f"⏳ Обработано {index} из {total} для "
            f"<b>{html.escape(source['title'])}</b>..."
        )
    await status_message.edit_text(
        f"✅ Готово для <b>{html.escape(source['title'])}</b>. "
        f"Создано черновиков: {processed} из {total}."
    )


@dp.message(Command("last5"))
async def last_five_posts(message: Message) -> None:
    """Открывает выбор источника и количества последних сообщений."""
    if not is_admin(message):
        return
    await message.answer("Выберите источник:", reply_markup=sources_keyboard())


@dp.callback_query(F.data.startswith("source:"))
async def source_action(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id not in ADMIN_USER_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    _, action, raw_id = callback.data.split(":", 2)
    if action == "back":
        await callback.message.edit_reply_markup(reply_markup=sources_keyboard(0))
        await callback.answer()
        return
    if action == "page":
        await callback.message.edit_reply_markup(reply_markup=sources_keyboard(int(raw_id)))
        await callback.answer()
        return
    source = get_source(int(raw_id))
    if not source:
        await callback.answer("Источник не найден", show_alert=True)
        return

    if action == "toggle":
        set_source_enabled(source["id"], not source["enabled"])
        await callback.message.edit_reply_markup(reply_markup=sources_keyboard())
        await callback.answer("Статус источника изменён")
        return
    if action == "delete":
        delete_source(source["id"])
        await callback.message.edit_reply_markup(reply_markup=sources_keyboard())
        await callback.answer("Источник удалён")
        return
    if action == "settings":
        await callback.message.edit_reply_markup(reply_markup=source_settings_keyboard(source["id"]))
        await callback.answer("Выберите настройку")
        return
    if action == "settings_keywords":
        await state.set_state(SourceSettingsState.waiting_keywords)
        await state.update_data(source_id=source["id"])
        await callback.message.answer("Введите ключевые слова через запятую или пустое значение для отключения:")
        await callback.answer()
        return
    if action == "settings_prompt":
        await state.set_state(SourceSettingsState.waiting_prompt)
        await state.update_data(source_id=source["id"])
        await callback.message.answer("Введите дополнительные правила для Gemini:")
        await callback.answer()
        return
    if action == "settings_auto":
        update_source_settings(source["id"], auto_publish=not source["auto_publish"])
        await callback.message.edit_reply_markup(reply_markup=sources_keyboard())
        await callback.answer("Автопубликация переключена")
        return
    if action == "last5":
        await callback.message.edit_reply_markup(reply_markup=source_count_keyboard(source["id"]))
        await callback.answer("Выберите количество постов")
        return
    if action.startswith("count"):
        limit = int(action.removeprefix("count"))
        await callback.answer(f"Загружаю последние {limit} пост(а/ов)...")
        status_message = await callback.message.answer("⏳ Обрабатываю выбранный источник...")
        try:
            await process_last_posts(source, status_message, limit)
        except Exception:
            logging.exception("Не удалось загрузить последние сообщения источника")
            await status_message.edit_text(
                "❌ Не удалось загрузить сообщения. Проверьте подписку личного аккаунта."
            )


@dp.callback_query(F.data.startswith("republish:"))
async def republish_action(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_USER_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    draft = get_any_draft(int(callback.data.split(":", 1)[1]))
    if not draft:
        await callback.answer("Запись не найдена", show_alert=True)
        return
    try:
        await publish_draft(draft)
        await callback.answer("Опубликовано снова")
    except Exception:
        logging.exception("Ошибка повторной публикации")
        await callback.answer("Не удалось опубликовать", show_alert=True)


@dp.callback_query(F.data.startswith(("publish:", "reject:", "edit:", "schedule:", "retry:")))
async def draft_action(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id not in ADMIN_USER_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    action, raw_id = callback.data.split(":", 1)
    draft_id = int(raw_id)
    draft = get_draft(draft_id)
    if not draft:
        await callback.answer("Черновик уже обработан или не найден", show_alert=True)
        return

    if action == "edit":
        await state.set_state(SourceSettingsState.waiting_edit)
        await state.update_data(draft_id=draft_id)
        await callback.message.answer("Введите новый текст черновика одним сообщением:")
        await callback.answer()
        return
    if action == "schedule":
        await callback.answer(f"Используйте: /schedule {draft_id} YYYY-MM-DD HH:MM", show_alert=True)
        return
    if action == "retry":
        if not draft["original_text"]:
            await callback.answer("Для этого старого черновика нет исходного текста", show_alert=True)
            return
        try:
            source = get_source(draft["source_id"]) if draft["source_id"] else None
            link = source_permalink(source, draft["source_message_id"]) if source else ""
            adapted = format_generated_post(
                await adapt_text(draft["original_text"], source["prompt"] if source else "", link),
                draft["original_text"],
                link,
            )
            update_draft_text(draft_id, adapted)
            await edit_moderation_message(
                callback.message,
                f"<b>Черновик #{draft_id} обновлён</b>\n\n{adapted}",
                reply_markup=keyboard(draft_id),
            )
            await callback.answer("Готово")
        except Exception:
            logging.exception("Ошибка повторной обработки черновика")
            await callback.answer("Gemini вернул ошибку", show_alert=True)
        return

    if action == "reject":
        set_status(draft_id, "rejected")
        await edit_moderation_message(callback.message, "❌ Черновик отклонён")
        await callback.answer()
        return

    try:
        await publish_draft(draft)
        set_status(draft_id, "published")
        await edit_moderation_message(callback.message, "✅ Опубликовано в канале")
        await callback.answer()
    except Exception:
        logging.exception("Ошибка публикации")
        await callback.answer("Не удалось опубликовать. Проверьте права бота", show_alert=True)


async def scheduler_loop() -> None:
    """Публикует черновики, для которых наступило запланированное время."""
    while True:
        for draft in due_drafts():
            try:
                await publish_draft(draft)
                set_status(draft["id"], "published")
                await bot.send_message(ADMIN_USER_ID, f"✅ Запланированный пост #{draft['id']} опубликован.")
            except Exception:
                logging.exception("Ошибка запланированной публикации #%s", draft["id"])
                await bot.send_message(ADMIN_USER_ID, f"❌ Не удалось опубликовать пост #{draft['id']}.")
        await asyncio.sleep(30)


async def main() -> None:
    init_db()
    session_parent = Path(TELETHON_SESSION).parent
    if str(session_parent) != ".":
        session_parent.mkdir(parents=True, exist_ok=True)
    # Telegram не разрешает одновременно webhook и long polling.
    # Удаляем старый webhook при запуске локального бота.
    await bot.delete_webhook(drop_pending_updates=False)
    await user_client.start()
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Проверить работу бота"),
            BotCommand(command="sources", description="Управление источниками"),
            BotCommand(command="refreshsources", description="Обновить названия каналов"),
            BotCommand(command="addsource", description="Добавить канал-источник"),
            BotCommand(command="last5", description="Обработать 5 последних постов"),
            BotCommand(command="id", description="Узнать свой Telegram ID"),
            BotCommand(command="help", description="Показать список команд"),
            BotCommand(command="stats", description="Статистика публикаций"),
            BotCommand(command="history", description="История публикаций"),
            BotCommand(command="cancel", description="Отменить расписание"),
            BotCommand(command="target", description="Выбрать канал назначения"),
            BotCommand(command="cleanup", description="Очистить старые записи"),
            BotCommand(command="setkeywords", description="Фильтр источника"),
            BotCommand(command="setprompt", description="Правила источника"),
            BotCommand(command="autopublish", description="Автопубликация источника"),
            BotCommand(command="edit", description="Изменить черновик"),
            BotCommand(command="retry", description="Повторить обработку"),
            BotCommand(command="schedule", description="Запланировать публикацию"),
        ]
    )
    logging.info("0x00 SPACE publisher запущен; UserBot читает %s", SOURCE_CHANNEL)
    await asyncio.gather(
        dp.start_polling(bot),
        user_client.run_until_disconnected(),
        scheduler_loop(),
        dashboard_server(),
    )


if __name__ == "__main__":
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_PATH, encoding="utf-8")],
    )
    asyncio.run(main())
