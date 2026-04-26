import sys
import os
import asyncio
import logging

# Ensure src is in python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.config import LOG_FILE
from src.core.daemon import run_daemon
from src.utils import send_notification

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("TelegramBackup")

if __name__ == "__main__":
    try:
        asyncio.run(run_daemon())
    except KeyboardInterrupt:
        logger.info("Daemon stopped by user.")
    except Exception as e:
        logger.exception(f"Fatal daemon error: {e}")
        send_notification("❌ Backup Daemon Crashed", str(e)[:100])
