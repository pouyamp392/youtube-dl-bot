import os
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


# ---------------- Cookie Helper ----------------

def prepare_cookie_file(src: str, dst: Path) -> int:
    """فایل کوکی را به فرمت درست Netscape (با Tab) تبدیل می‌کند.

    اگر موقع Paste کردن Tabها به فاصله تبدیل شده باشند یا خط اول
    افتاده باشد، اینجا درست می‌شود. تعداد کوکی‌های معتبر را برمی‌گرداند.
    """
    lines = ["# Netscape HTTP Cookie File"]
    with open(src, encoding="utf-8", errors="ignore") as f:
        for raw in f:
            line = raw.replace("\ufeff", "").strip()
            if not line:
                continue
            # کامنت‌ها رد می‌شوند، ولی #HttpOnly_ یک کوکی واقعی است
            if line.startswith("#") and not line.startswith("#HttpOnly_"):
                continue
            parts = line.split(None, 6)  # جداکردن با هر نوع فاصله، حداکثر ۷ ستون
            if len(parts) < 7:
                continue
            lines.append("\t".join(parts))
    dst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines) - 1


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

            # Download runs outside the async event loop.
            # چند کلاینت یوتیوب را پشت‌سرهم امتحان می‌کنیم؛ روی آی‌پی
            # دیتاسنتر (مثل Render) بعضی کلاینت‌ها کمتر بات‌چک می‌شوند.
            def download():
                attempts = [
                    # (اسم، extractor_args، استفاده از کوکی؟)
                    ("tv", {"youtube": {"player_client": ["tv"]}}, False),
                    ("mweb", {"youtube": {"player_client": ["mweb"]}}, False),
                    ("default", None, True),  # پیش‌فرض yt-dlp + کوکی
                ]
                last_err = None
                for name, args, use_cookies in attempts:
                    opts = dict(ydl_opts)
                    if args:
                        opts["extractor_args"] = args
                    if not use_cookies:
                        opts.pop("cookiefile", None)
                    try:
                        logger.info("Trying client: %s", name)
                        with yt_dlp.YoutubeDL(opts) as ydl:
                            ydl.download([url])
                        logger.info("Client '%s' worked", name)
                        return
                    except Exception as e:
                        last_err = e
                        logger.warning(
                            "Client '%s' failed: %s", name, str(e)[:200]
                        )
                raise last_err

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
