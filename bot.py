import asyncio
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Iterable, List, Optional

from telegram import ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)


DB_PATH = Path("bot.db")
ASK_COURSE, ASK_SECTION, ASK_URL, ASK_UPDATE_DATE, ASK_BROADCAST, ASK_SEARCH_DATE = range(6)
BUTTON_LATEST = "Latest"
BUTTON_LAST_7_DAYS = "Last 7 Days"
BUTTON_SEARCH_BY_DATE = "Search by Date"
BUTTON_CANCEL = "Cancel"


@dataclass
class Entry:
    id: int
    url: str
    module: str
    tag: str
    message: str
    created_at: str
    entry_date: str


def load_env(env_path: str = ".env") -> None:
    path = Path(env_path)
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS updates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                module TEXT NOT NULL,
                tag TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                entry_date TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscribers (
                user_id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                first_name TEXT NOT NULL DEFAULT '',
                last_seen TEXT NOT NULL
            )
            """
        )
        conn.commit()


def save_update(
    course: str,
    module_name: str,
    url: str,
    message: str,
    entry_date: Optional[str] = None,
) -> None:
    now = datetime.now()
    saved_date = entry_date or now.date().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO updates (url, module, tag, message, created_at, entry_date)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                url.strip(),
                course.strip(),
                module_name.strip(),
                message.strip(),
                now.isoformat(timespec="seconds"),
                saved_date,
            ),
        )
        conn.commit()


def format_display_date(entry_date: str) -> str:
    try:
        parsed = datetime.strptime(entry_date, "%Y-%m-%d")
    except ValueError:
        return escape(entry_date)
    return parsed.strftime("%d-%b").lower()


def row_to_entry(row: sqlite3.Row) -> Entry:
    return Entry(
        id=row["id"],
        url=row["url"],
        module=row["module"],
        tag=row["tag"],
        message=row["message"],
        created_at=row["created_at"],
        entry_date=row["entry_date"],
    )


def remember_subscriber(update: Update) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO subscribers (user_id, chat_id, username, first_name, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id = excluded.chat_id,
                username = excluded.username,
                first_name = excluded.first_name,
                last_seen = excluded.last_seen
            """,
            (
                user.id,
                chat.id,
                user.username or "",
                user.first_name or "",
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()


def fetch_entries(query: str, params: Iterable[object] = ()) -> List[Entry]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, tuple(params)).fetchall()
    return [row_to_entry(row) for row in rows]


def fetch_latest(limit: int = 5) -> List[Entry]:
    return fetch_entries(
        """
        SELECT * FROM updates
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    )


def fetch_entry_by_id(entry_id: int) -> Optional[Entry]:
    entries = fetch_entries(
        """
        SELECT * FROM updates
        WHERE id = ?
        LIMIT 1
        """,
        (entry_id,),
    )
    return entries[0] if entries else None


def fetch_by_days(days: int, limit: int = 20) -> List[Entry]:
    since = (date.today() - timedelta(days=days)).isoformat()
    return fetch_entries(
        """
        SELECT * FROM updates
        WHERE entry_date >= ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (since, limit),
    )


def fetch_by_date(target_date: str, limit: int = 20) -> List[Entry]:
    return fetch_entries(
        """
        SELECT * FROM updates
        WHERE entry_date = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (target_date, limit),
    )


def fetch_by_tag(tag: str, limit: int = 20) -> List[Entry]:
    return fetch_entries(
        """
        SELECT * FROM updates
        WHERE tag = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (tag.strip().lower(), limit),
    )


def fetch_by_module(module: str, limit: int = 20) -> List[Entry]:
    return fetch_entries(
        """
        SELECT * FROM updates
        WHERE module = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (module.strip().lower(), limit),
    )


def fetch_distinct(column: str) -> List[str]:
    if column not in {"tag", "module"}:
        raise ValueError("Unsupported column")

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            f"""
            SELECT DISTINCT {column}
            FROM updates
            WHERE {column} != ''
            ORDER BY {column} ASC
            """
        ).fetchall()
    return [row[0] for row in rows]


def delete_update(entry_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            DELETE FROM updates
            WHERE id = ?
            """,
            (entry_id,),
        )
        conn.commit()
    return cursor.rowcount > 0


def fetch_subscriber_chat_ids() -> List[int]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT chat_id
            FROM subscribers
            ORDER BY chat_id ASC
            """
        ).fetchall()
    return [row[0] for row in rows]


def is_admin(user_id: Optional[int]) -> bool:
    if user_id is None:
        return False
    raw = os.getenv("ADMIN_USER_IDS", "")
    allowed = {part.strip() for part in raw.split(",") if part.strip()}
    return str(user_id) in allowed


def format_entries(entries: List[Entry], title: str) -> str:
    if not entries:
        return f"{title}\n\nNo course updates were found."

    lines = [f"<b>{escape(title)}</b>"]
    for entry in entries:
        url_part = (
            f'<a href="{escape(entry.url, quote=True)}">here</a>'
            if entry.url
            else "in the course portal"
        )
        lines.append(
            (
                "\n"
                f"<code>{entry.id}</code>. On {format_display_date(entry.entry_date)}, "
                f"the course <b>{escape(entry.module)}</b> and module <b>{escape(entry.tag)}</b> "
                f"were updated. "
                f"You may review it {url_part}."
            )
        )
    return "\n".join(lines)


def user_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [BUTTON_LATEST, BUTTON_LAST_7_DAYS],
            [BUTTON_SEARCH_BY_DATE],
        ],
        resize_keyboard=True,
    )


def date_search_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [BUTTON_CANCEL],
        ],
        resize_keyboard=True,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remember_subscriber(update)
    user = update.effective_user
    admin_note = (
        "\nAdmin commands: /update, /delete, /broadcast."
        if is_admin(user.id if user else None)
        else ""
    )
    await update.message.reply_text(
        "Please choose an option below to view course updates."
        f"{admin_note}",
        reply_markup=user_keyboard(),
    )


async def update_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    remember_subscriber(update)
    user = update.effective_user
    if not is_admin(user.id if user else None):
        await update.message.reply_text("Only admins may use /update.")
        return ConversationHandler.END

    context.user_data["draft_update"] = {}
    await update.message.reply_text("Please send the course name.")
    return ASK_COURSE


async def update_course(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    remember_subscriber(update)
    context.user_data["draft_update"]["course"] = update.message.text.strip()
    await update.message.reply_text("Please send the module name.")
    return ASK_SECTION


async def update_section(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    remember_subscriber(update)
    context.user_data["draft_update"]["module_name"] = update.message.text.strip()
    await update.message.reply_text("Please send the update URL.")
    return ASK_URL


async def update_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    remember_subscriber(update)
    context.user_data["draft_update"]["url"] = update.message.text.strip()
    await update.message.reply_text(
        "Please send the date in YYYY-MM-DD format, or type skip to use today's date."
    )
    return ASK_UPDATE_DATE


async def update_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    remember_subscriber(update)
    draft = context.user_data.get("draft_update", {})
    raw_date = update.message.text.strip()
    entry_date = None
    if raw_date.lower() != "skip":
        try:
            datetime.strptime(raw_date, "%Y-%m-%d")
        except ValueError:
            await update.message.reply_text(
                "Invalid date format. Please send the date as YYYY-MM-DD, or type skip."
            )
            return ASK_UPDATE_DATE
        entry_date = raw_date

    save_update(
        course=draft["course"],
        module_name=draft["module_name"],
        url=draft["url"],
        message="",
        entry_date=entry_date,
    )
    context.user_data.pop("draft_update", None)
    await update.message.reply_text("The course update has been saved successfully.")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    remember_subscriber(update)
    context.user_data.pop("draft_update", None)
    context.user_data.pop("awaiting_search_date", None)
    await update.message.reply_text("The current action has been cancelled.", reply_markup=user_keyboard())
    return ConversationHandler.END


async def delete_saved_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remember_subscriber(update)
    user = update.effective_user
    if not is_admin(user.id if user else None):
        await update.message.reply_text("Only admins may use /delete.")
        return

    if not context.args:
        entries = fetch_latest(limit=10)
        await update.message.reply_text(
            format_entries(entries, "Latest Course Updates"),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        await update.message.reply_text("Usage: /delete ID")
        return

    raw_id = context.args[0]
    if not raw_id.isdigit():
        await update.message.reply_text("Usage: /delete ID")
        return

    entry_id = int(raw_id)
    entry = fetch_entry_by_id(entry_id)
    if entry is None:
        await update.message.reply_text(f"No update was found with ID {entry_id}.")
        return

    delete_update(entry_id)
    await update.message.reply_text(f"Update ID {entry_id} has been deleted.")


async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    remember_subscriber(update)
    user = update.effective_user
    if not is_admin(user.id if user else None):
        await update.message.reply_text("Only admins may use /broadcast.")
        return ConversationHandler.END

    context.user_data["broadcast_mode"] = True
    await update.message.reply_text("Please send the message you would like to broadcast to all users.")
    return ASK_BROADCAST


async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    remember_subscriber(update)
    user = update.effective_user
    if not is_admin(user.id if user else None):
        await update.message.reply_text("Only admins may use /broadcast.")
        return ConversationHandler.END

    message = update.message.text.strip()
    chat_ids = fetch_subscriber_chat_ids()
    sent = 0
    failed = 0

    for chat_id in chat_ids:
        try:
            await context.bot.send_message(chat_id=chat_id, text=message)
            sent += 1
        except Forbidden:
            failed += 1
        except TelegramError:
            failed += 1

    context.user_data.pop("broadcast_mode", None)
    await update.message.reply_text(
        f"Broadcast completed. Delivered to {sent} chat(s) and failed for {failed}."
    )
    return ConversationHandler.END


async def latest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remember_subscriber(update)
    entries = fetch_latest()
    await update.message.reply_text(
        format_entries(entries, "Latest Course Updates"),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=user_keyboard(),
    )


async def days_7(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remember_subscriber(update)
    entries = fetch_by_days(7)
    await update.message.reply_text(
        format_entries(entries, "Course Updates From Last 7 Days"),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=user_keyboard(),
    )

async def by_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remember_subscriber(update)
    if not context.args:
        await update.message.reply_text("Usage: /date YYYY-MM-DD")
        return

    raw_date = context.args[0]
    try:
        datetime.strptime(raw_date, "%Y-%m-%d")
    except ValueError:
        await update.message.reply_text("Invalid date format. Please use YYYY-MM-DD.")
        return

    entries = fetch_by_date(raw_date)
    await update.message.reply_text(
        format_entries(entries, f"Course Updates for {raw_date}"),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=user_keyboard(),
    )


async def search_by_date_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    remember_subscriber(update)
    context.user_data["awaiting_search_date"] = True
    await update.message.reply_text(
        "Please send a date in YYYY-MM-DD format.",
        reply_markup=date_search_keyboard(),
    )
    return ASK_SEARCH_DATE


async def search_by_date_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    remember_subscriber(update)
    raw_date = update.message.text.strip()
    try:
        datetime.strptime(raw_date, "%Y-%m-%d")
    except ValueError:
        await update.message.reply_text(
            "Invalid date format. Please send it as YYYY-MM-DD or tap Cancel.",
            reply_markup=date_search_keyboard(),
        )
        return ASK_SEARCH_DATE

    context.user_data.pop("awaiting_search_date", None)
    entries = fetch_by_date(raw_date)
    await update.message.reply_text(
        format_entries(entries, f"Course Updates for {raw_date}"),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=user_keyboard(),
    )
    return ConversationHandler.END


def main() -> None:
    load_env()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is missing. Add it to .env")

    init_db()

    application = Application.builder().token(token).build()

    update_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("update", update_start),
            CommandHandler("broadcast", broadcast_start),
            MessageHandler(filters.Regex(f"^{BUTTON_SEARCH_BY_DATE}$"), search_by_date_start),
        ],
        states={
            ASK_COURSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, update_course)],
            ASK_SECTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, update_section)],
            ASK_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, update_url)],
            ASK_UPDATE_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, update_date)],
            ASK_BROADCAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_message)],
            ASK_SEARCH_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_by_date_message)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex(f"^{BUTTON_CANCEL}$"), cancel),
        ],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(update_conversation)
    application.add_handler(CommandHandler("latest", latest))
    application.add_handler(MessageHandler(filters.Regex(f"^{BUTTON_LATEST}$"), latest))
    application.add_handler(MessageHandler(filters.Regex(r"^/7days(?:@\w+)?$"), days_7))
    application.add_handler(MessageHandler(filters.Regex(f"^{BUTTON_LAST_7_DAYS}$"), days_7))
    application.add_handler(CommandHandler("date", by_date))
    application.add_handler(CommandHandler("delete", delete_saved_update))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(MessageHandler(filters.Regex(f"^{BUTTON_CANCEL}$"), cancel))

    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.ERROR,
    )

    # Python 3.14 no longer creates a default event loop automatically.
    asyncio.set_event_loop(asyncio.new_event_loop())
    application.run_polling()


if __name__ == "__main__":
    main()
