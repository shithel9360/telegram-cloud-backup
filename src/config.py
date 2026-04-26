import os
import sys

# --- Paths (cross-platform) ---
CONFIG_FILE  = os.path.expanduser("~/.tele_backup_config.json")
DB_FILE      = os.path.expanduser("~/.tele_backup_state.db")
SESSION_FILE = os.path.expanduser("~/.tele_backup_session")

if sys.platform == "win32":
    LOG_DIR = os.path.expandvars(r"%APPDATA%\TelegramPhotosBackup")
else:
    LOG_DIR = os.path.expanduser("~/Library/Logs/TelegramPhotosBackup")

os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "backup.log")

# --- Timing ---
POLL_INTERVAL = 10   # seconds between scan cycles
MIN_FILE_AGE  = 3    # seconds a file must be "stable" before upload
MAX_FILE_SIZE = 2000 * 1024 * 1024   # 2 GB hard cap

# --- Supported extensions ---
IMAGE_EXT     = ('.jpg', '.jpeg', '.png', '.heic', '.gif', '.raw', '.dng', '.bmp', '.webp')
VIDEO_EXT     = ('.mov', '.mp4', '.m4v', '.avi', '.mkv', '.wmv')
SUPPORTED_EXT = IMAGE_EXT + VIDEO_EXT

# --- Embedded App API (for simplified onboarding) ---
TELEGRAM_API_ID   = 36355055
TELEGRAM_API_HASH = "9b819327f0403ce37b08e316a8464cb6"

def get_icloud_path() -> str:
    """Auto-detect iCloud Photos folder on Windows and macOS."""
    if sys.platform == "win32":
        candidates = [
            os.path.expandvars(r"%USERPROFILE%\Pictures\iCloud Photos\Photos"),
            os.path.expandvars(r"%USERPROFILE%\Pictures\iCloud Photos"),
            os.path.expandvars(r"%USERPROFILE%\iCloudPhotos\Photos"),
            os.path.expandvars(r"%USERPROFILE%\Pictures"),
        ]
        for c in candidates:
            if os.path.exists(c):
                return c
        return candidates[0]
    else:
        return os.path.expanduser("~/Pictures/Photos Library.photoslibrary/originals")

# Default photos path
PHOTOS_ORIGINALS = get_icloud_path()
