import os
import json
import socket
import subprocess
from datetime import datetime
from src.config import CONFIG_FILE

def format_size(b: int) -> str:
    if b >= 1024**3: return f"{b/1024**3:.2f} GB"
    if b >= 1024**2: return f"{b/1024**2:.1f} MB"
    if b >= 1024:    return f"{b/1024:.1f} KB"
    return f"{b} B"

def is_network_available(host="8.8.8.8", port=53, timeout=3) -> bool:
    try:
        socket.setdefaulttimeout(timeout)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((host, port))
        s.close()
        return True
    except OSError:
        return False

def send_notification(title: str, message: str):
    """Best-effort macOS notification."""
    try:
        script = f'display notification "{message}" with title "{title}" sound name "Glass"'
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except Exception:
        pass

def keep_photos_open():
    """Silently launch the Photos app in the background if it's not running, or poke it if it is."""
    try:
        result = subprocess.run(["pgrep", "-x", "Photos"], capture_output=True)
        if result.returncode != 0:
            # Not running, launch and hide
            script = '''
            tell application "Photos" to launch
            delay 1
            tell application "System Events"
                if exists process "Photos" then
                    set visible of process "Photos" to false
                end if
            end tell
            '''
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
        else:
            # Already running, just poke it silently to wake up iCloud Sync
            subprocess.run(["osascript", "-e", 'tell application "Photos" to get properties'], capture_output=True, timeout=2)
    except Exception:
        pass

def get_file_date(path: str) -> str:
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "Unknown"

def build_caption(filename: str, path: str, size: int, date_taken: str = None, tags: list = None) -> str:
    ext = os.path.splitext(filename)[1].upper().lstrip('.')
    display_date = date_taken or get_file_date(path)
    
    caption = (
        f"📁 {filename}\n"
        f"📅 Date: {display_date}\n"
        f"🏷 {ext}  •  {format_size(size)}"
    )
    
    if tags:
        caption += "\n\n" + " ".join([f"#{t}" for t in tags])
        
    return caption

def get_disk_free_percent() -> float:
    """Return free disk space as a percentage."""
    try:
        stat = os.statvfs('/')
        free = stat.f_bfree * stat.f_frsize
        total = stat.f_blocks * stat.f_frsize
        return (free / total) * 100
    except Exception:
        return 100.0  # Safe default if we can't check

def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        raise FileNotFoundError(f"Config not found: {CONFIG_FILE}")
    with open(CONFIG_FILE) as f:
        return json.load(f)
