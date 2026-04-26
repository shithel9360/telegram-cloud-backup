import os
import asyncio
import logging

logger = logging.getLogger("TelegramBackup")

async def convert_heic_to_jpg(heic_path: str, export_dir: str) -> str:
    """
    Converts a HEIC image to JPG asynchronously using macOS 'sips'.
    Returns the path to the newly created JPG file, or the original
    if conversion fails.
    """
    if not heic_path.lower().endswith('.heic'):
        return heic_path

    filename = os.path.basename(heic_path)
    base_name = os.path.splitext(filename)[0]
    jpg_filename = f"{base_name}.jpg"
    jpg_path = os.path.join(export_dir, jpg_filename)

    cmd = [
        "sips",
        "-s", "format", "jpeg",
        heic_path,
        "--out", jpg_path
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        except asyncio.TimeoutError:
            proc.kill()
            logger.error(f"HEIC conversion timed out for {filename}")
            return heic_path

        if proc.returncode == 0 and os.path.exists(jpg_path):
            logger.info(f"Converted HEIC to JPG: {jpg_filename}")
            return jpg_path
        else:
            logger.error(f"HEIC conversion failed for {filename}: {stderr.decode()[:200]}")
    except Exception as e:
        logger.error(f"HEIC conversion error for {filename}: {e}")

    return heic_path
