import os
import re
import logging
import tempfile
import subprocess
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
import yt_dlp

# ---------------- تنظیمات ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is not set!")

# حالت‌های مکالمه
WAITING_LINK, WAITING_START, WAITING_END = range(3)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


def parse_time(text: str) -> float | None:
    """تبدیل متن به ثانیه. پشتیبانی از: 5 | 5.5 | 1:20 | 01:20.5"""
    text = text.strip().replace("،", ".")
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    if re.fullmatch(r"\d{1,2}:\d{1,2}(\.\d+)?", text):
        parts = text.split(":")
        return int(parts[0]) * 60 + float(parts[1])
    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "سلام 👋\n"
        "لینک پست ردیت که ویدیو داره رو بفرست.\n"
        "بعد ازت می‌پرسم از کجا تا کجا برش بزنم.\n\n"
        "برای لغو هر وقت /cancel بزن."
    )
    return WAITING_LINK


async def receive_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    url = update.message.text.strip()

    if "reddit.com" not in url and "redd.it" not in url:
        await update.message.reply_text("لطفاً یک لینک معتبر ردیت بفرست.")
        return WAITING_LINK

    context.user_data["url"] = url
    await update.message.reply_text(
        "لینک دریافت شد ✅\n"
        "زمان شروع برش رو بگو (مثلاً `0` یا `5` یا `1:20`):"
    )
    return WAITING_START


async def receive_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    start_sec = parse_time(update.message.text)
    if start_sec is None:
        await update.message.reply_text("فرمت زمان اشتباهه. مثال: `0` یا `12` یا `1:05`")
        return WAITING_START

    context.user_data["start"] = start_sec
    await update.message.reply_text(
        "حالا زمان پایان رو بگو (یا مدت زمان برش).\n"
        "مثال: `2.8` یعنی ۲.۸ ثانیه بعد از شروع\n"
        "یا `1:23` برای زمان مطلق"
    )
    return WAITING_END


async def receive_end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    end_sec = parse_time(update.message.text)
    if end_sec is None:
        await update.message.reply_text("فرمت زمان اشتباهه. دوباره بفرست.")
        return WAITING_END

    start = context.user_data["start"]

    # اگر عدد کوچیک داد، احتمالاً مدت زمانه
    if end_sec <= 15 and end_sec > 0:
        duration = end_sec
    else:
        duration = end_sec - start

    if duration <= 0:
        await update.message.reply_text("مدت زمان باید بیشتر از صفر باشه.")
        return WAITING_END

    # ذخیره مدت زمان اصلی
    context.user_data["duration"] = duration

    if duration > 2.9:
        speed = duration / 2.9
        await update.message.reply_text(
            f"مدت زمان انتخابی {duration:.1f} ثانیه است.\n"
            f"ویدیو با سرعت {speed:.1f} برابر تند می‌شه تا در ۲.۹ ثانیه جا بشه...\n"
            "در حال پردازش، لطفاً صبر کن ⏳"
        )
    else:
        await update.message.reply_text("دارم ویدیو رو دانلود و تبدیل می‌کنم... صبر کن ⏳")

    try:
        webm_path = await process_video(
            context.user_data["url"],
            start,
            duration,
            update.effective_user.id
        )
        await update.message.reply_document(
            document=open(webm_path, "rb"),
            filename="sticker.webm",
            caption="✅ فایل آماده استیکر تلگرام\n"
                    "این فایل رو مستقیم به ربات @Stickers بفرست و دستور /newvideo رو بزن."
        )
        os.remove(webm_path)
    except Exception as e:
        logger.error(e, exc_info=True)
        await update.message.reply_text(f"❌ خطا پیش اومد:\n`{str(e)[:400]}`")

    context.user_data.clear()
    return ConversationHandler.END

async def process_video(url: str, start: float, duration: float, user_id: int) -> str:
    temp_dir = Path(tempfile.gettempdir()) / f"tg_sticker_{user_id}"
    temp_dir.mkdir(exist_ok=True)

    # دانلود با yt-dlp
    ydl_opts = {
        "format": "bestvideo+bestaudio/best",
        "outtmpl": str(temp_dir / "original.%(ext)s"),
        "quiet": True,
        "noplaylist": True,
        "merge_output_format": "mp4",
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        downloaded_file = ydl.prepare_filename(info)

        if not os.path.exists(downloaded_file):
            possible = list(temp_dir.glob("original.*"))
            if possible:
                downloaded_file = str(possible[0])
            else:
                raise Exception("فایل دانلود شده پیدا نشد")

    output_file = str(temp_dir / "sticker.webm")

    # محاسبه سرعت
    max_duration = 2.9
    if duration > max_duration:
        speed = duration / max_duration
        # setpts برای تند کردن ویدیو
        pts_filter = f"setpts=PTS/{speed}"
        output_duration = max_duration
    else:
        speed = 1.0
        pts_filter = "setpts=PTS-STARTPTS"
        output_duration = duration

    # فیلتر کامل
    vf = f"{pts_filter},scale='if(eq(a,1),512,if(gt(a,1),512,-2))':'if(eq(a,1),512,if(gt(a,1),-2,512))'"

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-t", str(duration),          # اول قسمت مورد نظر رو برش بزن
        "-i", downloaded_file,
        "-vf", vf,
        "-c:v", "libvpx-vp9",
        "-an",
        "-crf", "32",
        "-b:v", "0",
        "-r", "30",
        "-t", str(output_duration),   # مدت نهایی خروجی
        "-pix_fmt", "yuva420p",
        output_file
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(f"FFmpeg error:\n{result.stderr[-700:]}")

    size_kb = os.path.getsize(output_file) / 1024
    if size_kb > 256:
        # تلاش دوباره با کیفیت پایین‌تر
        cmd[cmd.index("-crf") + 1] = "40"
        subprocess.run(cmd, capture_output=True)
        size_kb = os.path.getsize(output_file) / 1024

    if size_kb > 280:
        raise Exception(f"حجم فایل هنوز بالاست ({size_kb:.0f} KB). ویدیو خیلی پیچیده است.")

    try:
        os.remove(downloaded_file)
    except:
        pass

    return output_file

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("لغو شد.")
    return ConversationHandler.END


def main():
    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_link)],
            WAITING_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_start)],
            WAITING_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_end)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("cancel", cancel))

    print("ربات در حال اجراست...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
