import os
import re
import uuid
import logging
import threading
from flask import Flask, request
from dotenv import load_dotenv
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import yt_dlp

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
        "🎬 **Welcome to Video Downloader Bot!**\n\n"
        "I can download publicly available videos from platforms like YouTube, Facebook, Twitter/X, and more!\n\n"
        "👉 **How to use:**\n"
        "Just send or paste any video link here. I will extract the available formats and let you choose your preferred download size!\n\n"
        "⚠️ *Note: Due to Telegram limits, only formats under 50MB can be sent.*"
    )
    bot.reply_to(message, welcome_text, parse_mode="Markdown")

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    urls = re.findall(URL_REGEX, message.text)
    if not urls:
        bot.reply_to(message, "👋 Send me a video link (e.g. YouTube, Facebook, X) to download it!")
        return

    url = urls[0]
    status_msg = bot.reply_to(message, "🔍 Analyzing link... Please wait.")

    # Run analysis in a thread to keep bot responsive
    threading.Thread(target=analyze_video_link, args=(message.chat.id, status_msg.message_id, url)).start()

def analyze_video_link(chat_id, message_id, url):
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'no_playlist': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        title = info.get('title', 'Video')
        formats = info.get('formats', [])
        
        # Filter formats:
        # 1. Must contain both video and audio (since ffmpeg is not available to merge separate streams)
        # 2. Must be estimated under 50MB (Telegram bot limit)
        valid_formats = []
        seen_res = set() # To keep list clean, group by resolution and extension

        for f in formats:
            vcodec = f.get('vcodec')
            acodec = f.get('acodec')
            
            # Pre-merged format has both codecs populated and they are not 'none'
            if vcodec and vcodec != 'none' and acodec and acodec != 'none':
                # File size calculation
                filesize = f.get('filesize') or f.get('filesize_approx')
                if filesize and filesize > 50 * 1024 * 1024:
                    # Exceeds 50MB Telegram Bot API limit
                    continue
                
                res = f.get('resolution') or f"{f.get('height', 'unknown')}p"
                ext = f.get('ext', 'mp4')
                
                # Check for uniqueness of resolution + extension to keep layout clean
                res_key = f"{res}_{ext}"
                if res_key not in seen_res:
                    seen_res.add(res_key)
                    valid_formats.append(f)

        if not valid_formats:
            bot.edit_message_text(
                "❌ **Error:** No compatible formats under 50MB containing both video and audio could be found for this link.",
                chat_id=chat_id,
                message_id=message_id,
                parse_mode="Markdown"
            )
            return

        # Sort by resolution height (descending quality)
        valid_formats.sort(key=lambda x: x.get('height') or 0, reverse=True)

        # Create session ID
        session_id = str(uuid.uuid4())[:8]
        download_sessions[session_id] = {
            'url': url,
            'title': title,
            'formats': {f['format_id']: f for f in valid_formats}
        }

        # Build inline keyboard
        keyboard = InlineKeyboardMarkup()
        for f in valid_formats:
            format_id = f['format_id']
            res = f.get('resolution') or f"{f.get('height', 'unknown')}p"
            ext = f.get('ext', 'mp4')
            filesize = f.get('filesize') or f.get('filesize_approx')
            
            if filesize:
                size_mb = filesize / (1024 * 1024)
                size_str = f"{size_mb:.1f} MB"
            else:
                size_str = "unknown size"

            btn_text = f"🎬 {res} ({ext.upper()}) - {size_str}"
            callback_data = f"dl:{session_id}:{format_id}"
            keyboard.add(InlineKeyboardButton(text=btn_text, callback_data=callback_data))

        # Send info with keyboard options
        info_text = (
            f"🎬 **Video Found:**\n"
            f"`{title}`\n\n"
            f"Select a size/quality to download:"
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
            f"❌ **Error:** Could not extract video information.\n\n*Details:* {str(e)[:150]}...",
            chat_id=chat_id,
            message_id=message_id,
            parse_mode="Markdown"
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith('dl:'))
def handle_download_callback(call):
    parts = call.data.split(':')
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
        
        ydl_opts = {
            'format': format_id,
            'outtmpl': output_template,
            'quiet': True,
            'no_warnings': True,
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

        # Update status before sending
        bot.edit_message_text(
            f"📤 **Uploading to Telegram...**\n`{title}`",
            chat_id=chat_id,
            message_id=message_id,
            parse_mode="Markdown"
        )

        # Send video file
        with open(downloaded_file_path, 'rb') as video:
            bot.send_video(
                chat_id,
                video,
                caption=f"🎥 **{title}**\n\nDownloaded via Video Downloader Bot",
                parse_mode="Markdown",
                timeout=180 # Longer timeout for uploading video
            )

        # Delete the original status message
        bot.delete_message(chat_id, message_id)

    except Exception as e:
        logger.error(f"Download failed: {e}")
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
        # Run Flask development server when executing locally with RENDER_EXTERNAL_URL
        port = int(os.getenv("PORT", 8080))
        app.run(host="0.0.0.0", port=port)
    else:
        logger.info("Starting Video Downloader Bot polling locally...")
        print("Bot is running in polling mode... Press Ctrl+C to stop.")
        try:
            bot.remove_webhook()
            bot.infinity_polling()
        except Exception as e:
            logger.error(f"Error occurred: {e}")
