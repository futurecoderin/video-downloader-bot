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
import subprocess
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

def get_video_duration(file_path):
    """Get video duration in seconds using ffprobe."""
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            file_path
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        return float(result.stdout.strip())
    except Exception as e:
        logger.warning(f"Could not get video duration via ffprobe: {e}")
        return 0.0

def compress_video_to_limit(input_path, target_max_mb=47.5):
    """
    Compress video with FFmpeg to ensure it is strictly below target_max_mb (Telegram Bot API 50MB limit).
    Returns path to compressed video if successful and within limits, otherwise None.
    """
    if not shutil.which("ffmpeg"):
        logger.warning("ffmpeg is not installed, skipping compression")
        return None

    duration = get_video_duration(input_path)
    if duration <= 0:
        logger.warning("Could not determine duration for compression")
        return None

    target_bits = target_max_mb * 1024 * 1024 * 8
    target_total_bps = target_bits / duration

    # Allocate audio bitrate (48kbps - 96kbps)
    if target_total_bps < 180_000:
        audio_bps = 48_000
    elif target_total_bps < 350_000:
        audio_bps = 64_000
    else:
        audio_bps = 96_000

    video_bps = int(target_total_bps - audio_bps)
    if video_bps < 40_000:
        logger.warning(f"Calculated video bitrate {video_bps} is too low to produce a watchable video")
        return None

    base, _ = os.path.splitext(input_path)
    output_path = f"{base}_compressed.mp4"

    # Scale video down if bitrate is low to maintain sharp visual quality
    scale_filter = "scale='min(1280,iw)':-2"
    if video_bps < 300_000:
        scale_filter = "scale='min(640,iw)':-2"
    elif video_bps < 600_000:
        scale_filter = "scale='min(854,iw)':-2"

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-c:v", "libx264",
        "-b:v", str(video_bps),
        "-maxrate", str(int(video_bps * 1.3)),
        "-bufsize", str(int(video_bps * 2)),
        "-vf", scale_filter,
        "-c:a", "aac",
        "-b:a", str(audio_bps),
        "-preset", "fast",
        "-movflags", "+faststart",
        output_path
    ]

    logger.info(f"Starting FFmpeg compression: duration={duration:.1f}s, v_bitrate={video_bps//1000}k, a_bitrate={audio_bps//1000}k")
    try:
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        if os.path.exists(output_path):
            comp_size = os.path.getsize(output_path)
            logger.info(f"Compressed file created: {comp_size / (1024*1024):.2f} MB")
            if comp_size <= target_max_mb * 1024 * 1024:
                return output_path
            else:
                logger.warning(f"Compressed file still exceeded target size: {comp_size / (1024*1024):.2f} MB")
                return output_path
    except Exception as e:
        logger.error(f"FFmpeg compression failed: {e}")
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except Exception:
                pass
    return None

def analyze_video_link(chat_id, message_id, url, from_user=None):
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'no_playlist': True,
            'socket_timeout': 30,
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
        MAX_EST_BYTES = 200 * 1024 * 1024  # Allow up to 200MB since we can auto-compress

        valid_options = []
        for opt in FORMAT_OPTIONS:
            bps = BITRATE.get(opt['height'], 2_000_000)
            est_bytes = int(duration * bps / 8) if duration else 0
            if est_bytes > 0 and est_bytes > MAX_EST_BYTES:
                logger.info(f"Skipping {opt['label']}: estimated {est_bytes/1024/1024:.1f}MB > 200MB")
                continue
            if not ffmpeg_available and opt['height'] > 360:
                continue
            valid_options.append(opt)

        # Always keep at least 360p as fallback
        if not valid_options:
            valid_options = [FORMAT_OPTIONS[-1]]

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
            f"_(Videos over 50MB are automatically optimized to fit Telegram)_"
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
        err_str = str(e)
        if "HTTP Error 530" in err_str or "530" in err_str:
            err_msg = (
                "❌ **Video Unavailable on Host Server**\n\n"
                "The hosting site (CDN) returned a **530 Origin Host Error**.\n"
                "This means the video stream has been deleted, expired, or is broken on the source website's servers."
            )
        elif "Unsupported URL" in err_str:
            err_msg = "❌ **Unsupported Link:** This website or link type is currently not supported."
        else:
            err_msg = f"❌ **Error:** Could not extract video information.\n\n*Details:* {err_str[:200]}"

        bot.edit_message_text(
            err_msg,
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
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        return

    # Answer query to stop loading spinner
    bot.answer_callback_query(call.id, "Downloading started...")

    # Log quality chosen
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
    compressed_file_path = None
    try:
        output_template = os.path.join(downloads_dir, f"{session_id}_%(title)s.%(ext)s")
        ydl_format = format_info.get('ydl_format', format_id)
        logger.info(f"Downloading format: {ydl_format} for {url}")

        ydl_opts = {
            'format': ydl_format,
            'outtmpl': output_template,
            'quiet': True,
            'no_warnings': True,
            'socket_timeout': 60,
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
            if filename.startswith(session_id) and not filename.endswith("_compressed.mp4"):
                downloaded_file_path = os.path.join(downloads_dir, filename)
                break

        if not downloaded_file_path or not os.path.exists(downloaded_file_path):
            raise Exception("Downloaded file not found on disk.")

        actual_size = os.path.getsize(downloaded_file_path)
        MAX_TG_SIZE = 49.5 * 1024 * 1024  # 49.5MB safe Telegram Bot API limit
        file_to_send = downloaded_file_path
        was_compressed = False

        if actual_size > MAX_TG_SIZE:
            size_mb = actual_size / (1024 * 1024)
            logger.info(f"File is {size_mb:.1f} MB (>49.5MB). Initiating auto-compression...")
            bot.edit_message_text(
                f"🔄 **File is {size_mb:.1f} MB (exceeds Telegram 50 MB limit).**\n"
                f"`{title}`\n\n"
                f"Compressing video to fit Telegram's limit... Please wait a moment.",
                chat_id=chat_id,
                message_id=message_id,
                parse_mode="Markdown"
            )

            compressed_file_path = compress_video_to_limit(downloaded_file_path, target_max_mb=47.5)
            if compressed_file_path and os.path.exists(compressed_file_path):
                comp_size = os.path.getsize(compressed_file_path)
                if comp_size <= 49.5 * 1024 * 1024:
                    file_to_send = compressed_file_path
                    was_compressed = True
                    logger.info(f"Compression successful: {comp_size / (1024*1024):.1f} MB")
                else:
                    logger.warning(f"Compressed file still over limit: {comp_size / (1024*1024):.1f} MB")

        final_size = os.path.getsize(file_to_send)
        final_mb = final_size / (1024 * 1024)

        if final_size > 50 * 1024 * 1024:
            from_user = session.get('from_user')
            if from_user:
                sheets_logger.log_video_downloader(from_user, "Download", format_id, f"Too Large ({final_mb:.1f} MB)")
            bot.edit_message_text(
                f"⚠️ **File Too Large for Telegram**\n"
                f"`{title}`\n\n"
                f"Downloaded video is **{final_mb:.1f} MB** and could not be compressed under Telegram's 50 MB limit.\n"
                f"Please try selecting a lower quality option (e.g. 360p or 480p).",
                chat_id=chat_id,
                message_id=message_id,
                parse_mode="Markdown"
            )
            return

        # Update status before sending
        bot.edit_message_text(
            f"📤 **Uploading to Telegram...**\n`{title}`\n_{final_mb:.1f} MB_",
            chat_id=chat_id,
            message_id=message_id,
            parse_mode="Markdown"
        )

        caption_note = "\n_(Optimized for Telegram 50MB limit)_" if was_compressed else ""
        caption_text = f"🎥 **{title}**{caption_note}\n\nDownloaded via Video Downloader Bot\n🔗 abhishekvigyan.com"

        # Send video file
        with open(file_to_send, 'rb') as video:
            bot.send_video(
                chat_id,
                video,
                caption=caption_text,
                parse_mode="Markdown",
                timeout=240
            )

        # Log success
        from_user = session.get('from_user')
        if from_user:
            sheets_logger.log_video_downloader(from_user, "Download", format_id, f"Success ({final_mb:.1f} MB)")

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
        # Cleanup files from disk
        for fpath in [downloaded_file_path, compressed_file_path]:
            if fpath and os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except Exception as ex:
                    logger.error(f"Failed to delete file {fpath}: {ex}")
        
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
