import os
import subprocess
import logging
from src.config import PHOTOS_ORIGINALS, SUPPORTED_EXT

logger = logging.getLogger("TelegramBackup")

def scan_media(allowed_exts: tuple = SUPPORTED_EXT) -> list:
    """Return all supported media files, sorted oldest-first.
    
    Uses 'find' via subprocess to bypass macOS TCC/sandbox restrictions
    that prevent long-running daemon processes from walking the Photos Library
    via Python's os.walk(). The subprocess inherits FDA from the shell.
    """
    if not os.path.exists(PHOTOS_ORIGINALS):
        logger.error(f"originals dir not found: {PHOTOS_ORIGINALS}")
        return []

    ext_args = []
    for i, ext in enumerate(allowed_exts):
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
