import os
import shutil
import logging
import asyncio
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from dotenv import load_dotenv
import yt_dlp
from yt_dlp.version import __version__ as YTDLP_VERSION

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

# مسیر فایل کوکی (روی Render مقدارش مسیر Secret File است:
# /etc/secrets/cookies.txt  — نه محتوای کوکی!)
COOKIES_PATH = os.getenv("COOKIES_PATH", "cookies.txt")

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

# نسخه‌ی yt-dlp توی لاگ چاپ می‌شه تا مطمئن بشی آپدیت واقعاً نصب شده
logger.info("yt-dlp version: %s", YTDLP_VERSION)
logger.info(
    "Cookie file '%s' exists: %s",
    COOKIES_PATH,
    os.path.exists(COOKIES_PATH),
)

MAX_FILE_SIZE = 49 * 1024 * 1024


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


# ---------------- Bot Commands ----------------

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.effective_message.reply_text(
        "سلام 👋\n\n"
        "لینک ویدئوی یوتیوب را بفرست.\n"
        "ویدئوهای متعلق به خودت یا دارای مجوز دانلود پشتیبانی می‌شوند. 🎬"
    )


async def handle_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

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
                # لاگ کامل‌تر برای پیدا کردن دلیل خطا
                "quiet": False,
                "no_warnings": False,
                "noplaylist": True,
                "socket_timeout": 30,
                "format": "best[ext=mp4]/best",
                "max_filesize": MAX_FILE_SIZE,
                # معادل --remote-components ejs:github در خط فرمان
                "remote_components": ["ejs:github"],
            }

            # کپی کوکی داخل پوشه‌ی موقت، چون yt-dlp می‌خواد
            # بعد از اجرا دوباره توی فایل کوکی بنویسه و ممکنه
            # فایل اصلی (مثلاً Secret File) فقط‌خواندنی باشه.
            if os.path.exists(COOKIES_PATH):
                cookie_copy = Path(tmpdir) / "cookies.txt"
                shutil.copy(COOKIES_PATH, cookie_copy)
                ydl_opts["cookiefile"] = str(cookie_copy)
            else:
                logger.warning("Cookie file not found: %s", COOKIES_PATH)

            # Download runs outside the async event loop.
            def download():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])

            await asyncio.to_thread(download)

            files = [
                p for p in Path(tmpdir).iterdir()
                if p.is_file()
                and p.name != "cookies.txt"
                and p.stat().st_size > 0
            ]

            if not files:
                raise FileNotFoundError(
                    "Downloaded video was not found"
                )

            # Select the largest output file.
            filename = max(
                files,
                key=lambda p: p.stat().st_size
            )

            if filename.stat().st_size > MAX_FILE_SIZE:
                await status.edit_text(
                    "❌ حجم ویدئو بیشتر از حد مجاز این ربات است."
                )
                return

            await status.edit_text(
                "📤 ویدئو آماده است؛ در حال ارسال..."
            )

            with filename.open("rb") as video:
                await context.bot.send_video(
                    chat_id=message.chat_id,
                    video=video,
                    supports_streaming=True,
                    caption="🎬 ویدئوی شما آماده است.",
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=30,
                    pool_timeout=30,
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

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
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
