"""
Telegram Photos Backup — Persistent Daemon
==========================================
Runs as a long-lived background process.
Polls the Photos originals folder every 30s and uploads any new media to Telegram.
launchd keeps it alive automatically if it ever crashes.
"""

import os
import json
import sqlite3
import tempfile
import shutil
import logging
import asyncio
import socket
import subprocess
import time
from datetime import datetime

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

CONFIG_FILE         = os.path.expanduser("~/.tele_backup_config.json")
DB_FILE             = os.path.expanduser("~/.tele_backup_state.db")
LOG_DIR             = os.path.expanduser("~/Library/Logs/TelegramPhotosBackup")
LOG_FILE            = os.path.join(LOG_DIR, "backup.log")
PHOTOS_ORIGINALS    = os.path.expanduser("~/Pictures/Photos Library.photoslibrary/originals")
SESSION_FILE        = os.path.expanduser("~/.tele_backup_session")

POLL_INTERVAL   = 30        # seconds between each full scan
MIN_FILE_AGE    = 20        # seconds — don't upload files newer than this (iCloud still writing)
MAX_FILE_SIZE   = 2000 * 1024 * 1024  # 2 GB Telegram limit

SUPPORTED_EXT = ('.jpg', '.jpeg', '.png', '.heic', '.mov', '.mp4', '.m4v', '.gif', '.raw', '.dng')

os.makedirs(LOG_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Logging — both file and stdout so launchd captures it
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("TelegramBackup")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def format_size(b: int) -> str:
    if b >= 1024**3: return f"{b/1024**3:.2f} GB"
    if b >= 1024**2: return f"{b/1024**2:.1f} MB"
    if b >= 1024:    return f"{b/1024:.1f} KB"
    return f"{b} B"


def is_network_available(host="8.8.8.8", port=53, timeout=3) -> bool:
    try:
        socket.setdefaulttimeout(timeout)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((host, port))
        s.close()
        return True
    except OSError:
        return False


def send_notification(title: str, message: str):
    """Best-effort macOS notification."""
    try:
        script = f'display notification "{message}" with title "{title}" sound name "Glass"'
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except Exception:
        pass


def keep_photos_open():
    """Silently launch the Photos app in the background if it's not running."""
    try:
        # Check if Photos is running first so we don't spam it
        result = subprocess.run(["pgrep", "-x", "Photos"], capture_output=True)
        if result.returncode != 0:
            # Not running, launch and completely hide it
            script = '''
            tell application "Photos" to launch
            delay 1
            tell application "System Events"
                if exists process "Photos" then
                    set visible of process "Photos" to false
                end if
            end tell
            '''
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except Exception:
        pass


def get_file_date(path: str) -> str:
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "Unknown"


def build_caption(filename: str, path: str, size: int) -> str:
    ext = os.path.splitext(filename)[1].upper().lstrip('.')
    return (
        f"📁 {filename}\n"
        f"📅 {get_file_date(path)}\n"
        f"🏷 {ext}  •  {format_size(size)}"
    )

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        raise FileNotFoundError(f"Config not found: {CONFIG_FILE}")
    with open(CONFIG_FILE) as f:
        return json.load(f)


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS uploads (
                    uuid      TEXT PRIMARY KEY,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    filename  TEXT,
                    file_size INTEGER DEFAULT 0
                 )""")
    try:
        c.execute("ALTER TABLE uploads ADD COLUMN file_size INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    return conn


def is_uploaded(conn: sqlite3.Connection, file_hash: str) -> bool:
    c = conn.cursor()
    c.execute("SELECT 1 FROM uploads WHERE uuid = ?", (file_hash,))
    return bool(c.fetchone())


def mark_uploaded(conn: sqlite3.Connection, file_hash: str, filename: str, size: int = 0):
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO uploads (uuid, filename, file_size) VALUES (?, ?, ?)",
              (file_hash, filename, size))
    conn.commit()


def get_stats(conn: sqlite3.Connection):
    c = conn.cursor()
    c.execute("SELECT COUNT(*), SUM(file_size) FROM uploads WHERE filename NOT LIKE 'SKIPPED%'")
    r = c.fetchone()
    return r[0] or 0, r[1] or 0

# ---------------------------------------------------------------------------
# Media scanner
# ---------------------------------------------------------------------------

def scan_media() -> list:
    """Return all supported media files, sorted oldest-first.
    
    Uses 'find' via subprocess to bypass macOS TCC/sandbox restrictions
    that prevent long-running daemon processes from walking the Photos Library
    via Python's os.walk(). The subprocess inherits FDA from the shell.
    """
    if not os.path.exists(PHOTOS_ORIGINALS):
        logger.error(f"originals dir not found: {PHOTOS_ORIGINALS}")
        return []

    ext_args = []
    for i, ext in enumerate(SUPPORTED_EXT):
        if i > 0:
            ext_args.append('-o')
        ext_args += ['-iname', f'*{ext}']

    cmd = ['find', PHOTOS_ORIGINALS, '-type', 'f', '('] + ext_args + [')']
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0 and result.stderr:
            logger.error(f"find error: {result.stderr[:200]}")
        files = [line.strip() for line in result.stdout.splitlines()
                 if line.strip() and not os.path.basename(line.strip()).startswith('.')]
    except subprocess.TimeoutExpired:
        logger.error("scan_media: find command timed out")
        return []
    except Exception as e:
        logger.error(f"scan_media error: {e}")
        return []

    logger.info(f"scan_media: found {len(files)} media files")
    files.sort(key=lambda x: os.path.getmtime(x))
    return files

# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

async def upload_file(client, chat_id: int, filepath: str, caption: str, retries: int = 3) -> bool:
    name = os.path.basename(filepath)
    for attempt in range(1, retries + 1):
        try:
            logger.info(f"Uploading {name} (attempt {attempt}/{retries})…")
            await client.send_file(
                chat_id, filepath,
                caption=caption,
                force_document=True,
                allow_cache=False,
                part_size_kb=512
            )
            logger.info(f"✅ Uploaded: {name}")
            return True
        except Exception as e:
            logger.error(f"Upload failed — {name} attempt {attempt}: {e}")
            if attempt < retries:
                await asyncio.sleep(5 * attempt)
    logger.error(f"Giving up on {name}")
    return False

# ---------------------------------------------------------------------------
# One scan-and-upload cycle
# ---------------------------------------------------------------------------

async def run_cycle(client, conn: sqlite3.Connection, channel_id: int,
                    prev_sizes: dict, export_dir: str, cycle_num: int = 0) -> int:
    """
    Scan for new media and upload. Returns number of files uploaded.
    prev_sizes: {filepath: size} from the previous scan — used for stability check.
    """
    now = time.time()
    media = scan_media()
    pending = [m for m in media if not is_uploaded(conn, f"{os.path.basename(m)}_{os.path.getsize(m)}")]
    logger.info(f"Cycle #{cycle_num} — {len(media)} files on disk, {len(pending)} pending upload")
    uploaded = 0
    new_prev_sizes = {}

    for path in media:
        filename = os.path.basename(path)
        try:
            size = os.path.getsize(path)
            mtime = os.path.getmtime(path)
        except OSError:
            continue

        file_hash = f"{filename}_{size}"

        # Already backed up?
        if is_uploaded(conn, file_hash):
            continue

        # Too large for Telegram?
        if size > MAX_FILE_SIZE:
            logger.warning(f"Skipping {filename} — {format_size(size)} exceeds 2 GB limit")
            mark_uploaded(conn, file_hash, f"SKIPPED_2GB_{filename}", size)
            continue

        # ── Stability gate 1: file must be at least MIN_FILE_AGE seconds old ──
        age = now - mtime
        if age < MIN_FILE_AGE:
            logger.debug(f"Too new ({age:.0f}s), will retry: {filename}")
            new_prev_sizes[path] = size
            continue

        # ── Stability gate 2: size must be the same as last scan ──
        # (catches iCloud mid-download where mtime doesn't update until done)
        last_size = prev_sizes.get(path)
        new_prev_sizes[path] = size
        if last_size is None or last_size != size:
            # First time we've seen it, or size changed — wait one more cycle
            logger.debug(f"Size changed/new, will retry next cycle: {filename}")
            continue

        # ── Safe to upload ──
        logger.info(f"New media detected: {filename} ({format_size(size)})")
        caption = build_caption(filename, path, size)
        exported = os.path.join(export_dir, filename)
        try:
            shutil.copy2(path, exported)
            ok = await upload_file(client, channel_id, exported, caption)
            if ok:
                mark_uploaded(conn, file_hash, filename, size)
                uploaded += 1
            # Remove temp copy immediately regardless
        except Exception as e:
            logger.error(f"Error processing {filename}: {e}")
        finally:
            if os.path.exists(exported):
                try:
                    os.remove(exported)
                except OSError:
                    pass

    # Update prev_sizes in-place so caller retains it across cycles
    prev_sizes.clear()
    prev_sizes.update(new_prev_sizes)
    logger.info(f"Cycle #{cycle_num} done — uploaded: {uploaded}, watching: {len(new_prev_sizes)} unstable files")
    return uploaded

# ---------------------------------------------------------------------------
# Main daemon loop
# ---------------------------------------------------------------------------

async def run_daemon():
    logger.info("=" * 60)
    logger.info("Telegram Photos Backup Daemon starting…")
    logger.info("=" * 60)

    # ── Load config ──
    try:
        config = load_config()
    except FileNotFoundError as e:
        logger.error(str(e))
        return

    try:
        api_id = int(config["api_id"])
    except (KeyError, ValueError, TypeError):
        logger.error("Invalid api_id in config.")
        return

    api_hash   = config.get("api_hash", "").strip()
    channel_id = config.get("channel_id", "").strip()

    if not all([api_id, api_hash, channel_id]):
        logger.error("Incomplete config — need api_id, api_hash, channel_id.")
        return

    # Normalise channel_id
    cid = str(channel_id).strip()
    if cid.lstrip("-").isdigit():
        cid_int = int(cid)
        if cid_int > 0:
            cid_int = int(f"-100{cid_int}")
    else:
        logger.error(f"Invalid channel_id: {channel_id}")
        return

    # ── Connect to Telegram (once, persistent) ──
    from telethon import TelegramClient

    client = TelegramClient(SESSION_FILE, api_id, api_hash)
    await client.connect()

    if not await client.is_user_authorized():
        logger.error("Session not authorised. Run interactive_login.py first.")
        await client.disconnect()
        return

    logger.info("✅ Connected to Telegram. Daemon is active.")
    send_notification("📸 Backup Daemon", "Auto-backup started. All new photos will be sent automatically.")

    conn       = init_db()
    export_dir = os.path.join(tempfile.gettempdir(), "tele_backup_export")
    os.makedirs(export_dir, exist_ok=True)
    prev_sizes: dict = {}  # stability tracking across cycles
    offline_warned   = False
    cycle_num        = 0

    # ── Polling loop ──
    while True:
        try:
            if not is_network_available():
                if not offline_warned:
                    logger.warning("No internet. Will retry when network is back…")
                    offline_warned = True
                await asyncio.sleep(POLL_INTERVAL)
                continue
            offline_warned = False

            # Reconnect if Telegram dropped the connection
            if not client.is_connected():
                logger.info("Reconnecting to Telegram…")
                await client.connect()

            # Ensure Photos is running hidden to force iCloud synchronization
            keep_photos_open()

            cycle_num += 1
            uploaded = await run_cycle(client, conn, cid_int, prev_sizes, export_dir, cycle_num)

            if uploaded > 0:
                total_count, total_bytes = get_stats(conn)
                logger.info(
                    f"✅ Uploaded {uploaded} file(s) this cycle — "
                    f"Total backup: {total_count} files ({format_size(total_bytes)})"
                )
                send_notification(
                    "📸 Telegram Backup",
                    f"{uploaded} new file(s) backed up automatically!"
                )

        except Exception as e:
            logger.exception(f"Cycle error (will retry in {POLL_INTERVAL}s): {e}")

        await asyncio.sleep(POLL_INTERVAL)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        asyncio.run(run_daemon())
    except KeyboardInterrupt:
        logger.info("Daemon stopped by user.")
    except Exception as e:
        logger.exception(f"Fatal daemon error: {e}")
        send_notification("❌ Backup Daemon Crashed", str(e)[:100])
