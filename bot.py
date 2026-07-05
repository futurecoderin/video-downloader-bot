import os
import re
import uuid
import logging
import shutil
import threading
from flask import Flask, request
from dotenv import load_dotenv
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import yt_dlp
import sheets_logger

# Load environment variables
script_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(script_dir, ".env"))
if not os.getenv("TELEGRAM_BOT_TOKEN"):
    load_dotenv(os.path.join(script_dir, "env.txt"))

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Setup logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Ensure downloads directory exists
downloads_dir = os.path.join(script_dir, "downloads")
os.makedirs(downloads_dir, exist_ok=True)

if not TOKEN:
    print("❌ ERROR: TELEGRAM_BOT_TOKEN is not set in the env.txt or .env file!")
    print("\nHow to configure:")
    print("1. Search for @BotFather on Telegram and start a chat.")
    print("2. Send '/newbot' and follow instructions to get your Token.")
    print("3. Paste the token into the 'env.txt' file inside 'video_downloader/':")
    print("   TELEGRAM_BOT_TOKEN=your_actual_token_here")
    import sys
    sys.exit(0)

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)


@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return 'OK', 200
    else:
        return 'Forbidden', 403

# Automatically set webhook if WEBHOOK_URL or RENDER_EXTERNAL_URL is available
public_url = os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL")
if public_url:
    logger.info(f"Setting webhook to {public_url}/webhook...")
    bot.remove_webhook()
    bot.set_webhook(url=f"{public_url}/webhook")

# In-memory session storage for downloads
# Format: { session_id: { "url": url, "title": title, "formats": { format_id: format_info } } }
download_sessions = {}

# Simple regex to check for URLs
URL_REGEX = r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+[^\s]*'

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    welcome_text = (
        "🎬 *Welcome to Video Downloader Bot!*\n\n"
        "I can download publicly available videos from platforms like YouTube, Instagram, "
        "X (Twitter), TikTok, Facebook, and more!\n\n"
        "👉 *How to use:*\n"
        "Just send or paste any public video link. I'll extract available qualities "
        "so you can choose your preferred download size!\n\n"
        "⚠️ _Note: Due to Telegram limits, only files under 50 MB can be sent._\n\n"
        "🔗 _Developed & maintained by_ [abhishekvigyan.com](https://abhishekvigyan.com)"
    )
    bot.reply_to(message, welcome_text, parse_mode="Markdown", disable_web_page_preview=True)
    sheets_logger.log_video_downloader(message.from_user, "/start or /help", "", "OK")

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    urls = re.findall(URL_REGEX, message.text)
    if not urls:
        bot.reply_to(message, "👋 Send me a video link (e.g. YouTube, Instagram, X) to download it!")
        return

    url = urls[0]
    status_msg = bot.reply_to(message, "🔍 Analyzing link... Please wait.")
    # Log URL received
    sheets_logger.log_video_downloader(message.from_user, "URL Received", url, "Analyzing")
    # Run analysis in a thread to keep bot responsive
    threading.Thread(target=analyze_video_link, args=(message.chat.id, status_msg.message_id, url, message.from_user)).start()

# yt-dlp format selector strings (stored in session, short key used in callback_data)
# These leverage yt-dlp's built-in format resolution instead of manual filtering.
FORMAT_OPTIONS = [
    {
        'key': 'q_best',
        'label': '🏆 Best Quality',
        'ydl_format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best',
        'height': 9999,
    },
    {
        'key': 'q_720',
        'label': '📺 720p HD',
        'ydl_format': 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]/best',
        'height': 720,
    },
    {
        'key': 'q_480',
        'label': '📱 480p',
        'ydl_format': 'bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best[height<=480]/best',
        'height': 480,
    },
    {
        'key': 'q_360',
        'label': '📷 360p',
        'ydl_format': 'bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best[height<=360]/best',
        'height': 360,
    },
]

def analyze_video_link(chat_id, message_id, url, from_user=None):
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'no_playlist': True,
            'extractor_args': {
                'youtube': {
                    'player_client': ['ios', 'android', 'web'],
                }
            },
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        title = info.get('title', 'Video')
        duration = info.get('duration') or 0  # seconds
        logger.info(f"Video: {title}, duration: {duration}s")

        ffmpeg_available = shutil.which("ffmpeg") is not None
        logger.info(f"ffmpeg available: {ffmpeg_available}")

        # Build quality options — estimate sizes based on duration
        # Rough bitrate estimates: 720p~2.5Mbps, 480p~1.2Mbps, 360p~0.7Mbps total
        BITRATE = {9999: 4_000_000, 720: 2_500_000, 480: 1_200_000, 360: 700_000}  # bits/sec
        MAX_BYTES = 49 * 1024 * 1024  # 49MB safe limit

        valid_options = []
        for opt in FORMAT_OPTIONS:
            bps = BITRATE.get(opt['height'], 2_000_000)
            est_bytes = int(duration * bps / 8) if duration else 0
            # Skip options we're CERTAIN exceed 49MB
            if est_bytes > 0 and est_bytes > MAX_BYTES:
                logger.info(f"Skipping {opt['label']}: estimated {est_bytes/1024/1024:.1f}MB > 49MB")
                continue
            # Skip high-res options if ffmpeg not available (can't merge streams)
            if not ffmpeg_available and opt['height'] > 360:
                continue
            valid_options.append(opt)

        # Always keep at least 360p as last resort even for long videos
        if not valid_options:
            valid_options = [FORMAT_OPTIONS[-1]]  # 360p fallback

        # Create session ID
        session_id = str(uuid.uuid4())[:8]
        download_sessions[session_id] = {
            'url': url,
            'title': title,
            'duration': duration,
            'formats': {opt['key']: opt for opt in valid_options},
            'from_user': from_user,
        }

        # Build inline keyboard
        keyboard = InlineKeyboardMarkup()
        for opt in valid_options:
            bps = BITRATE.get(opt['height'], 2_000_000)
            est_bytes = int(duration * bps / 8) if duration else 0
            if est_bytes > 0:
                est_mb = est_bytes / (1024 * 1024)
                size_str = f"~{est_mb:.0f} MB"
            else:
                size_str = "size unknown"
            btn_text = f"{opt['label']} ({size_str})"
            callback_data = f"dl:{session_id}:{opt['key']}"
            keyboard.add(InlineKeyboardButton(text=btn_text, callback_data=callback_data))

        duration_str = f"{int(duration//60)}m {int(duration%60)}s" if duration else "unknown"
        info_text = (
            f"🎬 **Video Found:**\n"
            f"`{title}`\n"
            f"⏱ Duration: {duration_str}\n\n"
            f"Select quality to download:\n"
            f"_(Telegram limit: 50MB. Large files may fail.)_"
        )
        bot.edit_message_text(
            info_text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"Failed to analyze link {url}: {e}")
        bot.edit_message_text(
            f"❌ **Error:** Could not extract video information.\n\n*Details:* {str(e)[:200]}",
            chat_id=chat_id,
            message_id=message_id,
            parse_mode="Markdown"
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith('dl:'))
def handle_download_callback(call):
    # Use maxsplit=2 so format IDs like "140+251" or those containing colons are safe
    parts = call.data.split(':', 2)
    if len(parts) != 3:
        return

    _, session_id, format_id = parts
    
    session = download_sessions.get(session_id)
    if not session:
        bot.answer_callback_query(call.id, "❌ Session expired! Please resend the video link.", show_alert=True)
        # Remove buttons from message
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        return

    # Answer query to stop loading spinner
    bot.answer_callback_query(call.id, "Downloading started...")

    # Log quality chosen
    session = download_sessions.get(session_id)
    if session:
        from_user = session.get('from_user')
        fmt = session.get('formats', {}).get(format_id, {})
        label = fmt.get('label', format_id)
        if from_user:
            sheets_logger.log_video_downloader(from_user, "Quality Selected", label, "Downloading")

    # Start download in a thread
    threading.Thread(target=download_and_send_video, args=(call.message.chat.id, call.message.message_id, session_id, format_id)).start()

def download_and_send_video(chat_id, message_id, session_id, format_id):
    session = download_sessions.get(session_id)
    if not session:
        return

    url = session['url']
    title = session['title']
    format_info = session['formats'].get(format_id)

    if not format_info:
        bot.send_message(chat_id, "❌ Error: Selected format not found.")
        return

    # Update message status
    bot.edit_message_text(
        f"⏳ **Downloading video...**\n`{title}`\n\nPlease wait a moment.",
        chat_id=chat_id,
        message_id=message_id,
        parse_mode="Markdown"
    )

    downloaded_file_path = None
    try:
        # Set output template using unique session ID to identify the file
        output_template = os.path.join(downloads_dir, f"{session_id}_%(title)s.%(ext)s")
        
        # Use ydl_format from the session option if available, otherwise use format_id directly
        ydl_format = format_info.get('ydl_format', format_id)
        logger.info(f"Downloading format: {ydl_format} for {url}")

        ydl_opts = {
            'format': ydl_format,
            'outtmpl': output_template,
            'quiet': True,
            'no_warnings': True,
            'merge_output_format': 'mp4',
            'extractor_args': {
                'youtube': {
                    'player_client': ['ios', 'android', 'web'],
                }
            },
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # Scan download folder for the downloaded file
        for filename in os.listdir(downloads_dir):
            if filename.startswith(session_id):
                downloaded_file_path = os.path.join(downloads_dir, filename)
                break

        if not downloaded_file_path or not os.path.exists(downloaded_file_path):
            raise Exception("Downloaded file not found on disk.")

        # Check actual file size before attempting upload
        actual_size = os.path.getsize(downloaded_file_path)
        MAX_TG_SIZE = 50 * 1024 * 1024  # 50MB Telegram Bot API limit
        if actual_size > MAX_TG_SIZE:
            size_mb = actual_size / (1024 * 1024)
            from_user = session.get('from_user')
            if from_user:
                sheets_logger.log_video_downloader(from_user, "Download", format_id, f"Too Large ({size_mb:.1f} MB)")
            bot.edit_message_text(
                f"⚠️ **File Too Large for Telegram**\n"
                f"`{title}`\n\n"
                f"Downloaded file is **{size_mb:.1f} MB**, but Telegram's Bot API limit is 50 MB.\n"
                f"Try a lower quality option.",
                chat_id=chat_id,
                message_id=message_id,
                parse_mode="Markdown"
            )
            return

        # Update status before sending
        size_mb = actual_size / (1024 * 1024)
        bot.edit_message_text(
            f"📤 **Uploading to Telegram...**\n`{title}`\n_{size_mb:.1f} MB_",
            chat_id=chat_id,
            message_id=message_id,
            parse_mode="Markdown"
        )

        # Send video file
        with open(downloaded_file_path, 'rb') as video:
            bot.send_video(
                chat_id,
                video,
                caption=f"🎥 **{title}**\n\nDownloaded via Video Downloader Bot\n🔗 abhishekvigyan.com",
                parse_mode="Markdown",
                timeout=180
            )

        # Log success
        from_user = session.get('from_user')
        if from_user:
            sheets_logger.log_video_downloader(from_user, "Download", format_id, f"Success ({size_mb:.1f} MB)")

        # Delete the original status message
        bot.delete_message(chat_id, message_id)

    except Exception as e:
        logger.error(f"Download failed: {e}")
        from_user = session.get('from_user') if session else None
        if from_user:
            sheets_logger.log_video_downloader(from_user, "Download", format_id, f"Failed: {str(e)[:80]}")
        bot.send_message(
            chat_id,
            f"❌ **Failed to download video.**\n\n*Error:* {str(e)[:150]}...",
            parse_mode="Markdown"
        )
    finally:
        # Cleanup file from disk
        if downloaded_file_path and os.path.exists(downloaded_file_path):
            try:
                os.remove(downloaded_file_path)
            except Exception as ex:
                logger.error(f"Failed to delete file {downloaded_file_path}: {ex}")
        
        # Remove session to free memory
        if session_id in download_sessions:
            del download_sessions[session_id]

if __name__ == "__main__":
    if public_url:
        port = int(os.getenv("PORT", 8080))
        app.run(host="0.0.0.0", port=port)
    else:
        logger.info("Starting Video Downloader Bot polling locally...")
        print("Bot is running in polling mode... Press Ctrl+C to stop.")
        try:
            bot.remove_webhook()
            # Set bot About/Description now that connection is confirmed
            try:
                bot.set_my_description(
                    "🎬 Download videos from YouTube, Instagram, X (Twitter), TikTok, and more!\n\n"
                    "Just send any public video link and choose your preferred quality.\n\n"
                    "⚠️ Files are limited to 50 MB due to Telegram Bot API restrictions.\n\n"
                    "🔗 Developed & maintained by https://abhishekvigyan.com"
                )
                bot.set_my_short_description(
                    "Download videos from YouTube & more! Developed by abhishekvigyan.com"
                )
                logger.info("Bot description updated successfully.")
            except Exception as _de:
                logger.warning(f"Could not set bot description: {_de}")
            bot.infinity_polling()
        except Exception as e:
            logger.error(f"Error occurred: {e}")
