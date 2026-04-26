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

# Concurrency limit: initialized in run_daemon to ensure correct loop association
_UPLOAD_SEMAPHORE = None

# Lock for DB: initialized in run_daemon
_DB_LOCK = None

# How often to poke Photos app (every N cycles)
_PHOTOS_POKE_EVERY = 15

async def process_file(
    path: str,
    client,
    conn: sqlite3.Connection,
    channel_id: int,
    export_dir: str,
    convert_heic: bool,
    compress_videos: bool,
    free_up_space: bool,
    prev_sizes: dict,
    new_prev_sizes: dict,
    now: float,
) -> bool:
    """
    Process a single media file: validate, stabilize, convert, compress, tag, upload.
    """
    filename = os.path.basename(path)
    ext = filename.lower()

    try:
        size = os.path.getsize(path)
        mtime = os.path.getmtime(path)
    except OSError:
        return False

    file_hash = f"{filename}_{size}"

    if is_uploaded(conn, file_hash):
        return False

    if size > MAX_FILE_SIZE:
        async with _DB_LOCK:
            if not is_uploaded(conn, file_hash):
                mark_uploaded(conn, file_hash, f"SKIPPED_2GB_{filename}", size)
                logger.warning(f"Skipping {filename} — exceeds 2 GB limit")
        return False

    # Gate 1: Age
    age = now - mtime
    if age < MIN_FILE_AGE:
        new_prev_sizes[path] = size
        return False

    # Gate 2: Stability
    try:
        await asyncio.sleep(1.0)
        size_after = os.path.getsize(path)
        if size != size_after:
            new_prev_sizes[path] = size_after
            return False
    except OSError:
        return False

    async with _DB_LOCK:
        if is_uploaded(conn, file_hash):
            return False
        mark_uploaded(conn, file_hash, f"IN_FLIGHT_{filename}", size)

    async with _UPLOAD_SEMAPHORE:
        api_state["status"] = f"Processing {filename}"
        logger.info(f"Processing: {filename} ({format_size(size)})")
        
        safe_name = f"{int(time.time() * 1000)}_{filename}"
        exported = os.path.join(export_dir, safe_name)
        final_export = None

        try:
            await asyncio.to_thread(shutil.copy2, path, exported)
            final_export = exported
            
            tags = []
            date_taken = None

            # 1. Image Processing
            if ext.endswith(('.jpg', '.jpeg', '.png', '.heic', '.dng')):
                date_taken = await asyncio.to_thread(get_exif_date, exported)
                if await asyncio.to_thread(detect_faces, exported):
                    tags.append("People")
                
                if convert_heic and ext.endswith('.heic'):
                    final_export = await convert_heic_to_jpg(exported, export_dir)

            # 2. Video Processing
            elif ext.endswith(('.mov', '.mp4', '.m4v')) and compress_videos:
                comp_name = f"compressed_{safe_name}"
                if not comp_name.lower().endswith(('.mp4', '.mov')):
                    comp_name += ".mp4"
                comp_path = os.path.join(export_dir, comp_name)
                final_export = await compress_video(exported, comp_path)

            caption_filename = filename
            if final_export != exported:
                raw = os.path.basename(final_export)
                caption_filename = "_".join(raw.split("_")[1:]) if "_" in raw else raw

            caption = build_caption(caption_filename, path, size, date_taken=date_taken, tags=tags)
            ok = await upload_file(client, channel_id, final_export, caption)

            if ok:
                async with _DB_LOCK:
                    mark_uploaded(conn, file_hash, filename, size)
                logger.info(f"✅ Done: {filename}")
                
                # 3. Cleanup logic (Free up space)
                if free_up_space:
                    free_pct = get_disk_free_percent()
                    if free_pct < 10.0:
                        logger.info(f"Low disk space ({free_pct:.1f}%). Deleting local: {filename}")
                        try:
                            os.remove(path)
                        except OSError as e:
                            logger.warning(f"Could not delete {filename}: {e}")
                return True
            else:
                async with _DB_LOCK:
                    conn.execute("DELETE FROM uploads WHERE uuid = ?", (file_hash,))
                    conn.commit()
                return False

        except FloodWaitError as e:
            logger.warning(f"FloodWait: sleeping {e.seconds}s")
            await asyncio.sleep(e.seconds)
            async with _DB_LOCK:
                conn.execute("DELETE FROM uploads WHERE uuid = ?", (file_hash,))
                conn.commit()
            return False
        except Exception as e:
            logger.error(f"Error processing {filename}: {e}")
            async with _DB_LOCK:
                conn.execute("DELETE FROM uploads WHERE uuid = ?", (file_hash,))
                conn.commit()
            return False
        finally:
            for f in [exported, final_export]:
                if f and os.path.exists(f):
                    try:
                        os.remove(f)
                    except OSError:
                        pass

async def run_cycle(
    client,
    conn: sqlite3.Connection,
    channel_id: int,
    prev_sizes: dict,
    export_dir: str,
    cycle_num: int = 0,
    allowed_exts: tuple = None,
    convert_heic: bool = True,
    compress_videos: bool = True,
    free_up_space: bool = False,
) -> int:
    from src.config import SUPPORTED_EXT

    now = time.time()
    media = scan_media(allowed_exts or SUPPORTED_EXT)
    new_prev_sizes: dict = {}

    not_yet_uploaded = [
        p for p in media
        if not is_uploaded(conn, f"{os.path.basename(p)}_{os.path.getsize(p)}")
    ]

    if not_yet_uploaded:
        logger.info(f"Cycle #{cycle_num} — {len(media)} total, {len(not_yet_uploaded)} new")

    results = await asyncio.gather(
        *[
            process_file(
                path=p, client=client, conn=conn, channel_id=channel_id,
                export_dir=export_dir, convert_heic=convert_heic,
                compress_videos=compress_videos, free_up_space=free_up_space,
                prev_sizes=prev_sizes, new_prev_sizes=new_prev_sizes, now=now,
            )
            for p in media
        ],
        return_exceptions=False,
    )

    uploaded = sum(1 for r in results if r is True)
    prev_sizes.clear()
    prev_sizes.update(new_prev_sizes)

    if uploaded > 0 or not_yet_uploaded:
        logger.info(f"Cycle #{cycle_num} done — uploaded: {uploaded}")

    return uploaded

async def run_daemon():
    logger.info("Telegram Photos Backup Pro Daemon starting…")
    
    try:
        config = load_config()
        api_id = int(config["api_id"])
        api_hash = config.get("api_hash", "").strip()
        channel_id = config.get("channel_id", "").strip()
    except Exception as e:
        logger.error(f"Config error: {e}")
        return

    cid = str(channel_id).strip()
    if cid.lstrip("-").isdigit():
        cid_int = int(cid)
        if cid_int > 0: cid_int = int(f"-100{cid_int}")
    else:
        logger.error(f"Invalid channel_id: {channel_id}")
        return

    client = TelegramClient(SESSION_FILE, api_id, api_hash, auto_reconnect=True)
    await client.connect()

    if not await client.is_user_authorized():
        logger.error("Session not authorised.")
        await client.disconnect()
        return

    global _UPLOAD_SEMAPHORE, _DB_LOCK
    _UPLOAD_SEMAPHORE = asyncio.Semaphore(2)
    _DB_LOCK = asyncio.Lock()

    logger.info("✅ Connected to Telegram.")
    api_state["status"] = "Running"
    
    conn = init_db()
    try:
        c = conn.cursor()
        c.execute("DELETE FROM uploads WHERE filename LIKE 'IN_FLIGHT%'")
        conn.commit()
    except Exception: pass
    
    export_dir = os.path.join(tempfile.gettempdir(), "tele_backup_export")
    os.makedirs(export_dir, exist_ok=True)

    prev_sizes: dict = {}
    cycle_num = 0
    
    while True:
        try:
            if not is_network_available():
                api_state["status"] = "Offline"
                await asyncio.sleep(POLL_INTERVAL)
                continue
            
            if not client.is_connected():
                await client.connect()

            current_config = load_config()
            mode = current_config.get("upload_mode", "Both")
            do_convert = current_config.get("convert_heic", True)
            do_compress = current_config.get("compress_videos", True)
            do_cleanup = current_config.get("free_up_space", False)

            from src.config import IMAGE_EXT, VIDEO_EXT, SUPPORTED_EXT
            if mode == "Photos Only": exts = IMAGE_EXT
            elif mode == "Videos Only": exts = VIDEO_EXT
            else: exts = SUPPORTED_EXT

            cycle_num += 1
            await run_cycle(
                client, conn, cid_int, prev_sizes, export_dir, cycle_num,
                allowed_exts=exts, convert_heic=do_convert,
                compress_videos=do_compress, free_up_space=do_cleanup
            )

        except Exception as e:
            logger.exception(f"Cycle error: {e}")
            await asyncio.sleep(10)

        await asyncio.sleep(POLL_INTERVAL)
