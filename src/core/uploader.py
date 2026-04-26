import os
import asyncio
import logging

logger = logging.getLogger("TelegramBackup")

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
