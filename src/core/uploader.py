import os
import asyncio
import logging

logger = logging.getLogger("TelegramBackup")


async def upload_file(client, chat_id: int, filepath: str,
                      caption: str, retries: int = 3) -> bool:
    """Upload a file to Telegram as a document (full quality, no compression)."""
    name = os.path.basename(filepath)
    for attempt in range(1, retries + 1):
        try:
            logger.info(f"Uploading {name} (attempt {attempt}/{retries})…")
            await client.send_file(
                chat_id,
                filepath,
                caption=caption,
                force_document=True,   # Send as FILE — no quality loss
                allow_cache=False,
                part_size_kb=512,
            )
            logger.info(f"✅ Uploaded: {name}")
            return True
        except Exception as e:
            logger.error(f"Upload error — {name} attempt {attempt}: {e}")
            if attempt < retries:
                await asyncio.sleep(5 * attempt)
    logger.error(f"Giving up on {name}")
    return False
