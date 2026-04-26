import os

CONFIG_FILE = os.path.expanduser("~/.tele_backup_config.json")
DB_FILE = os.path.expanduser("~/.tele_backup_state.db")
import sys

CONFIG_FILE = os.path.expanduser("~/.tele_backup_config.json")
DB_FILE = os.path.expanduser("~/.tele_backup_state.db")
SESSION_FILE = os.path.expanduser("~/.tele_backup_session")

if sys.platform == "win32":
    LOG_DIR = os.path.expandvars("%APPDATA%\\TelegramPhotosBackup")
    # Common Windows iCloud Photos path
    PHOTOS_ORIGINALS = os.path.expandvars("%USERPROFILE%\\Pictures\\iCloud Photos\\Photos")
else:
    LOG_DIR = os.path.expanduser("~/Library/Logs/TelegramPhotosBackup")
    PHOTOS_ORIGINALS = os.path.expanduser("~/Pictures/Photos Library.photoslibrary/originals")

LOG_FILE = os.path.join(LOG_DIR, "backup.log")

POLL_INTERVAL = 5
MIN_FILE_AGE = 3
MAX_FILE_SIZE = 2000 * 1024 * 1024

IMAGE_EXT = ('.jpg', '.jpeg', '.png', '.heic', '.gif', '.raw', '.dng')
VIDEO_EXT = ('.mov', '.mp4', '.m4v')
SUPPORTED_EXT = IMAGE_EXT + VIDEO_EXT
