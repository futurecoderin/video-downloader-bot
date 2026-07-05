"""
Google Sheets Activity Logger - shared helper for Telegram bots.

Setup:
1. Place 'google_credentials.json' (service account key) in the same folder as this file.
2. Add GOOGLE_SPREADSHEET_ID=<your_sheet_id> to env.txt in the same folder.
3. Share the Google Sheet with the service account email (Editor access).

If either credential is missing, logging is silently skipped and the bot runs normally.
"""

import os
import logging
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_sheet_cache: dict = {}   # tab_name -> gspread.Worksheet
_spreadsheet = None
_initialized = False
_disabled = False          # set True if setup is missing so we stop retrying


def _init():
    """Lazy-initialize the gspread client once. Thread-safe.
    Retries on transient errors (e.g. PermissionError before sheet was shared).
    Only permanently disables if credentials file is missing."""
    global _spreadsheet, _initialized, _disabled
    if _initialized or _disabled:
        return
    with _lock:
        if _initialized or _disabled:
            return
        try:
            import gspread
            from google.oauth2.service_account import Credentials

            script_dir = os.path.dirname(os.path.abspath(__file__))
            creds_path = os.path.join(script_dir, "google_credentials.json")
            spreadsheet_id = os.getenv("GOOGLE_SPREADSHEET_ID", "").strip()

            if not os.path.exists(creds_path):
                logger.warning("📊 Sheets: google_credentials.json not found — logging disabled permanently.")
                _disabled = True   # permanent: file genuinely missing
                return
            if not spreadsheet_id:
                logger.warning("📊 Sheets: GOOGLE_SPREADSHEET_ID not set — logging disabled permanently.")
                _disabled = True   # permanent: env var genuinely missing
                return

            scopes = ["https://www.googleapis.com/auth/spreadsheets"]
            creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
            client = gspread.authorize(creds)
            _spreadsheet = client.open_by_key(spreadsheet_id)
            _initialized = True   # only set True on SUCCESS
            logger.info("📊 Google Sheets logger initialized successfully.")
        except Exception as e:
            logger.error(f"📊 Sheets init failed (will retry next call): {e}")
            # Do NOT set _initialized=True — next log call will retry


def _get_worksheet(tab_name: str, headers: list):
    """Return cached worksheet, creating it with headers if it doesn't exist."""
    if tab_name in _sheet_cache:
        return _sheet_cache[tab_name]
    with _lock:
        if tab_name in _sheet_cache:
            return _sheet_cache[tab_name]
        try:
            try:
                ws = _spreadsheet.worksheet(tab_name)
            except Exception:
                # Tab doesn't exist yet — create it
                ws = _spreadsheet.add_worksheet(title=tab_name, rows=5000, cols=len(headers))
                ws.append_row(headers, value_input_option="USER_ENTERED")
            _sheet_cache[tab_name] = ws
            return ws
        except Exception as e:
            logger.error(f"📊 Sheets: Could not get/create tab '{tab_name}': {e}")
            return None


def _append_async(tab_name: str, headers: list, row: list):
    """Append a row in a daemon thread so it never blocks the bot."""
    def _write():
        _init()
        if _disabled or _spreadsheet is None:
            return
        try:
            ws = _get_worksheet(tab_name, headers)
            if ws:
                ws.append_row(row, value_input_option="USER_ENTERED")
        except Exception as e:
            logger.error(f"📊 Sheets: Failed to write row: {e}")

    threading.Thread(target=_write, daemon=True).start()


# ─────────────────────────────────────────────
# Public helpers — one per bot
# ─────────────────────────────────────────────

VIDEO_DOWNLOADER_HEADERS = [
    "Timestamp (UTC)", "User ID", "Username", "Full Name", "Language",
    "Action", "Detail", "Status"
]

FORWARDER_HEADERS = [
    "Timestamp (UTC)", "User ID", "Username", "Full Name", "Language",
    "Action", "Detail"
]


def log_video_downloader(user, action: str, detail: str = "", status: str = ""):
    """
    Log a Video Downloader Bot event.

    user  — telebot message.from_user object
    action — e.g. "URL Received", "Quality Selected", "Download Success"
    detail — URL, quality name, error message, etc.
    status — "OK", "Too Large", "Failed", etc.
    """
    username = f"@{user.username}" if getattr(user, "username", None) else "N/A"
    full_name = " ".join(filter(None, [
        getattr(user, "first_name", ""),
        getattr(user, "last_name", "") or ""
    ])) or "N/A"
    row = [
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        str(getattr(user, "id", "")),
        username,
        full_name,
        getattr(user, "language_code", "N/A") or "N/A",
        action,
        str(detail)[:500],
        status,
    ]
    _append_async("Video Downloader", VIDEO_DOWNLOADER_HEADERS, row)


def log_message_forwarder(user_id, username, first_name, last_name, lang, action: str, detail: str = ""):
    """
    Log a Message Forwarder Bot event.

    All user fields passed individually since Telethon uses different objects.
    """
    full_name = " ".join(filter(None, [first_name or "", last_name or ""])) or "N/A"
    uname = f"@{username}" if username else "N/A"
    row = [
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        str(user_id),
        uname,
        full_name,
        lang or "N/A",
        action,
        str(detail)[:500],
    ]
    _append_async("Message Forwarder", FORWARDER_HEADERS, row)


CHAR_COUNTER_HEADERS = [
    "Timestamp (UTC)", "User ID", "Username", "Full Name", "Language",
    "Action", "Detail"
]


def log_char_counter(user, action: str, detail: str = ""):
    """
    Log a Character Counter Bot event.

    user   — telebot message.from_user object
    action — e.g. "/start or /help", "Text Analyzed", "Forwarded Message", "No Text Sent"
    detail — e.g. "chars=142, words=28, lines=3"
    """
    username = f"@{user.username}" if getattr(user, "username", None) else "N/A"
    full_name = " ".join(filter(None, [
        getattr(user, "first_name", ""),
        getattr(user, "last_name", "") or ""
    ])) or "N/A"
    row = [
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        str(getattr(user, "id", "")),
        username,
        full_name,
        getattr(user, "language_code", "N/A") or "N/A",
        action,
        str(detail)[:500],
    ]
    _append_async("Char Counter", CHAR_COUNTER_HEADERS, row)
