import os
import sys
import shutil
import logging
import asyncio
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

import httpx
from telegram import (
    Update,
    LinkPreviewOptions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeAllPrivateChats,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from dotenv import load_dotenv
import yt_dlp
from yt_dlp.version import __version__ as YTDLP_VERSION

load_dotenv()

# مسیر bin محیط ویرچوال را به PATH اضافه می‌کنیم تا yt-dlp بتواند `deno` را پیدا کند
os.environ["PATH"] = (
    os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", "")
)

BOT_TOKEN = os.getenv("BOT_TOKEN")

# تنظیمات عضویت اجباری از طریق ربات ادمین مرکزی
ADMIN_API_URL = os.getenv("ADMIN_API_URL", "https://admin-dl-bot.onrender.com").rstrip("/")
API_KEY = os.getenv("API_KEY", "")

# مسیر فایل کوکی (روی Render مقدارش مسیر Secret File است:
# /etc/secrets/cookies.txt  — نه محتوای کوکی!)
COOKIES_PATH = os.getenv("COOKIES_PATH", "cookies.txt")

# برای ارسال ویدئوهای بزرگ‌تر از ۵۰ مگابایت (اختیاری):
# API_ID و API_HASH را از my.telegram.org بگیر و توی Environment بگذار.
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not found")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# جلوگیری از چاپ توکن ربات توی لاگ (httpx آدرس کامل درخواست را لاگ می‌کند)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

logger.info("yt-dlp version: %s", YTDLP_VERSION)
logger.info(
    "Cookie file '%s' exists: %s",
    COOKIES_PATH,
    os.path.exists(COOKIES_PATH),
)
logger.info("deno path: %s", shutil.which("deno"))

# سقف ارسال با Bot API معمولی
MAX_FILE_SIZE = 49 * 1024 * 1024

# سقف ویدئوهای بزرگ (پیش‌فرض ۵۰۰ مگابایت؛ سرور رایگان رم/دیسک/زمان محدود دارد)
LARGE_MAX_MB = int(os.getenv("LARGE_MAX_MB", "500"))
LARGE_MAX_SIZE = LARGE_MAX_MB * 1024 * 1024

# کلاینت Telethon (برای ارسال فایل بزرگ) داخل post_init ساخته می‌شود
tclient = None

# ⚠️ متن دکمه استارت — همه‌جا از همین استفاده می‌شود
START_BUTTON_TEXT = "🚀 استارت"


# ---------------- Cookie Helper ----------------

def prepare_cookie_file(src: str, dst: Path) -> int:
    """فایل کوکی را به فرمت درست Netscape (با Tab) تبدیل می‌کند."""
    lines = ["# Netscape HTTP Cookie File"]
    with open(src, encoding="utf-8", errors="ignore") as f:
        for raw in f:
            line = raw.replace("\ufeff", "").strip()
            if not line:
                continue
            if line.startswith("#") and not line.startswith("#HttpOnly_"):
                continue
            parts = line.split(None, 6)
            if len(parts) < 7:
                continue
            lines.append("\t".join(parts))
    dst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines) - 1


def format_for_limit(limit: int) -> str:
    """فقط فرمت‌هایی که هم تصویر و هم صدا دارند و زیر سقف حجم‌اند."""
    return (
        f"best[ext=mp4][filesize<{limit}]/"
        f"best[ext=mp4][filesize_approx<{limit}]/"
        f"best[filesize<{limit}]/"
        f"best[filesize_approx<{limit}]"
    )


def is_format_unavailable(err: Exception) -> bool:
    return "Requested format is not available" in str(err)


# ---------------- Health Server ----------------

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"YouTube bot is alive")

    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info("Health server started on port %s", port)
    server.serve_forever()


# ---------------- Bot Commands (منوی آبی کنار کیبورد) ----------------

async def setup_bot_commands(app: Application):
    commands = [
        BotCommand("start", START_BUTTON_TEXT),
    ]

    await app.bot.set_my_commands(
        commands,
        scope=BotCommandScopeDefault(),
    )

    await app.bot.set_my_commands(
        commands,
        scope=BotCommandScopeAllPrivateChats(),
    )

    logger.info("Bot commands configured.")


# ---------------- Reply Keyboard (کیبورد پایین) ----------------

def get_main_reply_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(START_BUTTON_TEXT)],
    ]
    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="لینک یوتیوب را بفرست یا استارت بزن...",
    )


# ---------------- URL Validation ----------------

def is_youtube_url(url: str) -> bool:
    try:
        host = (urlparse(url.strip()).hostname or "").lower()

        return host in {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "music.youtube.com",
            "youtu.be",
            "www.youtu.be",
        }
    except Exception:
        return False


# ---------------- Large upload (Telethon) ----------------

async def post_init(application: Application):
    """هم Telethon را راه می‌اندازد، هم منوی آبی را تنظیم می‌کند."""
    global tclient

    # ⚠️ اول منوی آبی را تنظیم می‌کنیم تا از همان ابتدا در دسترس باشد
    try:
        await setup_bot_commands(application)
    except Exception:
        logger.exception("Failed to set bot commands")

    if not (API_ID and API_HASH):
        logger.info("API_ID/API_HASH not set: large uploads disabled")
        return

    try:
        from telethon import TelegramClient, events
        from telethon.sessions import StringSession

        client = TelegramClient(StringSession(), int(API_ID), API_HASH)
        await client.start(bot_token=BOT_TOKEN)

        @client.on(events.NewMessage(incoming=True))
        async def _cache_entities(event):
            pass

        tclient = client
        logger.info("Telethon ready: large uploads enabled (up to %s MB)", LARGE_MAX_MB)
    except Exception:
        tclient = None
        logger.exception("Telethon failed to start: large uploads disabled")


async def send_large_video(chat_id: int, path: Path, info: dict, caption: str):
    """ارسال ویدئوی بزرگ با Telethon. در صورت خطا exception می‌دهد."""
    from telethon.tl.types import DocumentAttributeVideo

    entity = None
    for _ in range(5):
        try:
            entity = await tclient.get_input_entity(chat_id)
            break
        except ValueError:
            await asyncio.sleep(2)

    if entity is None:
        raise RuntimeError("Telethon could not resolve this chat")

    attrs = [
        DocumentAttributeVideo(
            duration=int(info.get("duration") or 0),
            w=int(info.get("width") or 0),
            h=int(info.get("height") or 0),
            supports_streaming=True,
        )
    ]

    await tclient.send_file(
        entity,
        str(path),
        caption=caption,
        attributes=attrs,
        supports_streaming=True,
        force_document=False,
    )


# ---------------- Mandatory Join (Central Admin API) ----------------

async def check_membership(user_id: int):
    """خروجی: (اجازه دسترسی, کانال‌های باقی‌مانده برای عضویت)."""
    if not API_KEY:
        logger.error("API_KEY is not configured; mandatory join check is blocked")
        return False, []

    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            response = await client.get(
                f"{ADMIN_API_URL}/api/check",
                params={"user_id": user_id},
                headers={"X-API-Key": API_KEY},
            )
            response.raise_for_status()
            data = response.json()

        if data.get("enabled") is False:
            return True, []

        missing = data.get("missing") or []
        allowed = bool(data.get("ok")) and not missing
        return allowed, missing
    except Exception:
        logger.exception("Central membership API request failed")
        return False, []


def _channel_value(channel, *keys):
    if isinstance(channel, dict):
        for key in keys:
            value = channel.get(key)
            if value:
                return str(value)
        return ""
    return str(channel) if channel else ""


async def membership_markup(missing):
    """دکمه‌های عضویت را از اطلاعات کانال‌های برگشتی API می‌سازد."""
    rows = []
    channels = missing if all(isinstance(item, dict) for item in missing) else []
    if missing and not channels:
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
                response = await client.get(
                    f"{ADMIN_API_URL}/api/channels",
                    headers={"X-API-Key": API_KEY},
                )
                response.raise_for_status()
                payload = response.json()
            all_channels = payload.get("channels", payload) if isinstance(payload, dict) else payload
            if isinstance(all_channels, list):
                wanted = {str(item).lstrip("@").lower() for item in missing}
                for channel in all_channels:
                    if not isinstance(channel, dict):
                        continue
                    identifiers = {
                        _channel_value(channel, "id", "channel_id", "username", "channel", "chat_id").lstrip("@").lower(),
                        _channel_value(channel, "username", "channel", "chat_id").lstrip("@").lower(),
                    }
                    if wanted.intersection(identifiers):
                        channels.append(channel)
        except Exception:
            logger.exception("Could not load channel buttons from central API")

    for channel in channels:
        url = _channel_value(channel, "join_url", "url", "invite_link", "channel_url", "link")
        name = _channel_value(channel, "title", "name", "username", "channel") or "عضویت در کانال"
        if url.startswith("http://") or url.startswith("https://"):
            rows.append([InlineKeyboardButton(f"عضویت در {name}", url=url)])

    rows.append([InlineKeyboardButton("✅ عضو شدم؛ بررسی دوباره", callback_data="join:check")])
    return InlineKeyboardMarkup(rows)


async def require_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return False

    allowed, missing = await check_membership(user.id)
    if allowed:
        return True

    await message.reply_text(
        "🔒 برای استفاده از ربات، ابتدا در کانال‌های زیر عضو شو؛ سپس روی «عضو شدم» بزن.",
        reply_markup=await membership_markup(missing),
    )
    return False


async def on_join_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    allowed, missing = await check_membership(query.from_user.id)
    if allowed:
        await query.edit_message_text(
            "✅ عضویتت تأیید شد. حالا لینک ویدئوی یوتیوب را برای ربات بفرست."
        )
    else:
        await query.edit_message_text(
            "❌ هنوز عضویتت تأیید نشده. در کانال‌های لازم عضو شو و دوباره بررسی کن.",
            reply_markup=await membership_markup(missing),
        )


# ---------------- Bot Commands ----------------

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await require_membership(update, context):
        return
    # ⚠️ کیبورد پایین همیشه همراه پیام استارت ارسال می‌شود
    await update.effective_message.reply_text(
        "سلام 👋\n\n"
        "لینک ویدئوی یوتیوب را بفرست.\n"
        "ویدئوهای متعلق به خودت یا دارای مجوز دانلود پشتیبانی می‌شوند. 🎬",
        reply_markup=get_main_reply_keyboard(),
    )


# ---------------- هندلر دکمه استارت از کیبورد پایین ----------------

async def handle_reply_keyboard(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    if not message or not message.text:
        return

    if message.text.strip() == START_BUTTON_TEXT:
        await start(update, context)


async def handle_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if not await require_membership(update, context):
        return

    if not message or not message.text:
        return

    url = message.text.strip()

    if not is_youtube_url(url):
        await message.reply_text(
            "❌ لطفاً یک لینک معتبر یوتیوب بفرست."
        )
        return

    status = await message.reply_text(
        "⏳ در حال بررسی و آماده‌سازی ویدئو..."
    )

    try:
        with TemporaryDirectory() as tmpdir:
            output_template = str(
                Path(tmpdir) / "video.%(ext)s"
            )

            ydl_opts = {
                "outtmpl": output_template,
                "quiet": False,
                "no_warnings": False,
                "noplaylist": True,
                "socket_timeout": 30,
                "remote_components": ["ejs:github"],
            }

            if os.path.exists(COOKIES_PATH):
                cookie_copy = Path(tmpdir) / "cookies.txt"
                count = prepare_cookie_file(COOKIES_PATH, cookie_copy)
                logger.info("Valid cookie lines found: %s", count)
                if count > 0:
                    ydl_opts["cookiefile"] = str(cookie_copy)
                else:
                    logger.warning(
                        "Cookie file has no valid cookie lines; running without cookies"
                    )
            else:
                logger.warning("Cookie file not found: %s", COOKIES_PATH)

            def download(limit: int):
                attempts = [
                    ("default", None, True),
                    ("tv", {"youtube": {"player_client": ["tv"]}}, False),
                    ("mweb", {"youtube": {"player_client": ["mweb"]}}, False),
                ]
                last_err = None
                for name, args, use_cookies in attempts:
                    opts = dict(ydl_opts)
                    opts["format"] = format_for_limit(limit)
                    if args:
                        opts["extractor_args"] = args
                    if not use_cookies:
                        opts.pop("cookiefile", None)
                    try:
                        logger.info("Trying client: %s (limit %s MB)", name, limit // 1024 // 1024)
                        with yt_dlp.YoutubeDL(opts) as ydl:
                            info = ydl.extract_info(url, download=True)
                        logger.info("Client '%s' worked", name)
                        return info
                    except Exception as e:
                        last_err = e
                        logger.warning(
                            "Client '%s' failed: %s", name, str(e)[:200]
                        )
                        if is_format_unavailable(e):
                            raise
                raise last_err

            def get_direct_link():
                opts = dict(ydl_opts)
                opts["format"] = "best[ext=mp4]/best"
                opts["skip_download"] = True
                with yt_dlp.YoutubeDL(opts) as ydl:
                    return ydl.extract_info(url, download=False)

            info = None

            try:
                info = await asyncio.to_thread(download, MAX_FILE_SIZE)
            except Exception as e:
                if not is_format_unavailable(e):
                    raise

            if info is None and tclient is not None:
                await status.edit_text(
                    "⏳ ویدئو بزرگ‌تر از ۵۰ مگابایت است؛ در حال دانلود نسخه‌ی کامل "
                    "(ممکن است چند دقیقه طول بکشد)..."
                )
                try:
                    info = await asyncio.to_thread(download, LARGE_MAX_SIZE)
                except Exception as e:
                    if not is_format_unavailable(e):
                        raise

            if info is None:
                await status.edit_text(
                    "⏳ ویدئو از سقف مجاز بزرگ‌تر است؛ در حال ساخت لینک مستقیم..."
                )
                link_info = await asyncio.to_thread(get_direct_link)
                direct = link_info.get("url")
                size = link_info.get("filesize") or link_info.get("filesize_approx")
                size_txt = f"{size / 1024 / 1024:.0f} MB" if size else "نامشخص"
                title = (link_info.get("title") or "")[:100]

                if not direct or len(direct) > 3500:
                    await status.edit_text(
                        "❌ این ویدئو خیلی بزرگ است و لینک مستقیم هم ساخته نشد."
                    )
                    return

                await status.edit_text(
                    f"⚠️ حجم ویدئو زیاد است ({size_txt}).\n"
                    f"{title}\n\n"
                    "لینک مستقیم دانلود (موقتی است و ممکن است فقط چند ساعت کار کند):\n"
                    f"{direct}",
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
                return

            video_exts = {".mp4", ".mkv", ".webm", ".mov"}
            files = [
                p for p in Path(tmpdir).iterdir()
                if p.is_file()
                and p.suffix.lower() in video_exts
                and p.stat().st_size > 0
            ]

            if not files:
                raise FileNotFoundError(
                    "Downloaded video was not found"
                )

            filename = max(
                files,
                key=lambda p: p.stat().st_size
            )
            file_size = filename.stat().st_size

            await status.edit_text(
                "📤 ویدئو آماده است؛ در حال ارسال..."
            )

            if file_size <= MAX_FILE_SIZE:
                with filename.open("rb") as video:
                    await context.bot.send_video(
                        chat_id=message.chat_id,
                        video=video,
                        supports_streaming=True,
                        duration=int(info.get("duration") or 0) or None,
                        width=info.get("width"),
                        height=info.get("height"),
                        caption="🎬 ویدئوی شما آماده است.",
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=30,
                        pool_timeout=30,
                    )
            else:
                await send_large_video(
                    message.chat_id,
                    filename,
                    info,
                    "🎬 ویدئوی شما آماده است.",
                )

            await status.delete()

    except Exception as e:
        logger.exception("YouTube download failed")

        await status.edit_text(
            f"❌ خطا:\n{str(e)[:700]}"
        )


# ---------------- Main ----------------

def main():
    threading.Thread(
        target=start_health_server,
        daemon=True,
    ).start()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_join_check, pattern=r"^join:check$"))

    # ⚠️ هندلر دکمه استارت از کیبورد پایین — دقیقاً قبل از handle_url
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Regex(
                rf"^{re.escape(START_BUTTON_TEXT)}$"
            ),
            handle_reply_keyboard,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_url,
        )
    )

    logger.info("YouTube bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
