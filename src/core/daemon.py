import os
import time
import shutil
import asyncio
import sqlite3
import tempfile
import logging
import datetime
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError

from src.config import POLL_INTERVAL, MIN_FILE_AGE, MAX_FILE_SIZE, SESSION_FILE
from src.utils import (
    load_config, is_network_available, send_notification,
    keep_photos_open, build_caption, format_size, get_disk_free_percent
)
from src.core.db import init_db, is_uploaded, mark_uploaded, get_stats
from src.core.scanner import scan_media
from src.core.uploader import upload_file
from src.media.image_converter import convert_heic_to_jpg
from src.media.processor import get_exif_date, detect_faces, compress_video
from src.api.server import state as api_state

logger = logging.getLogger("TelegramBackup")

_UPLOAD_SEMAPHORE = None
_DB_LOCK = None

async def process_file(
    path: str, client, conn, channel_id, export_dir, 
    convert_heic, compress_videos, free_up_space, 
    prev_sizes, new_prev_sizes, now
) -> bool:
    filename = os.path.basename(path)
    ext = filename.lower()
    try:
        size = os.path.getsize(path)
        mtime = os.path.getmtime(path)
    except OSError: return False

    file_hash = f"{filename}_{size}"
    if is_uploaded(conn, file_hash): return False

    if size > MAX_FILE_SIZE:
        async with _DB_LOCK:
            if not is_uploaded(conn, file_hash):
                mark_uploaded(conn, file_hash, f"SKIPPED_2GB_{filename}", size)
        return False

    if now - mtime < MIN_FILE_AGE:
        new_prev_sizes[path] = size
        return False

    async with _DB_LOCK:
        if is_uploaded(conn, file_hash): return False
        mark_uploaded(conn, file_hash, f"IN_FLIGHT_{filename}", size)

    async with _UPLOAD_SEMAPHORE:
        api_state["status"] = f"Uploading {filename}"
        safe_name = f"{int(time.time() * 1000)}_{filename}"
        exported = os.path.join(export_dir, safe_name)
        final_export = None
        try:
            await asyncio.to_thread(shutil.copy2, path, exported)
            final_export = exported
            tags, date_taken = [], None
            if ext.endswith(('.jpg', '.jpeg', '.png', '.heic', '.dng')):
                date_taken = await asyncio.to_thread(get_exif_date, exported)
                if await asyncio.to_thread(detect_faces, exported): tags.append("People")
                if convert_heic and ext.endswith('.heic'):
                    final_export = await convert_heic_to_jpg(exported, export_dir)
            elif ext.endswith(('.mov', '.mp4', '.m4v')) and compress_videos:
                comp_path = os.path.join(export_dir, f"compressed_{safe_name}")
                final_export = await compress_video(exported, comp_path)

            caption = build_caption(filename, path, size, date_taken=date_taken, tags=tags)
            if await upload_file(client, channel_id, final_export, caption):
                async with _DB_LOCK: mark_uploaded(conn, file_hash, filename, size)
                if free_up_space and get_disk_free_percent() < 10.0:
                    try: os.remove(path)
                    except: pass
                return True
            else:
                async with _DB_LOCK:
                    conn.execute("DELETE FROM uploads WHERE uuid = ?", (file_hash,))
                    conn.commit()
                return False
        except Exception as e:
            logger.error(f"Error: {e}")
            async with _DB_LOCK:
                conn.execute("DELETE FROM uploads WHERE uuid = ?", (file_hash,))
                conn.commit()
            return False
        finally:
            for f in [exported, final_export]:
                if f and os.path.exists(f): os.remove(f)

async def run_daemon():
    logger.info("Daemon starting…")
    try:
        config = load_config()
        api_id = int(config["api_id"])
        api_hash = config["api_hash"]
        channel_id = int(config["channel_id"])
        photos_path = config.get("photos_path")
    except Exception as e:
        logger.error(f"Config error: {e}. Please check Settings.")
        return

    client = TelegramClient(SESSION_FILE, api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        logger.error("NOT AUTHORIZED! Please run interactive login.")
        return

    global _UPLOAD_SEMAPHORE, _DB_LOCK
    _UPLOAD_SEMAPHORE, _DB_LOCK = asyncio.Semaphore(2), asyncio.Lock()
    conn = init_db()
    export_dir = os.path.join(tempfile.gettempdir(), "tele_backup_export")
    os.makedirs(export_dir, exist_ok=True)
    
    prev_sizes, cycle_num = {}, 0
    while True:
        try:
            if not is_network_available():
                await asyncio.sleep(POLL_INTERVAL)
                continue
            
            # Refresh config every cycle
            current_config = load_config()
            do_convert = current_config.get("convert_heic", True)
            do_compress = current_config.get("compress_videos", True)
            do_cleanup = current_config.get("free_up_space", False)
            scan_path = current_config.get("photos_path") or photos_path

            from src.config import SUPPORTED_EXT
            import src.core.scanner
            # Override scanner path temporarily
            original_path = src.core.scanner.PHOTOS_ORIGINALS
            src.core.scanner.PHOTOS_ORIGINALS = scan_path
            
            media = src.core.scanner.scan_media(SUPPORTED_EXT)
            src.core.scanner.PHOTOS_ORIGINALS = original_path # Restore

            if not media:
                logger.info(f"No media found in {scan_path}")
            else:
                now = time.time()
                new_prev_sizes = {}
                results = await asyncio.gather(*[
                    process_file(p, client, conn, channel_id, export_dir, do_convert, do_compress, do_cleanup, prev_sizes, new_prev_sizes, now)
                    for p in media
                ])
                prev_sizes = new_prev_sizes

        except Exception as e: logger.error(f"Cycle error: {e}")
        await asyncio.sleep(POLL_INTERVAL)
