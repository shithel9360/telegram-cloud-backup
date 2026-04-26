import os

CONFIG_FILE = os.path.expanduser("~/.tele_backup_config.json")
DB_FILE = os.path.expanduser("~/.tele_backup_state.db")
import sys

CONFIG_FILE = os.path.expanduser("~/.tele_backup_config.json")
DB_FILE = os.path.expanduser("~/.tele_backup_state.db")
SESSION_FILE = os.path.expanduser("~/.tele_backup_session")

def get_icloud_path():
    """Attempt to auto-detect iCloud Photos path on Windows."""
    if sys.platform != "win32":
        return os.path.expanduser("~/Pictures/Photos Library.photoslibrary/originals")
    
    # Common locations for iCloud Photos on Windows
    candidates = [
        os.path.expandvars("%USERPROFILE%\\Pictures\\iCloud Photos\\Photos"),
        os.path.expandvars("%USERPROFILE%\\Pictures\\iCloud Photos"),
        os.path.expandvars("%USERPROFILE%\\iCloudPhotos\\Photos"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0] # Return default if none found

PHOTOS_ORIGINALS = get_icloud_path()
LOG_FILE = os.path.join(LOG_DIR, "backup.log")

POLL_INTERVAL = 5
MIN_FILE_AGE = 3
MAX_FILE_SIZE = 2000 * 1024 * 1024

IMAGE_EXT = ('.jpg', '.jpeg', '.png', '.heic', '.gif', '.raw', '.dng')
VIDEO_EXT = ('.mov', '.mp4', '.m4v')
SUPPORTED_EXT = IMAGE_EXT + VIDEO_EXT
