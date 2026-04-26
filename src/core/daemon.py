import os
import sys
import time
import shutil
import asyncio
import tempfile
import logging

from telethon import TelegramClient
from telethon.errors import FloodWaitError

from src.config import (
    POLL_INTERVAL, MIN_FILE_AGE, MAX_FILE_SIZE,
    SESSION_FILE, SUPPORTED_EXT, TELEGRAM_API_ID, TELEGRAM_API_HASH
)
from src.utils import (
    load_config, is_network_available, send_notification,
    build_caption, format_size, get_disk_free_percent
)
from src.core.db import init_db, is_uploaded, mark_uploaded
from src.core.scanner import scan_media
from src.core.uploader import upload_file
from src.media.processor import get_exif_date, detect_faces, compress_video
from src.api.server import state as api_state

logger = logging.getLogger("TelegramBackup")

# Module-level placeholders — initialized inside run_daemon
_UPLOAD_SEMAPHORE = None
_DB_LOCK = None


async def process_file(path, client, conn, channel_id, export_dir,
                       convert_heic, compress_videos, free_up_space, now) -> bool:
    filename = os.path.basename(path)
    ext = filename.lower()
    try:
        size  = os.path.getsize(path)
        mtime = os.path.getmtime(path)
    except OSError:
        return False

    file_hash = f"{filename}_{size}"

    async with _DB_LOCK:
        if is_uploaded(conn, file_hash):
            return False

    if size > MAX_FILE_SIZE:
        async with _DB_LOCK:
            if not is_uploaded(conn, file_hash):
                mark_uploaded(conn, file_hash, f"SKIPPED_TOO_LARGE_{filename}", size)
        return False

    if now - mtime < MIN_FILE_AGE:
        return False

    # Mark in-flight to prevent duplicate uploads
    async with _DB_LOCK:
        if is_uploaded(conn, file_hash):
            return False
        mark_uploaded(conn, file_hash, f"IN_FLIGHT_{filename}", size)

    async with _UPLOAD_SEMAPHORE:
        api_state["status"] = f"Uploading {filename}"
        safe_name = f"{int(time.time() * 1000)}_{filename}"
        exported  = os.path.join(export_dir, safe_name)
        final_export = None

        try:
            await asyncio.to_thread(shutil.copy2, path, exported)
            final_export = exported

            tags, date_taken = [], None

            if ext.endswith(('.jpg', '.jpeg', '.png', '.dng', '.heic')):
                date_taken = await asyncio.to_thread(get_exif_date, exported)
                if await asyncio.to_thread(detect_faces, exported):
                    tags.append("People")

                if convert_heic and ext.endswith('.heic'):
                    from src.media.image_converter import convert_heic_to_jpg
                    final_export = await convert_heic_to_jpg(exported, export_dir)

            elif ext.endswith(('.mov', '.mp4', '.m4v', '.avi', '.mkv')) and compress_videos:
                comp_path = os.path.join(export_dir, f"compressed_{safe_name}")
                final_export = await compress_video(exported, comp_path)

            caption = build_caption(filename, path, size, date_taken=date_taken, tags=tags)

            success = await upload_file(client, channel_id, final_export, caption)

            if success:
                async with _DB_LOCK:
                    mark_uploaded(conn, file_hash, filename, size)
                api_state["total_files"] = api_state.get("total_files", 0) + 1
                api_state["total_size"]  = format_size(
                    (api_state.get("_raw_size", 0) + size)
                )
                api_state["_raw_size"] = api_state.get("_raw_size", 0) + size

                if free_up_space and get_disk_free_percent() < 10.0:
                    try:
                        os.remove(path)
                        logger.info(f"🗑 Freed space: deleted {filename}")
                    except Exception:
                        pass
                return True
            else:
                async with _DB_LOCK:
                    conn.execute("DELETE FROM uploads WHERE uuid = ?", (file_hash,))
                    conn.commit()
                return False

        except Exception as e:
            logger.exception(f"process_file error ({filename}): {e}")
            async with _DB_LOCK:
                try:
                    conn.execute("DELETE FROM uploads WHERE uuid = ?", (file_hash,))
                    conn.commit()
                except Exception:
                    pass
            return False
        finally:
            for f in set([exported, final_export]):
                if f and f != path and os.path.exists(f):
                    try:
                        os.remove(f)
                    except Exception:
                        pass


async def run_daemon(log_callback=None):
    """Main backup daemon loop. log_callback(msg) is called for GUI status updates."""

    def log(msg):
        logger.info(msg)
        if log_callback:
            log_callback(msg)

    log("Daemon starting…")

    try:
        config     = load_config()
        api_id     = int(config.get("api_id", TELEGRAM_API_ID))
        api_hash   = config.get("api_hash", TELEGRAM_API_HASH)
        channel_id = int(config["channel_id"])
        photos_path = config.get("photos_path", "")
    except Exception as e:
        log(f"❌ Config error: {e} — go to Settings and save your Channel ID.")
        return

    if not photos_path or not os.path.exists(photos_path):
        log(f"❌ Photos folder not found: {photos_path}  — check Settings.")
        return

    client = TelegramClient(SESSION_FILE, api_id, api_hash)
    try:
        await client.connect()
    except Exception as e:
        log(f"❌ Cannot connect to Telegram: {e}")
        return

    if not await client.is_user_authorized():
        log("❌ Not authorized. Please log in first via the Login screen.")
        await client.disconnect()
        return

    global _UPLOAD_SEMAPHORE, _DB_LOCK
    _UPLOAD_SEMAPHORE = asyncio.Semaphore(2)
    _DB_LOCK          = asyncio.Lock()

    conn = init_db()
    export_dir = os.path.join(tempfile.gettempdir(), "tele_backup_export")
    os.makedirs(export_dir, exist_ok=True)

    api_state["status"] = "Running"
    log(f"✅ Connected! Watching: {photos_path}")

    do_convert  = config.get("convert_heic", True)
    do_compress = config.get("compress_videos", True)
    do_cleanup  = config.get("free_up_space", False)

    while True:
        try:
            if not is_network_available():
                log("⏳ No network, waiting…")
                await asyncio.sleep(POLL_INTERVAL)
                continue

            # Reload config every cycle so settings changes take effect live
            try:
                cfg        = load_config()
                do_convert  = cfg.get("convert_heic", True)
                do_compress = cfg.get("compress_videos", True)
                do_cleanup  = cfg.get("free_up_space", False)
                photos_path = cfg.get("photos_path", photos_path)
                channel_id  = int(cfg.get("channel_id", channel_id))
            except Exception:
                pass

            media = scan_media(photos_path, SUPPORTED_EXT)
            now   = time.time()

            if not media:
                log(f"🔍 Scan: 0 new files found. Waiting {POLL_INTERVAL}s…")
            else:
                tasks = [
                    process_file(p, client, conn, channel_id, export_dir,
                                 do_convert, do_compress, do_cleanup, now)
                    for p in media
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                uploaded = sum(1 for r in results if r is True)
                if uploaded:
                    log(f"✅ Uploaded {uploaded} new file(s) this cycle.")

        except FloodWaitError as e:
            log(f"⏳ Telegram flood wait: {e.seconds}s")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            log(f"⚠️ Cycle error: {e}")

        await asyncio.sleep(POLL_INTERVAL)
