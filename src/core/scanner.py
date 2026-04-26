import os
import sys
import subprocess
import logging

logger = logging.getLogger("TelegramBackup")


def scan_media(photos_path: str, allowed_exts: tuple) -> list:
    """Return all supported media files from the given folder, oldest first."""
    if not os.path.exists(photos_path):
        logger.warning(f"Photos path not found: {photos_path}")
        return []

    files = []
    try:
        for root, dirs, fnames in os.walk(photos_path):
            # Skip hidden dirs
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for fname in fnames:
                if fname.startswith('.'):
                    continue
                if fname.lower().endswith(allowed_exts):
                    files.append(os.path.join(root, fname))
    except Exception as e:
        logger.error(f"scan_media error: {e}")
        return []

    files.sort(key=lambda x: os.path.getmtime(x))
    logger.info(f"scan_media: found {len(files)} files in {photos_path}")
    return files
