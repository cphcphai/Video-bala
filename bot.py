import asyncio
import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, Forbidden, NetworkError, TelegramError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("telegram_file_receiver_bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required.")

try:
    OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
except ValueError as exc:
    raise RuntimeError("OWNER_ID must be a numeric Telegram user ID.") from exc

if OWNER_ID <= 0:
    raise RuntimeError("OWNER_ID must be a positive Telegram user ID.")

DB_PATH = os.environ.get("DB_PATH", "bot.db")
PORT = int(os.environ.get("PORT", "10000"))
COOLDOWN_HOURS = 12
RESERVATION_TIMEOUT_MINUTES = 30
TELEGRAM_MESSAGE_LIMIT = 4096

db_lock = threading.RLock()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).astimezone(timezone.utc).isoformat()


def parse_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with db_lock, db_connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT NOT NULL,
                authorized INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS uploads (
                upload_id TEXT PRIMARY KEY,
                uploader_id INTEGER NOT NULL,
                uploader_username TEXT,
                uploader_name TEXT NOT NULL,
                upload_type TEXT NOT NULL CHECK(upload_type IN ('thumbnail','video')),
                telegram_file_id TEXT NOT NULL,
                telegram_file_unique_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS deliveries (
                upload_id TEXT NOT NULL,
                receiver_id INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('reserved','sent')),
                reserved_at TEXT NOT NULL,
                delivered_at TEXT,
                PRIMARY KEY(upload_id, receiver_id),
                FOREIGN KEY(upload_id) REFERENCES uploads(upload_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_users_authorized
                ON users(authorized);

            CREATE INDEX IF NOT EXISTS idx_uploads_type_created
                ON uploads(upload_type, created_at);

            CREATE INDEX IF NOT EXISTS idx_uploads_uploader
                ON uploads(uploader_id);

            CREATE INDEX IF NOT EXISTS idx_deliveries_receiver_status
                ON deliveries(receiver_id, status);

            CREATE INDEX IF NOT EXISTS idx_deliveries_receiver_delivered
                ON deliveries(receiver_id, delivered_at);

            CREATE INDEX IF NOT EXISTS idx_deliveries_upload
                ON deliveries(upload_id);
            """
        )
        now = utc_iso()
        conn.execute(
            """
            INSERT INTO users(user_id, username, full_name, authorized, created_at, updated_at)
            VALUES(?, ?, ?, 1, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                authorized=1,
                updated_at=excluded.updated_at
            """,
            (OWNER_ID, None, "Owner", now, now),
        )


def upsert_user(tg_user) -> None:
    if tg_user is None:
        return
    now = utc_iso()
    authorized = 1 if tg_user.id == OWNER_ID else None
    with db_lock, db_connect() as conn:
        if authorized is None:
            conn.execute(
                """
                INSERT INTO users(user_id, username, full_name, authorized, created_at, updated_at)
                VALUES(?, ?, ?, 0, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username,
                    full_name=excluded.full_name,
                    updated_at=excluded.updated_at
                """,
                (tg_user.id, tg_user.username, tg_user.full_name or "", now, now),
            )
        else:
            conn.execute(
                """
                INSERT INTO users(user_id, username, full_name, authorized, created_at, updated_at)
                VALUES(?, ?, ?, 1, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username,
                    full_name=excluded.full_name,
                    authorized=1,
                    updated_at=excluded.updated_at
                """,
                (tg_user.id, tg_user.username, tg_user.full_name or "", now, now),
            )


def is_authorized(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    with db_lock, db_connect() as conn:
        row = conn.execute(
            "SELECT authorized FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return bool(row and row["authorized"])


def set_authorized(user_id: int, authorized: bool) -> None:
    now = utc_iso()
    with db_lock, db_connect() as conn:
        conn.execute(
            """
            INSERT INTO users(user_id, username, full_name, authorized, created_at, updated_at)
            VALUES(?, NULL, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                authorized=excluded.authorized,
                updated_at=excluded.updated_at
            """,
            (user_id, str(user_id), 1 if authorized else 0, now, now),
        )


def release_stale_reservations() -> int:
    cutoff = utc_iso(utc_now() - timedelta(minutes=RESERVATION_TIMEOUT_MINUTES))
    with db_lock, db_connect() as conn:
        cur = conn.execute(
            """
            DELETE FROM deliveries
            WHERE status='reserved' AND reserved_at < ?
            """,
            (cutoff,),
        )
        return cur.rowcount


def reserve_upload(upload_type: str, receiver_id: int):
    release_stale_reservations()
    now = utc_iso()
    with db_lock, db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cooldown_cutoff = utc_iso(utc_now() - timedelta(hours=COOLDOWN_HOURS))
            recent = conn.execute(
                """
                SELECT delivered_at FROM deliveries
                JOIN uploads ON uploads.upload_id = deliveries.upload_id
                WHERE deliveries.receiver_id = ?
                  AND deliveries.status = 'sent'
                  AND uploads.upload_type = ?
                  AND deliveries.delivered_at >= ?
                ORDER BY deliveries.delivered_at DESC
                LIMIT 1
                """,
                (receiver_id, upload_type, cooldown_cutoff),
            ).fetchone()

            if recent:
                conn.execute("ROLLBACK")
                remaining = parse_utc(recent["delivered_at"]) + timedelta(hours=COOLDOWN_HOURS) - utc_now()
                return {"kind": "cooldown", "remaining": max(remaining, timedelta(0))}

            row = conn.execute(
                """
                SELECT u.*
                FROM uploads u
                WHERE u.upload_type = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM deliveries d
                      WHERE d.upload_id = u.upload_id
                        AND d.receiver_id = ?
                        AND d.status IN ('reserved','sent')
                  )
                ORDER BY u.created_at ASC, u.upload_id ASC
                LIMIT 1
                """,
                (upload_type, receiver_id),
            ).fetchone()

            if not row:
                conn.execute("ROLLBACK")
                return {"kind": "empty"}

            try:
                conn.execute(
                    """
                    INSERT INTO deliveries(upload_id, receiver_id, status, reserved_at, delivered_at)
                    VALUES(?, ?, 'reserved', ?, NULL)
                    """,
                    (row["upload_id"], receiver_id, now),
                )
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                return {"kind": "empty"}

            conn.execute("COMMIT")
            return {"kind": "reserved", "upload": dict(row)}
        except Exception:
            conn.execute("ROLLBACK")
            raise


def mark_delivery_sent(upload_id: str, receiver_id: int) -> bool:
    now = utc_iso()
    with db_lock, db_connect() as conn:
        cur = conn.execute(
            """
            UPDATE deliveries
            SET status='sent', delivered_at=?
            WHERE upload_id=? AND receiver_id=? AND status='reserved'
            """,
            (now, upload_id, receiver_id),
        )
        return cur.rowcount == 1


def release_reservation(upload_id: str, receiver_id: int) -> None:
    with db_lock, db_connect() as conn:
        conn.execute(
            """
            DELETE FROM deliveries
            WHERE upload_id=? AND receiver_id=? AND status='reserved'
            """,
            (upload_id, receiver_id),
        )


def record_upload(tg_user, upload_type: str, file_id: str, unique_id: str) -> str:
    upload_id = uuid.uuid4().hex
    with db_lock, db_connect() as conn:
        conn.execute(
            """
            INSERT INTO uploads(
                upload_id, uploader_id, uploader_username, uploader_name,
                upload_type, telegram_file_id, telegram_file_unique_id, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                upload_id,
                tg_user.id,
                tg_user.username,
                tg_user.full_name or "",
                upload_type,
                file_id,
                unique_id,
                utc_iso(),
            ),
        )
    return upload_id


def get_uploads_for_user(user_id: int):
    with db_lock, db_connect() as conn:
        return conn.execute(
            """
            SELECT
                u.*,
                COUNT(CASE WHEN d.status='sent' THEN 1 END) AS receive_count,
                COUNT(CASE WHEN d.status='reserved' THEN 1 END) AS reserved_count
            FROM uploads u
            LEFT JOIN deliveries d ON d.upload_id=u.upload_id
            WHERE u.uploader_id=?
            GROUP BY u.upload_id
            ORDER BY u.created_at DESC
            """,
            (user_id,),
        ).fetchall()


def get_all_uploads():
    with db_lock, db_connect() as conn:
        return conn.execute(
            """
            SELECT
                u.*,
                COUNT(CASE WHEN d.status='sent' THEN 1 END) AS receive_count,
                COUNT(CASE WHEN d.status='reserved' THEN 1 END) AS reserved_count
            FROM uploads u
            LEFT JOIN deliveries d ON d.upload_id=u.upload_id
            GROUP BY u.upload_id
            ORDER BY u.created_at DESC
            """
        ).fetchall()


def format_user(username: str | None, full_name: str) -> str:
    return f"@{username}" if username else (full_name or "Unknown")


def format_dt(value: str) -> str:
    return parse_utc(value).strftime("%Y-%m-%d %H:%M:%S UTC")


def format_remaining(delta: timedelta) -> str:
    total = max(0, int(delta.total_seconds()))
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    current = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            if current:
                chunks.append("".join(current))
                current, current_len = [], 0
            for i in range(0, len(line), limit):
                chunks.append(line[i:i + limit])
            continue
        if current_len + len(line) > limit:
            chunks.append("".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += len(line)
    if current:
        chunks.append("".join(current))
    return chunks or [""]


async def send_split(chat_id: int, text: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    for chunk in split_message(text):
        await context.bot.send_message(chat_id=chat_id, text=chunk)


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📥 Receive", callback_data="menu:receive"),
            InlineKeyboardButton("📤 Upload", callback_data="menu:upload"),
        ]
    ])


def receive_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🖼 Thumbnail", callback_data="receive:thumbnail")],
        [InlineKeyboardButton("🎬 Video", callback_data="receive:video")],
    ])


def upload_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🖼 Thumbnail", callback_data="upload:thumbnail")],
        [InlineKeyboardButton("🎬 Video", callback_data="upload:video")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    upsert_user(user)
    await update.effective_message.reply_text(
        "👋 Welcome!\n\nUse /help to check all command and help.",
        reply_markup=main_menu(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "/start\n/help\n/id\n/checkall\n/checkyourupload\n\n"
        "Owner commands:\n/add USERID\n/remove USERID\n\n"
        "Upload requires Telegram DOCUMENT/FILE.\n"
        "Normal photo/video messages are not accepted.\n"
        "Receive is only available to authorized users.\n"
        "Thumbnail and Video each have their own 12-hour limit."
    )
    await update.effective_message.reply_text(text)


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user:
        await update.effective_message.reply_text(f"🆔 Your Telegram ID: {user.id}")


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != OWNER_ID:
        await update.effective_message.reply_text("❌ Owner only.")
        return
    if len(context.args) != 1 or not context.args[0].isdigit() or int(context.args[0]) <= 0:
        await update.effective_message.reply_text("Usage: /add USERID\nUSERID must be numeric.")
        return
    target = int(context.args[0])
    if target == OWNER_ID:
        await update.effective_message.reply_text(f"✅ User {target} added. They can receive.")
        return
    set_authorized(target, True)
    await update.effective_message.reply_text(f"✅ User {target} added. They can receive.")


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != OWNER_ID:
        await update.effective_message.reply_text("❌ Owner only.")
        return
    if len(context.args) != 1 or not context.args[0].isdigit() or int(context.args[0]) <= 0:
        await update.effective_message.reply_text("Usage: /remove USERID\nUSERID must be numeric.")
        return
    target = int(context.args[0])
    if target == OWNER_ID:
        await update.effective_message.reply_text("❌ The owner cannot be removed.")
        return
    set_authorized(target, False)
    await update.effective_message.reply_text(f"✅ User {target} removed. They can no longer receive.")


async def checkyourupload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    upsert_user(user)
    rows = get_uploads_for_user(user.id)
    if not rows:
        await update.effective_message.reply_text("No uploads yet.")
        return
    lines = ["Your uploads:"]
    for r in rows:
        lines.append(
            f"\nUpload ID: {r['upload_id']}\n"
            f"Type: {r['upload_type'].title()}\n"
            f"Uploader ID: {r['uploader_id']}\n"
            f"Username: {format_user(r['uploader_username'], r['uploader_name'])}\n"
            f"Full name: {r['uploader_name']}\n"
            f"Date/time: {format_dt(r['created_at'])}\n"
            f"Receive count: {r['receive_count']}\n"
            f"Reserved deliveries: {r['reserved_count']}\n"
        )
    await send_split(user.id, "\n".join(lines), context)


async def checkall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    upsert_user(user)
    if not is_authorized(user.id):
        await update.effective_message.reply_text("❌ You don't have access.")
        return
    rows = get_all_uploads()
    if not rows:
        await update.effective_message.reply_text("No uploads yet.")
        return
    lines = ["All uploads:"]
    for r in rows:
        lines.append(
            f"\nUpload ID: {r['upload_id']}\n"
            f"Type: {r['upload_type'].title()}\n"
            f"Uploader ID: {r['uploader_id']}\n"
            f"Username: {format_user(r['uploader_username'], r['uploader_name'])}\n"
            f"Full name: {r['uploader_name']}\n"
            f"Date/time: {format_dt(r['created_at'])}\n"
            f"Receive count: {r['receive_count']}\n"
            f"Reserved deliveries: {r['reserved_count']}\n"
        )
    await send_split(user.id, "\n".join(lines), context)


async def receive_prompt(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(user_id):
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "❌ You don't have access.\n\n"
                f"🆔 Your Telegram ID: {user_id}\n\n"
                "📩 Access लेने के लिए @GVM_TRUST से contact करें."
            ),
        )
        return
    await context.bot.send_message(
        chat_id=chat_id,
        text="Choose what you want to receive:",
        reply_markup=receive_menu(),
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    try:
        await query.answer()
    except TelegramError:
        pass
    if not user:
        return
    upsert_user(user)
    data = query.data or ""
    if data == "menu:receive":
        await receive_prompt(query.message.chat_id, user.id, context)
        return
    if data == "menu:upload":
        await query.message.reply_text(
            "Choose upload type, then send the file as a Telegram Document/File.",
            reply_markup=upload_menu(),
        )
        return
    if data.startswith("upload:"):
        kind = data.split(":", 1)[1]
        if kind not in {"thumbnail", "video"}:
            await query.message.reply_text("❌ Invalid option.")
            return
        context.user_data["pending_upload_type"] = kind
        await query.message.reply_text(
            f"Now send the {kind} as a Telegram Document/File.\n"
            "Normal photo/video messages are not accepted."
        )
        return
    if data.startswith("receive:"):
        kind = data.split(":", 1)[1]
        if kind not in {"thumbnail", "video"}:
            await query.message.reply_text("❌ Invalid option.")
            return
        await receive_one(query.message.chat_id, user.id, kind, context)
        return
    await query.message.reply_text("❌ This button is no longer valid.")


async def receive_one(chat_id: int, receiver_id: int, upload_type: str, context) -> None:
    if not is_authorized(receiver_id):
        await receive_prompt(chat_id, receiver_id, context)
        return

    try:
        result = await asyncio.to_thread(reserve_upload, upload_type, receiver_id)
    except sqlite3.Error:
        logger.exception("Database error while reserving upload")
        await context.bot.send_message(chat_id=chat_id, text="❌ Temporary database error. Please try again.")
        return

    if result["kind"] == "cooldown":
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏳ {upload_type.title()} cooldown is active. Try again in {format_remaining(result['remaining'])}.",
        )
        return
    if result["kind"] == "empty":
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"ℹ️ No available {upload_type} file to receive right now.",
        )
        return

    upload = result["upload"]
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
        await context.bot.send_document(
            chat_id=chat_id,
            document=upload["telegram_file_id"],
            caption=f"Upload ID: {upload['upload_id']}",
        )
    except (Forbidden, BadRequest, NetworkError, TimedOut, TelegramError) as exc:
        logger.warning("Telegram delivery failed for %s: %s", upload["upload_id"], exc)
        await asyncio.to_thread(release_reservation, upload["upload_id"], receiver_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ Delivery failed. The reservation was released; please try again later.",
        )
        return
    except Exception:
        logger.exception("Unexpected delivery error")
        await asyncio.to_thread(release_reservation, upload["upload_id"], receiver_id)
        await context.bot.send_message(chat_id=chat_id, text="❌ Delivery failed safely. Please try again.")
        return

    try:
        marked = await asyncio.to_thread(mark_delivery_sent, upload["upload_id"], receiver_id)
        if not marked:
            logger.error("Delivery sent but reservation could not be marked sent: %s", upload["upload_id"])
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ File was sent, but the delivery record needs recovery. Please contact the owner.",
            )
            return
    except sqlite3.Error:
        logger.exception("Database error after successful Telegram delivery")
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ File was sent, but the delivery record could not be finalized.",
        )
        return

    receiver_name = format_user(user_username(receiver_id), user_full_name(receiver_id))
    notify = (
        "📦 Delivery\n\n"
        f"Uploader ID: {upload['uploader_id']}\n"
        f"Uploader: {format_user(upload['uploader_username'], upload['uploader_name'])}\n"
        f"Uploader name: {upload['uploader_name']}\n\n"
        f"Receiver ID: {receiver_id}\n"
        f"Receiver: {receiver_name}\n\n"
        f"Upload ID: {upload['upload_id']}\n"
        f"Type: {upload_type.title()}\n"
        "Status: Delivered"
    )
    try:
        await context.bot.send_message(chat_id=OWNER_ID, text=notify)
    except TelegramError:
        logger.warning("Could not notify owner about delivery %s", upload["upload_id"])


def user_username(user_id: int) -> str | None:
    with db_lock, db_connect() as conn:
        row = conn.execute("SELECT username FROM users WHERE user_id=?", (user_id,)).fetchone()
        return row["username"] if row else None


def user_full_name(user_id: int) -> str:
    with db_lock, db_connect() as conn:
        row = conn.execute("SELECT full_name FROM users WHERE user_id=?", (user_id,)).fetchone()
        return row["full_name"] if row else "Unknown"


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    document = update.effective_message.document if update.effective_message else None
    if not user or not document:
        return
    upsert_user(user)
    kind = context.user_data.get("pending_upload_type")
    if kind not in {"thumbnail", "video"}:
        await update.effective_message.reply_text(
            "Please press [📤 Upload], choose Thumbnail or Video, then send the file as a Telegram Document/File."
        )
        return

    mime = (document.mime_type or "").lower()
    filename = (document.file_name or "").lower()

    image_ok = mime.startswith("image/") or filename.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))
    video_ok = mime.startswith("video/") or filename.endswith((".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"))

    if kind == "thumbnail" and not image_ok:
        await update.effective_message.reply_text(
            "❌ Thumbnail uploads must be an appropriate image sent as a Telegram Document/File."
        )
        return
    if kind == "video" and not video_ok:
        await update.effective_message.reply_text(
            "❌ Video uploads must be an appropriate video sent as a Telegram Document/File."
        )
        return

    try:
        upload_id = await asyncio.to_thread(
            record_upload,
            user,
            kind,
            document.file_id,
            document.file_unique_id,
        )
    except sqlite3.Error:
        logger.exception("Could not record upload")
        await update.effective_message.reply_text("❌ Could not save the upload. Please try again.")
        return

    context.user_data.pop("pending_upload_type", None)
    await update.effective_message.reply_text(
        f"✅ Upload saved.\n\nUpload ID: {upload_id}\nType: {kind.title()}"
    )


async def reject_photo_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            "❌ Please send the file as a Telegram Document/File. Normal photo/video messages are not accepted."
        )


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return


def start_health_server():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="health-server", daemon=True)
    thread.start()
    logger.info("Health server listening on 0.0.0.0:%s", PORT)
    return server


async def post_init(application: Application) -> None:
    release_stale_reservations()


def build_application() -> Application:
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("id", id_command))
    application.add_handler(CommandHandler("add", add_command))
    application.add_handler(CommandHandler("remove", remove_command))
    application.add_handler(CommandHandler("checkyourupload", checkyourupload))
    application.add_handler(CommandHandler("checkall", checkall))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO, reject_photo_video))
    return application


def main() -> None:
    init_db()
    health_server = start_health_server()
    application = build_application()
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)
    finally:
        health_server.shutdown()
        health_server.server_close()


if __name__ == "__main__":
    main()
