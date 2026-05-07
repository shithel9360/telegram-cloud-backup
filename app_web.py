"""
Telegram Backup Pro — Web Edition (v3.1.0)
Single-file web app. Run with: python app_web.py
Then open: http://localhost:7878

NEW IN v3.1.0:
- FIX 1: Full Telegram 2FA (Two-Step Verification) support in login flow
- FIX 2: iCloud placeholder detection via Windows file attributes (no corrupt uploads)
- FIX 3: CSRF token on all POST endpoints (security hardening)
- FIX 4: Log file rotation (RotatingFileHandler, max 5MB x 3 = 15MB total)
- FIX 5: Temp upload files auto-cleaned on startup (prevents disk bloat)
- FIX 6: LRU hash cache (max 10,000 entries) prevents unbounded RAM growth
- FIX 7: Windows toast notifications + browser notifications on backup complete

NEW IN v3.0.0:
- BUG 1-10 FIX: See v3.0.0 changelog
- NEW: Upload retry with exponential backoff (3 attempts: 5s, 15s, 45s)
- NEW: Real per-file upload progress via Telethon progress_callback
- NEW: Failed uploads table + retry UI in dashboard

NEW IN v2.2.x:
- Instant delete after backup, path validation, watchdog bundled
"""

import os, sys, json, time, asyncio, threading, logging, shutil, tempfile, sqlite3, hashlib, secrets
from pathlib import Path
from datetime import datetime
from collections import deque, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ── Config ────────────────────────────────────────────────────────────
CONFIG_FILE  = Path.home() / ".tele_backup_config.json"
DB_FILE      = Path.home() / ".tele_backup_state.db"
SESSION_FILE = str(Path.home() / ".tele_backup_session")
APPDATA      = Path(os.environ.get("APPDATA", Path.home())) / "TelegramBackupPro"
APPDATA.mkdir(parents=True, exist_ok=True)
LOG_FILE     = APPDATA / "backup.log"
EXPORT_DIR   = APPDATA / "upload_temp"          # v3.1.0: stable temp dir
EXPORT_DIR.mkdir(parents=True, exist_ok=True)
PORT         = 7878

TELEGRAM_API_ID   = 36355055
TELEGRAM_API_HASH = "9b819327f0403ce37b08e316a8464cb6"

APP_VERSION  = "3.1.1"

# Improvement 1: Upload retry settings
MAX_RETRIES   = 3
RETRY_DELAYS  = [5, 15, 45]   # seconds between attempts

# v3.1.0 constants
MAX_LOG_LINES  = 200
MIN_MEDIA_SIZE = 10 * 1024   # 10 KB — below this an image/video is likely a stub
GITHUB_REPO  = "shithel9360/telegram-cloud-backup"
RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

update_state = {"available": False, "latest": APP_VERSION, "url": ""}

IMAGE_EXT = ('.jpg','.jpeg','.png','.heic','.gif','.raw','.dng','.bmp','.webp')
VIDEO_EXT = ('.mov','.mp4','.m4v','.avi','.mkv')
ALL_EXT   = IMAGE_EXT + VIDEO_EXT

# Size limits for security
MAX_BODY_SIZE = 1024 * 1024  # 1MB
MAX_PHONE_LEN = 15
MIN_CLEANUP_DAYS = 1
MAX_CLEANUP_DAYS = 365

try:
    _rotating_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
except (PermissionError, OSError):
    # Log file locked by another instance (Windows) — fall back to stream only
    _rotating_handler = logging.StreamHandler()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[_rotating_handler, logging.StreamHandler()]
)
logger = logging.getLogger("BackupPro")

# v3.1.0: CSRF token for POST endpoint protection
_csrf_token = secrets.token_hex(16)

# v3.1.0: Clean up leftover temp files from crashed sessions
def cleanup_temp_on_startup():
    try:
        files_cleaned = bytes_cleaned = 0
        for f in EXPORT_DIR.iterdir():
            if f.is_file():
                try:
                    bytes_cleaned += f.stat().st_size
                    f.unlink()
                    files_cleaned += 1
                except Exception:
                    pass
        if files_cleaned > 0:
            logging.getLogger("BackupPro").info(
                f"Startup cleanup: removed {files_cleaned} temp files"
            )
    except Exception as e:
        logging.getLogger("BackupPro").warning(f"Startup cleanup failed: {e}")

cleanup_temp_on_startup()

# ── Input Validation (NEW - Security Fix) ────────────────────────────
def validate_phone(phone: str) -> bool:
    """Validate phone number format"""
    if not phone or len(phone) > MAX_PHONE_LEN:
        return False
    if not phone.startswith("+"):
        return False
    if not phone[1:].replace(" ", "").isdigit():
        return False
    return True

def validate_channel_id(channel_id: str) -> bool:
    """Validate channel ID is numeric"""
    try:
        cid = int(channel_id)
        return cid != 0
    except (ValueError, TypeError):
        return False

def validate_cleanup_days(days: int) -> bool:
    """Validate cleanup days in valid range"""
    try:
        d = int(days)
        return MIN_CLEANUP_DAYS <= d <= MAX_CLEANUP_DAYS
    except (ValueError, TypeError):
        return False

def validate_path(user_path: str) -> bool:
    """Validate path is safe to use (Bug 1 Fix: permissive for iCloud/custom paths)"""
    if not user_path:
        return False
    if len(user_path) > 520:  # Allow long Windows paths
        return False
    if '\x00' in user_path:
        return False
    # Block actual traversal attacks
    parts = Path(user_path).parts
    if '..' in parts:
        return False
    try:
        resolved = Path(user_path).resolve()
    except (OSError, ValueError):
        return False
    resolved_str = str(resolved).lower()
    # Block known system directories on Windows
    _blocked = [
        'windows\\system32', 'windows\\syswow64', 'windows\\winsxs',
        'program files', 'programdata', '\\windows\\',
    ]
    for b in _blocked:
        if b in resolved_str:
            return False
    # Accept any path under home directory
    try:
        home_str = str(Path.home().resolve()).lower()
        if resolved_str.startswith(home_str):
            return True
    except Exception:
        pass
    # Accept any absolute path on a drive root that is not a system dir
    # e.g. C:\Users\..., D:\..., /mnt/...
    if Path(user_path).is_absolute():
        return True
    return False

# ── Config Cache (fix #4) ─────────────────────────────────────────────
_config_cache = None
_config_lock = threading.Lock()

def load_config() -> dict:
    """Load config with error recovery (Security Fix #6)"""
    global _config_cache
    with _config_lock:
        if _config_cache is None:
            if CONFIG_FILE.exists():
                try:
                    _config_cache = json.loads(CONFIG_FILE.read_text())
                except json.JSONDecodeError:
                    logger.error("Config file corrupted, using defaults")
                    _config_cache = {}
                except Exception as e:
                    logger.error(f"Failed to load config: {e}")
                    _config_cache = {}
            else:
                _config_cache = {}
    return _config_cache.copy()

def save_config(cfg: dict):
    global _config_cache
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
        with _config_lock:
            _config_cache = cfg.copy()
    except Exception as e:
        logger.error(f"Failed to save config: {e}")

# ── Global DB lock for thread safety (Bug 7 Fix) ──────────────────────
_db_lock = threading.Lock()

# ── Database Connection Pool ────────────────────────────────────────────
class DBConnectionPool:
    def __init__(self, db_path, pool_size=5):
        self.db_path = db_path
        self.pool = []
        self.lock = threading.Lock()
        for _ in range(pool_size):
            try:
                conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
                conn.execute("PRAGMA journal_mode=WAL")
                self.pool.append(conn)
            except Exception as e:
                logger.error(f"Failed to create database connection: {e}")
        self._init_db()
    
    def _init_db(self):
        try:
            conn = self.pool[0]
            conn.execute("""CREATE TABLE IF NOT EXISTS uploads (
                uuid TEXT PRIMARY KEY, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                filename TEXT, file_size INTEGER DEFAULT 0, local_path TEXT)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS failed_uploads (
                local_path TEXT PRIMARY KEY, filename TEXT, fail_reason TEXT,
                fail_count INTEGER DEFAULT 1,
                last_attempt DATETIME DEFAULT CURRENT_TIMESTAMP)""")
            conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Database initialization failed: {e}")
    
    def get_connection(self):
        with self.lock:
            if self.pool:
                return self.pool.pop()
        try:
            return sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30)
        except sqlite3.Error as e:
            logger.error(f"Cannot create database connection: {e}")
            return None
    
    def return_connection(self, conn):
        if conn is None:
            return
        with self.lock:
            if len(self.pool) < 5:
                self.pool.append(conn)
            else:
                try:
                    conn.close()
                except Exception:
                    pass
    
    def close_all(self):
        with self.lock:
            for conn in self.pool:
                try:
                    conn.close()
                except Exception:
                    pass
            self.pool.clear()

db_pool = DBConnectionPool(str(DB_FILE))

# ── Startup helpers ────────────────────────────────────────────────────
def set_windows_startup(enabled: bool):
    if sys.platform != "win32": return
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        app_name = "TelegramBackupPro"
        exe_path = sys.executable
        if not getattr(sys, 'frozen', False): return
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
        if enabled:
            winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, f'"{exe_path}" --silent')
        else:
            try: winreg.DeleteValue(key, app_name)
            except FileNotFoundError: pass
        winreg.CloseKey(key)
    except Exception as e:
        logger.error(f"Failed to set startup: {e}")

# ── Shared state (fix #6) ──────────────────────────────────────────────
state = {
    "status": "idle",
    "logs":   deque(maxlen=MAX_LOG_LINES),
    "count":  0,
    "size_str": "0 B",
    "authorized": False,
    "cleanup_enabled": False,
    "cleanup_count": 0,
    "delete_after_backup_enabled": False,
    "deleted_count": 0,
    "skipped_placeholders": 0,   # v3.1.0
}

def push_log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    state["logs"].append(line)
    logger.info(msg)

# ── DB helpers (fix #7) ────────────────────────────────────────────────
def is_uploaded(conn, fhash):
    if conn is None:
        return False
    try:
        with _db_lock:
            return bool(conn.execute("SELECT 1 FROM uploads WHERE uuid=?", (fhash,)).fetchone())
    except sqlite3.Error as e:
        logger.error(f"DB query failed: {e}")
        return False

def are_uploaded_batch(conn, fhashes):
    """Batch check with _db_lock (Bug 7 Fix)"""
    if conn is None or not fhashes:
        return set()
    try:
        placeholders = ','.join(['?'] * len(fhashes))
        query = f"SELECT uuid FROM uploads WHERE uuid IN ({placeholders})"
        with _db_lock:
            results = conn.execute(query, list(fhashes)).fetchall()
        return {row[0] for row in results}
    except sqlite3.Error as e:
        logger.error(f"Batch query failed: {e}")
        return set()

def mark_uploaded(conn, fhash, fname, size, local_path=""):
    if conn is None:
        return
    try:
        with _db_lock:
            conn.execute("REPLACE INTO uploads VALUES(?,CURRENT_TIMESTAMP,?,?,?)",
                        (fhash, fname, size, local_path))
            conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Failed to mark uploaded: {e}")
        try:
            with _db_lock:
                conn.rollback()
        except:
            pass

def get_uploaded_files(conn):
    """Get all successfully uploaded files with their local paths"""
    if conn is None:
        return []
    try:
        with _db_lock:
            results = conn.execute(
                "SELECT uuid, filename, file_size, local_path FROM uploads WHERE filename NOT LIKE 'IN_FLIGHT%' AND filename NOT LIKE 'SKIPPED%'"
            ).fetchall()
        return results
    except sqlite3.Error as e:
        logger.error(f"Failed to get uploaded files: {e}")
        return []

def is_file_in_db(conn, local_path):
    """Check if a file is successfully recorded in the database"""
    if conn is None:
        return False
    try:
        with _db_lock:
            result = conn.execute(
                "SELECT 1 FROM uploads WHERE local_path=? AND filename NOT LIKE 'IN_FLIGHT_%' AND filename NOT LIKE 'SKIPPED_%'",
                (local_path,)
            ).fetchone()
        return bool(result)
    except sqlite3.Error as e:
        logger.error(f"DB check failed: {e}")
        return False

def is_hash_uploaded_and_confirmed(conn, fhash):
    """Bug 1 Fix: check by hash that file was fully uploaded (not IN_FLIGHT/SKIPPED)"""
    if conn is None:
        return False
    try:
        with _db_lock:
            result = conn.execute(
                "SELECT 1 FROM uploads WHERE uuid=? AND filename NOT LIKE 'IN_FLIGHT_%' AND filename NOT LIKE 'SKIPPED_%'",
                (fhash,)
            ).fetchone()
        return bool(result)
    except sqlite3.Error as e:
        logger.error(f"Hash confirm check failed: {e}")
        return False

def clean_inflight_entries(conn):
    """Bug 2A Fix: remove stuck IN_FLIGHT rows from a previous crash"""
    if conn is None:
        return
    try:
        with _db_lock:
            conn.execute("DELETE FROM uploads WHERE filename LIKE 'IN_FLIGHT_%'")
            conn.commit()
        push_log("🧹 Cleaned up any incomplete upload entries.")
    except sqlite3.Error as e:
        logger.error(f"Failed to clean IN_FLIGHT entries: {e}")

def mark_failed_upload(conn, local_path, filename, reason):
    """Improvement 1: record a persistently-failing file"""
    if conn is None:
        return
    try:
        with _db_lock:
            conn.execute("""
                INSERT INTO failed_uploads(local_path,filename,fail_reason,fail_count,last_attempt)
                VALUES(?,?,?,1,CURRENT_TIMESTAMP)
                ON CONFLICT(local_path) DO UPDATE SET
                    fail_count=fail_count+1, fail_reason=excluded.fail_reason,
                    last_attempt=CURRENT_TIMESTAMP
            """, (local_path, filename, str(reason)))
            conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Failed to mark failed upload: {e}")

def get_failed_uploads(conn):
    """Improvement 1: list of persistently failed files"""
    if conn is None:
        return []
    try:
        with _db_lock:
            return conn.execute(
                "SELECT local_path,filename,fail_reason,fail_count,last_attempt FROM failed_uploads ORDER BY last_attempt DESC"
            ).fetchall()
    except sqlite3.Error as e:
        logger.error(f"Failed to get failed uploads: {e}")
        return []

def remove_failed_upload(conn, local_path):
    """Improvement 1: clear a file from failed list (retry)"""
    if conn is None:
        return
    try:
        with _db_lock:
            conn.execute("DELETE FROM failed_uploads WHERE local_path=?", (local_path,))
            conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Failed to remove failed upload: {e}")

def get_stats(conn):
    if conn is None:
        return 0, 0
    try:
        with _db_lock:
            r = conn.execute("SELECT COUNT(*),SUM(file_size) FROM uploads WHERE filename NOT LIKE 'SKIPPED%'").fetchone()
        return r[0] or 0, r[1] or 0
    except sqlite3.Error as e:
        logger.error(f"Failed to get stats: {e}")
        return 0, 0

# ── Config helpers ────────────────────────────────────────────────────
def fmt_size(b):
    if b>=(1<<30): return f"{b/(1<<30):.2f} GB"
    if b>=(1<<20): return f"{b/(1<<20):.1f} MB"
    if b>=(1<<10): return f"{b/(1<<10):.1f} KB"
    return f"{b} B"

def detect_icloud():
    """Bug 1 Fix: Search all known iCloud locations on Windows + macOS"""
    candidates = [
        Path.home() / "iCloudDrive",
        Path.home() / "Apple" / "Mobile Documents" / "com~apple~CloudDocs",
        Path.home() / "Pictures" / "iCloud Photos" / "Photos",
        Path.home() / "Pictures" / "iCloud Photos",
        Path.home() / "Pictures",
    ]
    # Try Windows-specific path via os.getlogin()
    try:
        win_path = Path("C:/Users") / os.getlogin() / "iCloudDrive"
        if win_path not in candidates:
            candidates.insert(2, win_path)
    except Exception:
        pass
    for c in candidates:
        try:
            if c.exists():
                return str(c)
        except Exception:
            continue
    return str(Path.home() / "Pictures")


# ── LRU Hash Cache (v3.1.0 Fix 6: bounded RAM) ────────────────────────────
class LRUHashCache:
    def __init__(self, maxsize=10000):
        self.cache   = OrderedDict()
        self.maxsize = maxsize
        self.lock    = threading.Lock()

    def get(self, key):
        with self.lock:
            if key not in self.cache:
                return None
            self.cache.move_to_end(key)
            return self.cache[key]

    def set(self, key, value):
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
            self.cache[key] = value
            if len(self.cache) > self.maxsize:
                self.cache.popitem(last=False)

    def invalidate(self, key):
        with self.lock:
            self.cache.pop(key, None)

    def clear(self):
        with self.lock:
            self.cache.clear()

_hash_cache = LRUHashCache(maxsize=10000)


def compute_file_hash(filepath, use_cache=True):
    """MD5 hash with LRU mtime-aware cache; skips iCloud placeholders."""
    fp_str = str(filepath)
    # Skip .icloud placeholder stubs
    if fp_str.lower().endswith('.icloud'):
        return None
    try:
        stat_info     = os.stat(fp_str)
        file_size     = stat_info.st_size
        current_mtime = stat_info.st_mtime
        if os.path.getsize(fp_str) != file_size:
            return None  # file being written
        ext = os.path.splitext(fp_str)[1].lower()
        if file_size < 1024 and ext in IMAGE_EXT + VIDEO_EXT:
            return None  # tiny stub
    except OSError:
        return None

    if use_cache:
        cached = _hash_cache.get(fp_str)
        if cached is not None:
            stored_mtime, stored_hash = cached
            if abs(current_mtime - stored_mtime) < 1.0:
                return stored_hash

    try:
        hash_obj = hashlib.md5()
        with open(fp_str, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):  # 64KB chunks
                hash_obj.update(chunk)
        result = hash_obj.hexdigest()
        if use_cache:
            _hash_cache.set(fp_str, (current_mtime, result))
        return result
    except (OSError, IOError) as e:
        logger.error(f"Failed to hash file: {e}")
        return None


# ── iCloud placeholder detection (v3.1.0 Fix 2) ──────────────────────────
def is_icloud_placeholder(filepath: str) -> bool:
    """Return True if the file is an iCloud online-only placeholder (Windows)."""
    if sys.platform != 'win32':
        return False
    try:
        import ctypes
        attrs = ctypes.windll.kernel32.GetFileAttributesW(filepath)
        if attrs == 0xFFFFFFFF:  # INVALID_FILE_ATTRIBUTES
            return False
        OFFLINE               = 0x1000
        RECALL_ON_DATA_ACCESS = 0x00400000
        RECALL_ON_OPEN        = 0x00040000
        return bool(attrs & (OFFLINE | RECALL_ON_DATA_ACCESS | RECALL_ON_OPEN))
    except Exception:
        return False


# ── Windows toast notifications (v3.1.0 Fix 7) ─────────────────────────
def send_windows_notification(title: str, message: str):
    """Send Windows 10/11 toast notification via PowerShell (silent on failure)."""
    if sys.platform != 'win32':
        return
    try:
        import subprocess
        ps = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text>{title}</text><text>{message}</text></binding></visual></toast>')
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Telegram Backup Pro')
$notifier.Show($toast)
"""
        subprocess.run(
            ["powershell", "-WindowStyle", "Hidden", "-Command", ps],
            capture_output=True, timeout=5
        )
    except Exception as e:
        logger.debug(f"Notification failed: {e}")


# ── Temp disk usage check ─────────────────────────────────────────────
def check_temp_disk_usage():
    """Warn if upload temp dir exceeds 500 MB."""
    try:
        if EXPORT_DIR.exists():
            total = sum(f.stat().st_size for f in EXPORT_DIR.iterdir() if f.is_file())
            if total > 500 * 1024 * 1024:
                push_log(f"\u26a0\ufe0f Temp directory is large: {fmt_size(total)}. Possible stuck uploads.")
    except Exception:
        pass

# ── File system watcher (fix #1) ───────────────────────────────────────
pending_files      = set()
pending_files_lock = threading.Lock()

# Bug 2C Fix: prevent duplicate concurrent uploads
_uploading_now      = set()
_uploading_lock     = threading.Lock()

# Improvement 2: per-file upload progress
_upload_progress      = {}  # {fname: pct_int}
_upload_progress_lock = threading.Lock()

class FileSystemWatcher(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory and event.src_path.lower().endswith(ALL_EXT):
            with pending_files_lock:
                pending_files.add(event.src_path)
    
    def on_modified(self, event):
        if not event.is_directory and event.src_path.lower().endswith(ALL_EXT):
            with pending_files_lock:
                pending_files.add(event.src_path)

observer = None

def start_file_watcher(path):
    global observer
    try:
        observer = Observer()
        observer.schedule(FileSystemWatcher(), path, recursive=True)
        observer.start()
        push_log(f"✅ File watcher started for {path}")
    except Exception as e:
        push_log(f"⚠️ File watcher failed: {e}")

def stop_file_watcher():
    global observer
    if observer:
        try:
            observer.stop()
            observer.join()
        except Exception as e:
            logger.error(f"Error stopping watcher: {e}")

# ── Instant Delete After Backup ──────────────────────────────────────────
def try_delete_file_after_backup(conn, local_path: str, backup_folder: str, fhash: str = "") -> bool:
    """
    Safely delete a file after successful backup.
    Bug 1/6 Fix: falls back to hash-based DB check if path-based check fails.
    Returns True if deletion succeeded, False otherwise.
    """
    if not local_path or not os.path.exists(local_path):
        return False

    try:
        # Check 1: File confirmed in DB (by path OR by hash fallback)
        confirmed = is_file_in_db(conn, local_path)
        if not confirmed and fhash:
            confirmed = is_hash_uploaded_and_confirmed(conn, fhash)
            if confirmed:
                logger.info(f"[DELETE] Confirmed by hash fallback: {local_path}")
        if not confirmed:
            logger.warning(f"[DELETE] File not confirmed in DB: {local_path}")
            return False

        # Check 2: Extension must be supported
        file_ext = os.path.splitext(local_path)[1].lower()
        if file_ext not in ALL_EXT:
            logger.warning(f"[DELETE] Unsupported extension: {local_path}")
            return False

        # Check 3: Path must be within backup folder (path traversal guard)
        local_resolved  = Path(local_path).resolve()
        backup_resolved = Path(backup_folder).resolve()
        if not str(local_resolved).startswith(str(backup_resolved)):
            logger.error(f"[DELETE] Path traversal blocked: {local_path}")
            return False

        # Check 4: File still exists
        if not os.path.exists(local_path):
            logger.info(f"[DELETE] Already deleted: {local_path}")
            return False

        os.remove(local_path)
        push_log(f"\U0001f5d1\ufe0f  Deleted from iCloud: {os.path.basename(local_path)}")
        state["deleted_count"] += 1
        logger.info(f"[DELETE] Deleted: {local_path}")
        return True

    except PermissionError:
        push_log(f"\u26a0\ufe0f  Permission denied: {os.path.basename(local_path)}")
        return False
    except FileNotFoundError:
        return False
    except Exception as e:
        logger.error(f"[DELETE] Error: {e}")
        return False

# ── Storage cleanup (Bug 9 Fix: boundary check) ──────────────────────────
def cleanup_icloud_storage(conn, cleanup_older_than_days=30, backup_folder=""):
    """Clean up backed-up files; only deletes within current backup_folder."""
    if conn is None:
        return 0, 0
    try:
        uploaded_files = get_uploaded_files(conn)
        cutoff_time    = time.time() - (cleanup_older_than_days * 86400)
        deleted_count  = 0
        deleted_size   = 0
        failed_files   = []

        for uuid, filename, file_size, local_path in uploaded_files:
            if not local_path:
                continue

            # Bug 9 Fix: skip files outside the current backup folder
            if backup_folder:
                try:
                    local_resolved  = Path(local_path).resolve()
                    backup_resolved = Path(backup_folder).resolve()
                    if not str(local_resolved).startswith(str(backup_resolved)):
                        push_log(f"\u23ed\ufe0f Skipping (outside backup folder): {filename}")
                        continue
                except Exception:
                    continue

            try:
                file_path = Path(local_path)
                if not file_path.exists():
                    push_log(f"\u23ed\ufe0f  Skipped (not found): {filename}")
                    continue
                if file_path.stat().st_mtime > cutoff_time:
                    push_log(f"\u23ed\ufe0f  Too new to delete: {filename}")
                    continue
                file_path.unlink()
                deleted_count += 1
                deleted_size  += file_size or 0
                push_log(f"\U0001f5d1\ufe0f  Deleted backed-up file: {filename}")
            except PermissionError:
                failed_files.append((filename, "Permission denied"))
                push_log(f"\u26a0\ufe0f  Permission denied: {filename}")
            except FileNotFoundError:
                push_log(f"\u23ed\ufe0f  Already deleted: {filename}")
            except Exception as e:
                failed_files.append((filename, str(e)))
                push_log(f"\u26a0\ufe0f  Failed to delete {filename}: {e}")

        if deleted_count > 0:
            push_log(f"\u2705 Cleanup complete: {deleted_count} files deleted ({fmt_size(deleted_size)})")
            state["cleanup_count"] += deleted_count
        if failed_files:
            push_log(f"\u26a0\ufe0f  {len(failed_files)} files could not be deleted")
        return deleted_count, deleted_size
    except Exception as e:
        push_log(f"\u274c Cleanup failed: {e}")
        return 0, 0

# ── Self-updater (Bug 3 Fix) ───────────────────────────────────────────
def do_self_update(download_url: str) -> bool:
    """Download new .exe and replace current one via batch script (Windows only)."""
    try:
        import urllib.request
        push_log("\u2b07\ufe0f Downloading update...")
        tmp_path = Path(tempfile.gettempdir()) / "TelegramBackup_new.exe"

        def _progress_hook(count, block_size, total):
            if total > 0:
                pct = min(int(count * block_size * 100 / total), 100)
                push_log(f"\u2b07\ufe0f Downloading... {pct}%")

        urllib.request.urlretrieve(download_url, str(tmp_path), reporthook=_progress_hook)
        push_log("\u2705 Download complete. Applying update...")

        if not getattr(sys, 'frozen', False):
            push_log("\u26a0\ufe0f Auto-update only works in the packaged .exe. Please update manually.")
            return False

        current_exe = Path(sys.executable)
        backup_exe  = current_exe.with_suffix('.exe.bak')
        bat_path    = Path(tempfile.gettempdir()) / "tele_backup_update.bat"
        bat_content = f"""@echo off
:wait
tasklist /fi "PID eq {os.getpid()}" 2>nul | find /i "TelegramBackup" >nul
if not errorlevel 1 (
    timeout /t 1 /nobreak >nul
    goto wait
)
move /y "{current_exe}" "{backup_exe}"
move /y "{tmp_path}" "{current_exe}"
start "" "{current_exe}"
timeout /t 3 /nobreak >nul
del "{backup_exe}" 2>nul
del "%~f0"
"""
        bat_path.write_text(bat_content)
        import subprocess
        subprocess.Popen(
            ['cmd', '/c', str(bat_path)],
            creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS
        )
        push_log("\U0001f504 Restarting with new version...")
        time.sleep(1)
        os._exit(0)
        return True
    except Exception as e:
        push_log(f"\u274c Update failed: {e}")
        return False

# ── Auto-updater check ──────────────────────────────────────────────
def check_for_update():
    """Check GitHub for a newer release version"""
    try:
        import urllib.request
        req = urllib.request.Request(
            RELEASES_URL,
            headers={"User-Agent": f"TelegramBackupPro/{APP_VERSION}"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        latest_tag  = data.get("tag_name", APP_VERSION).lstrip("v")
        assets      = data.get("assets", [])
        exe_asset   = next((a for a in assets if a["name"].endswith(".exe")), None)
        dl_url      = exe_asset["browser_download_url"] if exe_asset else data.get("html_url", "")

        def _ver_tuple(v):
            try: return tuple(int(x) for x in v.replace("-",".",1).split(".")[:4])
            except: return (0,)

        if _ver_tuple(latest_tag) > _ver_tuple(APP_VERSION):
            update_state["available"] = True
            update_state["latest"]    = latest_tag
            update_state["url"]       = dl_url
            push_log(f"🆕 Update available: v{latest_tag} — check the dashboard!")
    except Exception:
        pass

def _update_check_loop():
    while True:
        check_for_update()
        time.sleep(1800)

# ── Thread pool ────────────────────────────────────────────────────────
thread_pool = ThreadPoolExecutor(max_workers=3)

# ── Backup daemon ──────────────────────────────────────────────────────
_daemon_loop: asyncio.AbstractEventLoop = None
_daemon_task = None

async def _daemon(cfg):
    from telethon import TelegramClient
    from telethon.errors import FloodWaitError

    try:
        channel_id = int(cfg["channel_id"])
    except (ValueError, KeyError):
        push_log("\u274c Invalid channel ID")
        state["status"] = "stopped"
        return

    photos_path = cfg.get("photos_path", "")
    if not validate_path(photos_path):
        push_log(f"\u274c Invalid photos path: {photos_path}")
        state["status"] = "stopped"
        return
    if not Path(photos_path).exists():
        push_log(f"\u274c Photos folder not found: {photos_path}")
        state["status"] = "stopped"
        return

    api_id   = int(cfg.get("api_id", TELEGRAM_API_ID))
    api_hash = cfg.get("api_hash", TELEGRAM_API_HASH)
    cleanup_after_backup = cfg.get("cleanup_after_backup", False)
    delete_after_backup  = cfg.get("delete_after_backup", False)
    try:
        cleanup_days = int(cfg.get("cleanup_days", 30))
        if not validate_cleanup_days(cleanup_days):
            cleanup_days = 30
    except (ValueError, TypeError):
        cleanup_days = 30

    start_file_watcher(photos_path)
    client = TelegramClient(SESSION_FILE, api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        push_log("\u274c Not authorized. Please log in first.")
        state["status"] = "stopped"
        await client.disconnect()
        stop_file_watcher()
        return

    sem  = asyncio.Semaphore(2)
    conn = db_pool.get_connection()
    if conn is None:
        push_log("\u274c Cannot connect to database")
        state["status"] = "stopped"
        await client.disconnect()
        stop_file_watcher()
        return

    # Bug 2A Fix: clean any stuck IN_FLIGHT rows from a previous crash
    clean_inflight_entries(conn)

    # v3.1.0 Fix 5: use stable APPDATA temp dir instead of system temp
    export = EXPORT_DIR

    push_log(f"\u2705 Connected! Watching: {photos_path}")
    state["cleanup_enabled"]             = cleanup_after_backup
    state["delete_after_backup_enabled"] = delete_after_backup

    last_full_scan_time = 0.0  # Bug 2D/4 Fix: throttle full scans
    total_uploaded      = 0    # v3.1.0: for completion notification
    _daemon_error       = False

    try:
        while state["status"] == "running":
            try:
                # Bug 4 Fix: always drain watcher events first
                with pending_files_lock:
                    files_to_check = list(pending_files)
                    pending_files.clear()

                # Bug 2D/4 Fix: merge periodic full walk every 60s
                now = time.time()
                if now - last_full_scan_time >= 60:
                    last_full_scan_time = now
                    try:
                        for root, dirs, fnames in os.walk(photos_path):
                            dirs[:] = [d for d in dirs if not d.startswith('.')]
                            for fn in fnames:
                                if fn.lower().endswith(ALL_EXT):
                                    full_p = os.path.join(root, fn)
                                    if full_p not in files_to_check:
                                        files_to_check.append(full_p)
                    except PermissionError as e:
                        push_log(f"\u26a0\ufe0f Permission error scanning: {e}")
                    except Exception as e:
                        push_log(f"\u26a0\ufe0f Scan error: {e}")

                # Bug 4 Fix: sleep if nothing found at all
                if not files_to_check:
                    await asyncio.sleep(5)
                    continue

                now2      = time.time()
                file_hashes: dict = {}
                for fp in files_to_check:
                    fname = os.path.basename(fp)
                    # v3.1.0 Fix 2: skip .icloud stub files
                    if fp.lower().endswith('.icloud'):
                        state["skipped_placeholders"] += 1
                        continue
                    # v3.1.0 Fix 2: skip Windows online-only placeholders
                    if is_icloud_placeholder(fp):
                        push_log(f"\u23ed\ufe0f Skipped (iCloud placeholder not downloaded): {fname}")
                        state["skipped_placeholders"] += 1
                        continue
                    try:
                        mtime = os.path.getmtime(fp)
                        sz_check = os.path.getsize(fp)
                    except OSError:
                        continue
                    if now2 - mtime < 3:
                        continue
                    # v3.1.0 Fix 2: size sanity check for image stubs
                    if sz_check < MIN_MEDIA_SIZE and os.path.splitext(fp)[1].lower() in IMAGE_EXT:
                        push_log(f"\u23ed\ufe0f Skipped (too small, likely placeholder): {fname} ({sz_check}B)")
                        state["skipped_placeholders"] += 1
                        continue
                    fhash = compute_file_hash(fp, use_cache=True)
                    if fhash:
                        file_hashes[fp] = fhash

                if not file_hashes:
                    if cleanup_after_backup:
                        await asyncio.sleep(1)
                        cleanup_icloud_storage(conn, cleanup_days, photos_path)
                    push_log("\U0001f50d No new files. Waiting...")
                    await asyncio.sleep(15)
                    continue

                uploaded_set = are_uploaded_batch(conn, list(file_hashes.values()))
                tasks = []
                for fp, fhash in file_hashes.items():
                    if fhash in uploaded_set:
                        continue
                    try:
                        sz = os.path.getsize(fp)
                    except OSError:
                        continue
                    tasks.append(_upload_one(
                        client, conn, channel_id, fp, sz, fhash,
                        sem, export, fp, photos_path, delete_after_backup
                    ))

                if tasks:
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    ok = sum(1 for r in results if r is True)
                    if ok:
                        total_uploaded += ok
                        push_log(f"\u2705 Uploaded {ok} new file(s).")
                        if cleanup_after_backup:
                            await asyncio.sleep(2)
                            cleanup_icloud_storage(conn, cleanup_days, photos_path)
                    # Bug 5 Fix: cooldown after upload batch
                    await asyncio.sleep(5)

                # v3.1.0 Fix 5: check temp disk usage each iteration
                check_temp_disk_usage()

                cnt, raw = get_stats(conn)
                state["count"]    = cnt
                state["size_str"] = fmt_size(raw)

            except FloodWaitError as e:
                push_log(f"\u23f3 Flood wait {e.seconds}s")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                push_log(f"\u26a0\ufe0f Error: {e}")
                _daemon_error = True
                await asyncio.sleep(5)

    finally:
        await client.disconnect()
        db_pool.return_connection(conn)
        stop_file_watcher()
        push_log("\U0001f6d1 Daemon stopped.")
        # v3.1.0 Fix 7: Windows & browser-visible notifications
        if total_uploaded > 0 and not _daemon_error:
            send_windows_notification(
                "\u2705 Backup Complete",
                f"Telegram Backup Pro: {total_uploaded} file(s) uploaded successfully"
            )
        elif _daemon_error:
            send_windows_notification(
                "\u26a0\ufe0f Backup Stopped",
                "Telegram Backup Pro stopped unexpectedly. Check the dashboard."
            )

async def _upload_one(client, conn, channel_id, fp, sz, fhash, sem, export_dir, local_path="", backup_folder="", delete_after_backup=False):
    """Upload one file with retry, progress, dedup guard, and hash-verified delete."""
    from telethon.errors import FloodWaitError
    fname = os.path.basename(fp)

    # Bug 2C Fix: deduplicate concurrent uploads
    with _uploading_lock:
        if fhash in _uploading_now:
            return False
        _uploading_now.add(fhash)

    if is_uploaded(conn, fhash):
        with _uploading_lock:
            _uploading_now.discard(fhash)
        return False

    mark_uploaded(conn, fhash, f"IN_FLIGHT_{fname}", sz, local_path)
    tmp = None
    async with sem:
        try:
            ts  = int(time.time() * 1000)
            tmp = export_dir / f"{ts}_{fname}"
            await asyncio.get_event_loop().run_in_executor(thread_pool, shutil.copy2, fp, tmp)

            date_str = datetime.fromtimestamp(os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M")
            ext = os.path.splitext(fname)[1].upper().lstrip('.')
            cap = f"\U0001f4c1 {fname}\n\U0001f4c5 {date_str}\n\U0001f3f7 {ext}  \u2022  {fmt_size(sz)}"

            # Improvement 2: progress callback
            def make_progress_callback(fn):
                last_pct = [0]
                def _cb(sent, total):
                    if total == 0:
                        return
                    pct = int(sent * 100 / total)
                    if pct >= last_pct[0] + 10:
                        last_pct[0] = pct
                        push_log(f"\u2b06\ufe0f {fn}: {pct}%")
                        with _upload_progress_lock:
                            _upload_progress[fn] = pct
                return _cb

            push_log(f"\u2b06\ufe0f  Uploading {fname}...")

            # Improvement 1: retry with exponential backoff
            last_exc = None
            for attempt in range(MAX_RETRIES):
                try:
                    await client.send_file(
                        channel_id, str(tmp), caption=cap,
                        force_document=True,
                        progress_callback=make_progress_callback(fname)
                    )
                    last_exc = None
                    break
                except FloodWaitError as e:
                    push_log(f"\u23f3 Flood wait {e.seconds}s (attempt {attempt+1})")
                    await asyncio.sleep(e.seconds)
                    last_exc = e
                except (ConnectionError, TimeoutError, OSError) as e:
                    last_exc = e
                    if attempt < MAX_RETRIES - 1:
                        wait = RETRY_DELAYS[attempt]
                        push_log(f"\U0001f504 Retry {attempt+1}/{MAX_RETRIES} for {fname} in {wait}s...")
                        await asyncio.sleep(wait)
                    else:
                        push_log(f"\u274c Failed after {MAX_RETRIES} attempts: {fname}")
                        raise
                except Exception:
                    raise

            with _upload_progress_lock:
                _upload_progress.pop(fname, None)

            mark_uploaded(conn, fhash, fname, sz, local_path)

            # Bug 6 Fix: yield then verify by hash before deleting
            await asyncio.sleep(0)
            if delete_after_backup and backup_folder:
                if is_uploaded(conn, fhash):
                    try_delete_file_after_backup(conn, local_path, backup_folder, fhash)
                else:
                    push_log(f"\u26a0\ufe0f Could not verify upload for deletion: {fname}")

            # Remove from failed list if previously failed
            remove_failed_upload(conn, local_path)
            return True

        except Exception as e:
            push_log(f"\u274c Failed {fname}: {e}")
            with _upload_progress_lock:
                _upload_progress.pop(fname, None)
            try:
                with _db_lock:
                    conn.execute("DELETE FROM uploads WHERE uuid=?", (fhash,))
                    conn.commit()
            except Exception:
                pass
            # Improvement 1: record in failed_uploads after all retries exhausted
            mark_failed_upload(conn, local_path, fname, e)
            return False
        finally:
            if tmp and tmp.exists():
                try:
                    tmp.unlink()
                except Exception:
                    pass
            with _uploading_lock:
                _uploading_now.discard(fhash)

def start_daemon():
    global _daemon_loop
    cfg = load_config()
    _daemon_loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(_daemon_loop)
        _daemon_loop.run_until_complete(_daemon(cfg))
    finally:
        try:
            _daemon_loop.close()
        except Exception:
            pass
        _daemon_loop = None

# ── OTP login ──────────────────────────────────────────────────────────
_login_state = {"step": "idle", "phone": "", "hash": ""}

async def _send_otp(phone):
    from telethon import TelegramClient
    client = TelegramClient(SESSION_FILE, TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await client.connect()
    sent = await client.send_code_request(phone)
    _login_state["hash"] = sent.phone_code_hash
    _login_state["phone"] = phone
    await client.disconnect()

async def _verify_otp(code):
    """Sign in with OTP; detects 2FA and signals caller via SessionPasswordNeededError."""
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
    client = TelegramClient(SESSION_FILE, TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await client.connect()
    try:
        await client.sign_in(
            _login_state["phone"], code,
            phone_code_hash=_login_state["hash"]
        )
    except SessionPasswordNeededError:
        _login_state["step"] = "waiting_2fa"
        await client.disconnect()
        raise  # re-raise so handler can send needs_2fa response
    me = await client.get_me()
    state["authorized"] = True
    await client.disconnect()
    return me.first_name


async def _verify_2fa(password: str):
    """Complete login for 2FA-enabled accounts."""
    from telethon import TelegramClient
    from telethon.errors import PasswordHashInvalidError
    client = TelegramClient(SESSION_FILE, TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await client.connect()
    try:
        me = await client.sign_in(password=password)
        state["authorized"] = True
        return me.first_name
    except PasswordHashInvalidError:
        raise ValueError("Incorrect 2FA password. Please try again.")
    finally:
        await client.disconnect()

# ── HTTP Server ────────────────────────────────────────────────────────
from http.server import BaseHTTPRequestHandler, HTTPServer
import urllib.parse as urlparse

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send_json(self, data, code=200):
        try:
            body = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            logger.error(f"Failed to send response: {e}")

    def send_html(self, html: str):
        try:
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type","text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            logger.error(f"Failed to send HTML: {e}")

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/":
                # v3.1.0: inject CSRF token + PORT into HTML
                html = DASHBOARD_HTML.replace("{{CSRF_TOKEN}}", _csrf_token).replace("{{PORT}}", str(PORT))
                self.send_html(html)
            elif path == "/api/state":
                with _upload_progress_lock:
                    prog = dict(_upload_progress)
                self.send_json({**state, "logs": list(state["logs"])[-50:], "upload_progress": prog})
            elif path == "/api/config":
                self.send_json(load_config())
            elif path == "/api/detect_icloud":
                self.send_json({"path": detect_icloud()})
            elif path == "/api/update_info":
                self.send_json({**update_state, "current": APP_VERSION, "frozen": getattr(sys, 'frozen', False)})
            elif path == "/api/failed_files":
                _c = db_pool.get_connection()
                rows = get_failed_uploads(_c) if _c else []
                db_pool.return_connection(_c)
                self.send_json([{"path": r[0], "filename": r[1], "reason": r[2], "count": r[3], "last": r[4]} for r in rows])
            else:
                self.send_response(404)
                self.end_headers()
        except Exception as e:
            logger.error(f"GET error: {e}")
            self.send_json({"error": "Internal error"}, 500)

    def do_POST(self):
        try:
            # v3.1.0 Fix 3: CSRF token check on all POST requests
            token = self.headers.get("X-Backup-Token", "")
            if token != _csrf_token:
                self.send_json({"error": "Unauthorized"}, 403)
                return

            # Security Fix #10: Validate content length
            length = int(self.headers.get("Content-Length", 0))
            if length > MAX_BODY_SIZE:
                self.send_json({"error": "Request too large"}, 413)
                return

            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self.send_json({"error": "Invalid JSON"}, 400)
                return

            path = self.path

            if path == "/api/send_otp":
                phone = body.get("phone","").strip()
                
                # Security Fix #3: Validate input
                if not validate_phone(phone):
                    self.send_json({"ok": False, "error": "Invalid phone number"})
                    return
                
                try:
                    # Security Fix #4: Close event loop properly
                    loop = asyncio.new_event_loop()
                    try:
                        asyncio.set_event_loop(loop)
                        loop.run_until_complete(_send_otp(phone))
                    finally:
                        loop.close()
                    
                    _login_state["step"] = "waiting_otp"
                    self.send_json({"ok": True})
                except Exception as e:
                    self.send_json({"ok": False, "error": str(e)})

            elif path == "/api/verify_otp":
                code = body.get("code", "").strip()
                try:
                    from telethon.errors import SessionPasswordNeededError
                    loop = asyncio.new_event_loop()
                    try:
                        asyncio.set_event_loop(loop)
                        name = loop.run_until_complete(_verify_otp(code))
                    finally:
                        loop.close()
                    cfg = load_config()
                    cfg.update({"api_id": TELEGRAM_API_ID, "api_hash": TELEGRAM_API_HASH,
                                 "phone": _login_state["phone"]})
                    save_config(cfg)
                    self.send_json({"ok": True, "name": name})
                except SessionPasswordNeededError:
                    self.send_json({"ok": False, "needs_2fa": True,
                                    "error": "This account has Two-Step Verification enabled. Please enter your Telegram password."})
                except Exception as e:
                    self.send_json({"ok": False, "error": str(e)})

            elif path == "/api/verify_2fa":
                password = body.get("password", "").strip()
                if not password:
                    self.send_json({"ok": False, "error": "Password is required"})
                    return
                try:
                    loop = asyncio.new_event_loop()
                    try:
                        asyncio.set_event_loop(loop)
                        name = loop.run_until_complete(_verify_2fa(password))
                    finally:
                        loop.close()
                    cfg = load_config()
                    cfg.update({"api_id": TELEGRAM_API_ID, "api_hash": TELEGRAM_API_HASH,
                                 "phone": _login_state["phone"]})
                    save_config(cfg)
                    self.send_json({"ok": True, "name": name})
                except ValueError as e:
                    self.send_json({"ok": False, "error": str(e)})
                except Exception as e:
                    self.send_json({"ok": False, "error": str(e)})

            elif path == "/api/logout":
                try:
                    for candidate in [Path(SESSION_FILE + ".session"), Path(SESSION_FILE)]:
                        if candidate.exists():
                            candidate.unlink()
                    state["authorized"] = False
                    _login_state["step"] = "idle"
                    _login_state["phone"] = ""
                    _login_state["hash"]  = ""
                    self.send_json({"ok": True})
                except Exception as e:
                    self.send_json({"ok": False, "error": str(e)})

            elif path == "/api/save_config":
                cfg = load_config()
                
                # Security Fix #3: Validate all inputs
                if "phone" in body and body["phone"]:
                    if not validate_phone(body["phone"]):
                        self.send_json({"ok": False, "error": "Invalid phone"})
                        return
                    cfg["phone"] = body["phone"]
                
                if "channel_id" in body and body["channel_id"]:
                    if not validate_channel_id(body["channel_id"]):
                        self.send_json({"ok": False, "error": "Invalid channel ID"})
                        return
                    cfg["channel_id"] = body["channel_id"]
                
                if "photos_path" in body and body["photos_path"]:
                    if not validate_path(body["photos_path"]):
                        self.send_json({"ok": False, "error": "Invalid path"})
                        return
                    cfg["photos_path"] = body["photos_path"]
                
                if "cleanup_after_backup" in body:
                    cfg["cleanup_after_backup"] = bool(body["cleanup_after_backup"])
                
                if "cleanup_days" in body:
                    if not validate_cleanup_days(body["cleanup_days"]):
                        self.send_json({"ok": False, "error": "cleanup_days must be 1-365"})
                        return
                    cfg["cleanup_days"] = int(body["cleanup_days"])
                
                # ✅ NEW: Handle delete_after_backup config option
                if "delete_after_backup" in body:
                    cfg["delete_after_backup"] = bool(body["delete_after_backup"])
                
                if "windows_startup" in body:
                    cfg["windows_startup"] = bool(body["windows_startup"])
                
                if "auto_start_backup" in body:
                    cfg["auto_start_backup"] = bool(body["auto_start_backup"])
                
                save_config(cfg)
                if "windows_startup" in body:
                    set_windows_startup(body["windows_startup"])
                self.send_json({"ok": True})

            elif path == "/api/start":
                if state["status"] != "running":
                    state["status"] = "running"
                    push_log("🚀 Backup started.")
                    threading.Thread(target=start_daemon, daemon=True).start()
                self.send_json({"ok": True})

            elif path == "/api/stop":
                state["status"] = "stopped"
                self.send_json({"ok": True})

            elif path == "/api/quit":
                state["status"] = "stopped"
                self.send_json({"ok": True})
                def _shutdown():
                    time.sleep(1)
                    os._exit(0)
                threading.Thread(target=_shutdown).start()

            elif path == "/api/do_update":
                url = update_state.get("url", "")
                if not url:
                    self.send_json({"ok": False, "error": "No update URL available"})
                else:
                    self.send_json({"ok": True, "msg": "Update started..."})
                    threading.Thread(target=do_self_update, args=(url,), daemon=True).start()

            elif path == "/api/retry_failed":
                local_path = body.get("path", "")
                if local_path and os.path.exists(local_path):
                    _c = db_pool.get_connection()
                    if _c:
                        remove_failed_upload(_c, local_path)
                        db_pool.return_connection(_c)
                    with pending_files_lock:
                        pending_files.add(local_path)
                    self.send_json({"ok": True})
                else:
                    self.send_json({"ok": False, "error": "File not found"})

            else:
                self.send_response(404)
                self.end_headers()
        
        except Exception as e:
            logger.error(f"POST error: {e}")
            try:
                self.send_json({"error": "Internal error"}, 500)
            except:
                pass

# ── Dashboard HTML ────────────────────────────────────────────────────
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Telegram Backup Pro</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#e6edf3;min-height:100vh}
  .app{max-width:900px;margin:0 auto;padding:24px 16px}
  h1{font-size:1.6rem;font-weight:700;margin-bottom:4px}
  .sub{color:#8b949e;font-size:.9rem;margin-bottom:28px}
  .card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:24px;margin-bottom:20px}
  .card h2{font-size:1rem;font-weight:600;margin-bottom:16px;color:#58a6ff}
  .row{display:flex;gap:12px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
  label{min-width:140px;color:#8b949e;font-size:.875rem}
  input{flex:1;background:#0d1117;border:1px solid #30363d;border-radius:8px;
        padding:9px 12px;color:#e6edf3;font-size:.9rem;outline:none;min-width:200px}
  input:focus{border-color:#58a6ff}
  button{padding:10px 22px;border:none;border-radius:8px;cursor:pointer;font-size:.9rem;font-weight:600;transition:.2s}
  .btn-primary{background:#238636;color:#fff}
  .btn-primary:hover{background:#2ea043}
  .btn-blue{background:#1f6feb;color:#fff}
  .btn-blue:hover{background:#388bfd}
  .btn-red{background:#b62324;color:#fff}
  .btn-red:hover{background:#da3633}
  .btn-sm{padding:7px 14px;font-size:.8rem}
  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:20px}
  .stat{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:16px;text-align:center}
  .stat .val{font-size:1.6rem;font-weight:700;color:#58a6ff}
  .stat .key{font-size:.8rem;color:#8b949e;margin-top:4px}
  #logbox{background:#0d1117;border:1px solid #30363d;border-radius:8px;
           padding:12px;height:260px;overflow-y:auto;font-family:'Cascadia Code','Courier New',monospace;
           font-size:.8rem;color:#7ee787;line-height:1.6}
  .badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:.75rem;font-weight:600}
  .badge.idle{background:#21262d;color:#8b949e}
  .badge.running{background:#1a4720;color:#3fb950}
  .badge.stopped{background:#3d1a1a;color:#f85149}
  .tabs{display:flex;gap:4px;margin-bottom:20px;border-bottom:1px solid #30363d;padding-bottom:0}
  .tab{padding:8px 20px;cursor:pointer;border-radius:8px 8px 0 0;font-size:.9rem;color:#8b949e}
  .tab.active{background:#161b22;color:#e6edf3;border:1px solid #30363d;border-bottom:1px solid #161b22;margin-bottom:-1px}
  .pane{display:none}.pane.active{display:block}
  .msg{padding:10px 14px;border-radius:8px;margin-top:10px;font-size:.875rem}
  .msg.ok{background:#1a4720;color:#3fb950}
  .msg.err{background:#3d1a1a;color:#f85149}
  .info-row{background:#0d1117;padding:8px 12px;border-left:3px solid #58a6ff;margin:10px 0;font-size:.85rem}
  .checkbox-wrapper{display:flex;align-items:center;gap:12px;margin-bottom:12px}
  .checkbox-wrapper input[type="checkbox"]{width:auto;flex:0}
  .checkbox-wrapper label{margin:0;flex:1}
  .hint{display:block;color:#8b949e;font-size:.75rem;margin-top:4px}
</style>
</head>
<body>
<div class="app">
  <h1>📸 Telegram Backup Pro v3.0.0</h1>
  <p class="sub">Backup your photos & videos to Telegram — automatically.<br><span style="color:#58a6ff;font-weight:600;">Developed by Shithel</span></p>

  <div id="update-banner" style="display:none;background:#1c2a14;border:1px solid #3fb950;border-radius:10px;padding:12px 18px;margin-bottom:16px;align-items:center;gap:12px;">
    <span style="font-size:1.1rem;">🆕</span>
    <span id="update-text" style="flex:1;font-size:.9rem;"></span>
    <button id="update-btn" onclick="doUpdate()" style="background:#238636;color:#fff;padding:7px 16px;border-radius:6px;border:none;font-size:.85rem;font-weight:600;cursor:pointer;">⬇️ Download &amp; Install Update</button>
  </div>

  <div class="tabs">
    <div class="tab active" onclick="showTab('dashboard')">Dashboard</div>
    <div class="tab" onclick="showTab('login')">Telegram Login</div>
    <div class="tab" onclick="showTab('settings')">Settings</div>
  </div>

  <!-- DASHBOARD -->
  <div class="pane active" id="tab-dashboard">
    <div class="stats">
      <div class="stat"><div class="val" id="s-count">0</div><div class="key">Files Backed Up</div></div>
      <div class="stat"><div class="val" id="s-size">0 B</div><div class="key">Total Size</div></div>
      <div class="stat"><div class="val"><span class="badge idle" id="s-status">Idle</span></div><div class="key">Status</div></div>
      <div class="stat"><div class="val" id="s-skipped" style="font-size:1.3rem;">0</div><div class="key">⏭️ Placeholders Skipped</div></div>
    </div>
    <div id="cleanup-banner" style="display:none;background:#1a3d1a;border:1px solid #3fb950;border-radius:10px;padding:12px 18px;margin-bottom:16px;">
      <span style="font-size:.9rem;color:#3fb950">✅ Storage cleanup enabled - Backed-up files will be automatically deleted after <span id="cleanup-days">30</span> days</span>
    </div>
    <div id="delete-banner" style="display:none;background:#1a3d3d;border:1px solid #3fb950;border-radius:10px;padding:12px 18px;margin-bottom:16px;">
      <span style="font-size:.9rem;color:#3fb950">⚡ Instant delete enabled - Files will be deleted from iCloud immediately after successful backup</span>
    </div>
    <div class="card">
      <h2>Controls</h2>
      <div class="row">
        <button class="btn-primary" onclick="startBackup()">▶ Start Backup</button>
        <button class="btn-red" onclick="stopBackup()">⏹ Stop Backup</button>
        <button style="background:#555;color:#fff;" onclick="quitApp()">⏏ Exit App Completely</button>
      </div>
    </div>
    <div class="card">
      <h2>Live Log</h2>
      <div id="logbox">Waiting for activity…</div>
      <div id="progress-section" style="margin-top:10px;"></div>
    </div>
    <div id="failed-card" class="card" style="display:none;">
      <h2 style="color:#f85149;">⚠️ Failed Uploads</h2>
      <div id="failed-list"></div>
      <button class="btn-blue btn-sm" style="margin-top:10px;" onclick="retryAll()">&#x1f504; Retry All</button>
    </div>
  </div>

  <!-- LOGIN -->
  <div class="pane" id="tab-login">
    <div class="card">
      <h2>Connect Your Telegram Account</h2>
      <div class="row"><label>Phone Number</label><input id="phone" placeholder="+8801XXXXXXXXX" /></div>
      <div class="row"><label>Channel ID</label><input id="channel_id" placeholder="-100XXXXXXXXXX" /></div>
      <p style="color:#8b949e;font-size:.8rem;margin-bottom:14px">
        Tip: Forward any message from your channel to @userinfobot to get the Channel ID.
      </p>
      <button class="btn-blue" onclick="sendOtp()">Send Login Code &rarr;</button>
      <div id="otp-row" style="display:none;margin-top:16px">
        <div class="row"><label>Enter OTP</label><input id="otp" placeholder="Code from Telegram" /></div>
        <button class="btn-primary" onclick="verifyOtp()">&check; Verify Code</button>
      </div>
      <!-- v3.1.0: 2FA section -->
      <div id="twofa-section" style="display:none;margin-top:16px;padding:14px;background:#1a1a2e;border:1px solid #f0a500;border-radius:8px;">
        <p style="color:#f0a500;margin-bottom:12px;font-size:.9rem;">
          &#x1f510; This account has Two-Step Verification enabled.<br>Enter your Telegram password below.
        </p>
        <div class="row"><label>Telegram Password</label><input type="password" id="twofa-pass" placeholder="Your 2FA password" /></div>
        <button class="btn-primary" onclick="verify2FA()">&#x1f511; Confirm Password</button>
      </div>
      <div id="login-msg"></div>
      <!-- v3.1.0: Logout button -->
      <div id="logout-row" style="display:none;margin-top:16px;padding-top:12px;border-top:1px solid #30363d;">
        <button style="background:#b91c1c;color:#fff;border:none;padding:8px 18px;border-radius:6px;cursor:pointer;" onclick="doLogout()">&#x1f513; Logout from Telegram</button>
      </div>
    </div>
    <p style="text-align:center;color:#8b949e;font-size:.75rem;margin-top:8px;">&#x1f512; Dashboard is only accessible from this computer (localhost:{{PORT}})</p>
  </div>

  <!-- SETTINGS -->
  <div class="pane" id="tab-settings">
    <div class="card">
      <h2>Backup Folder</h2>
      <div class="row">
        <label>Photos Path</label>
        <input id="photos_path" placeholder="C:\\Users\\You\\Pictures\\iCloud Photos\\Photos" />
        <button class="btn-blue btn-sm" onclick="detectiCloud()">Auto-Detect iCloud</button>
      </div>
      <div class="row" style="margin-top:16px;">
        <label style="min-width:auto;cursor:pointer;">
          <input type="checkbox" id="cleanup_after_backup" style="min-width:auto;margin-right:8px;vertical-align:middle;">
          🗑️ Auto-cleanup: Delete backed-up files from iCloud (frees up storage)
        </label>
      </div>
      <div class="row" style="margin-top:8px;align-items:flex-start;">
        <label>Days before deletion</label>
        <input id="cleanup_days" type="number" min="1" max="365" value="30" style="max-width:100px;" />
        <span style="color:#8b949e;font-size:.8rem;margin-left:8px;">Files older than this will be deleted</span>
      </div>
      <div class="row" style="margin-top:16px;">
        <label style="min-width:auto;cursor:pointer;">
          <input type="checkbox" id="delete_after_backup" style="min-width:auto;margin-right:8px;vertical-align:middle;">
          ⚡ <strong>Delete from iCloud immediately after successful backup</strong>
        </label>
      </div>
      <p style="color:#f85149;font-size:.75rem;margin-bottom:16px;margin-left:28px;">
        ⚠️ Files will be permanently deleted immediately after Telegram upload. This cannot be undone.
      </p>
      <div class="row" style="margin-top:16px;">
        <label style="min-width:auto;cursor:pointer;">
          <input type="checkbox" id="windows_startup" style="min-width:auto;margin-right:8px;vertical-align:middle;">
          Start app automatically when Windows boots
        </label>
      </div>
      <div class="row" style="margin-top:8px;">
        <label style="min-width:auto;cursor:pointer;">
          <input type="checkbox" id="auto_start_backup" style="min-width:auto;margin-right:8px;vertical-align:middle;">
          Auto-start backup immediately when app opens
        </label>
      </div>
      <button class="btn-primary" style="margin-top:20px;" onclick="saveSettings()">Save Settings</button>
      <div id="settings-msg"></div>
    </div>
  </div>
</div>

<script>
function showTab(t){
  document.querySelectorAll('.tab').forEach((el,i)=>{el.classList.remove('active')});
  document.querySelectorAll('.pane').forEach(el=>el.classList.remove('active'));
  document.querySelector('#tab-'+t).classList.add('active');
  event.target.classList.add('active');
}

async function api(method,path,body){
  const headers={'Content-Type':'application/json','X-Backup-Token':'{{CSRF_TOKEN}}'};
  const r=await fetch(path,{method,headers,body:body?JSON.stringify(body):undefined});
  return r.json();
}

async function sendOtp(){
  const phone=document.getElementById('phone').value.trim();
  const ch=document.getElementById('channel_id').value.trim();
  if(!phone){alert('Enter your phone number'); return;}
  setMsg('login-msg','Sending code…','');
  const r=await api('POST','/api/send_otp',{phone});
  if(r.ok){
    document.getElementById('otp-row').style.display='block';
    setMsg('login-msg','✅ Code sent! Check Telegram.','ok');
    if(ch) await api('POST','/api/save_config',{channel_id:ch});
  } else {
    setMsg('login-msg','❌ '+(r.error||'Error'),'err');
  }
}

async function verifyOtp(){
  const code=document.getElementById('otp').value.trim();
  setMsg('login-msg','Verifying…','');
  const r=await api('POST','/api/verify_otp',{code});
  if(r.ok){
    setMsg('login-msg','\u2705 Logged in as '+r.name+'! Go to Settings to set your folder, then start backup.','ok');
    document.getElementById('logout-row').style.display='block';
  } else if(r.needs_2fa){
    document.getElementById('otp-row').style.display='none';
    document.getElementById('twofa-section').style.display='block';
    setMsg('login-msg','','');
  } else {
    setMsg('login-msg','\u274c '+(r.error||'Error'),'err');
  }
}

async function verify2FA(){
  const pw=document.getElementById('twofa-pass').value;
  if(!pw){alert('Enter your 2FA password');return;}
  setMsg('login-msg','Verifying password\u2026','');
  const r=await api('POST','/api/verify_2fa',{password:pw});
  if(r.ok){
    document.getElementById('twofa-section').style.display='none';
    document.getElementById('logout-row').style.display='block';
    setMsg('login-msg','\u2705 Logged in as '+r.name+'! Go to Settings to set your folder, then start backup.','ok');
  } else {
    setMsg('login-msg','\u274c '+(r.error||'Wrong password'),'err');
  }
}

async function doLogout(){
  if(!confirm('This will disconnect your Telegram account. You will need to log in again to resume backups. Continue?')) return;
  await api('POST','/api/logout',{});
  document.getElementById('logout-row').style.display='none';
  document.getElementById('twofa-section').style.display='none';
  document.getElementById('otp-row').style.display='none';
  setMsg('login-msg','\u2705 Logged out successfully.','ok');
}

async function detectiCloud(){
  const r=await api('GET','/api/detect_icloud');
  document.getElementById('photos_path').value=r.path;
}

async function saveSettings(){
  const path=document.getElementById('photos_path').value.trim();
  const cleanup=document.getElementById('cleanup_after_backup').checked;
  const cleanup_days=parseInt(document.getElementById('cleanup_days').value) || 30;
  const deleteAfterBackup=document.getElementById('delete_after_backup').checked;
  const startup=document.getElementById('windows_startup').checked;
  const auto=document.getElementById('auto_start_backup').checked;
  const r=await api('POST','/api/save_config',{
    photos_path:path, 
    cleanup_after_backup:cleanup,
    cleanup_days:cleanup_days,
    delete_after_backup:deleteAfterBackup,
    windows_startup:startup,
    auto_start_backup:auto
  });
  setMsg('settings-msg', r.ok?'✅ Saved!':'❌ '+(r.error||'Error'), r.ok?'ok':'err');
}

async function startBackup(){ await api('POST','/api/start',{}); }
async function stopBackup(){ await api('POST','/api/stop',{}); }
async function quitApp(){ 
  if(confirm('This will stop the backup and completely close the background engine. Are you sure?')) {
    await api('POST','/api/quit',{}); 
    document.body.innerHTML = '<h2 style="padding:40px;text-align:center;color:#8b949e">App closed. You can now close this browser tab.</h2>';
  }
}

function setMsg(id,text,type){
  const el=document.getElementById(id);
  el.innerHTML='<div class="msg '+type+'">'+text+'</div>';
}

let lastLogLen=0;
async function poll(){
  try{
    const d=await api('GET','/api/state');
    document.getElementById('s-count').textContent=d.count;
    document.getElementById('s-size').textContent=d.size_str;
    const badge=document.getElementById('s-status');
    badge.textContent=d.status.charAt(0).toUpperCase()+d.status.slice(1);
    badge.className='badge '+d.status;
    if(d.logs.length!==lastLogLen){
      lastLogLen=d.logs.length;
      const box=document.getElementById('logbox');
      box.innerHTML=d.logs.map(l=>'<div>'+l+'</div>').join('');
      box.scrollTop=box.scrollHeight;
    }
    document.getElementById('cleanup-banner').style.display=d.cleanup_enabled?'block':'none';
    document.getElementById('delete-banner').style.display=d.delete_after_backup_enabled?'block':'none';
    if(document.getElementById('s-skipped')) document.getElementById('s-skipped').textContent=d.skipped_placeholders||0;
    // v3.1.0 Fix 7: browser notification when backup transitions running->stopped
    if(typeof _lastStatus!=='undefined' && _lastStatus==='running' && d.status==='stopped' && d.count>0){
      if(Notification.permission==='granted'){
        new Notification('Telegram Backup Pro',{body:`Backup complete! ${d.count} files backed up.`});
      }
    }
    _lastStatus=d.status;
    // Improvement 2: show per-file progress bars
    const ps=document.getElementById('progress-section');
    if(d.upload_progress && Object.keys(d.upload_progress).length>0){
      ps.innerHTML=Object.entries(d.upload_progress).map(([fn,pct])=>
        `<div style="margin-bottom:6px;"><span style="font-size:.8rem;color:#8b949e;">${fn}</span>`+
        `<div style="background:#21262d;border-radius:4px;height:8px;margin-top:3px;"><div style="background:#58a6ff;width:${pct}%;height:8px;border-radius:4px;transition:width .3s;"></div></div></div>`
      ).join('');
    }else{ps.innerHTML='';}
    // Improvement 1: show failed files
    try{
      const fl=await api('GET','/api/failed_files');
      const fc=document.getElementById('failed-card');
      const flst=document.getElementById('failed-list');
      if(fl.length>0){
        fc.style.display='block';
        flst.innerHTML=fl.map(f=>`<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;padding:8px;background:#0d1117;border-radius:6px;">`+
          `<div style="flex:1;"><div style="font-size:.85rem;">${f.filename}</div><div style="font-size:.75rem;color:#8b949e;">${f.reason} &bull; ${f.count}x</div></div>`+
          `<button class="btn-blue btn-sm" onclick="retryFile('${f.path.replace(/'/g,"\\'")}')">&#x1f504; Retry</button></div>`
        ).join('');
      }else{fc.style.display='none';}
    }catch(e){}
  }catch(e){}
}

async function loadConfig(){
  try{
    const c=await api('GET','/api/config');
    if(c.phone) document.getElementById('phone').value=c.phone;
    if(c.channel_id) document.getElementById('channel_id').value=c.channel_id;
    if(c.photos_path) document.getElementById('photos_path').value=c.photos_path;
    if(c.cleanup_after_backup) document.getElementById('cleanup_after_backup').checked=true;
    if(c.cleanup_days) document.getElementById('cleanup_days').value=c.cleanup_days;
    if(c.delete_after_backup) document.getElementById('delete_after_backup').checked=true;
    if(c.windows_startup) document.getElementById('windows_startup').checked=true;
    if(c.auto_start_backup) document.getElementById('auto_start_backup').checked=true;
  }catch(e){}
}

loadConfig();
setInterval(poll,5000);
poll();
// v3.1.0 Fix 7: request browser notification permission on load
if(typeof Notification!=='undefined' && Notification.permission==='default'){
  Notification.requestPermission();
}
let _lastStatus=undefined;

async function retryFile(path){
  await api('POST','/api/retry_failed',{path});
  poll();
}
async function retryAll(){
  const fl=await api('GET','/api/failed_files');
  for(const f of fl) await api('POST','/api/retry_failed',{path:f.path});
  poll();
}

async function checkUpdate(){
  try{
    const u=await api('GET','/api/update_info');
    if(u.available){
      const b=document.getElementById('update-banner');
      document.getElementById('update-text').textContent=`New version v${u.latest} available! (You have v${u.current})`;
      const btn=document.getElementById('update-btn');
      if(u.frozen){
        btn.style.display='inline-block';
      }else{
        btn.textContent=`\ud83d\udce5 New version — run: git pull && python app_web.py`;
        btn.onclick=null; btn.style.cursor='default';
      }
      b.style.display='flex';
    }
  }catch(e){}
}
async function doUpdate(){
  const btn=document.getElementById('update-btn');
  btn.disabled=true; btn.textContent='\u23f3 Downloading...';
  const r=await api('POST','/api/do_update',{});
  if(!r.ok){btn.disabled=false;btn.textContent='\u2b07\ufe0f Download & Install Update';alert(r.error||'Update failed');}
}
checkUpdate();
setInterval(checkUpdate,300000);
</script>
</body>
</html>"""

# ── Session validity check (Bug 8 Fix) ────────────────────────────────────
def is_session_valid() -> bool:
    """Quick check if a Telethon session file exists and is non-empty."""
    for candidate in [Path(SESSION_FILE + ".session"), Path(SESSION_FILE)]:
        if candidate.exists() and candidate.stat().st_size > 0:
            return True
    return False

# ── Entry point ────────────────────────────────────────────────────
if __name__ == "__main__":
    cfg = load_config()

    if Path(SESSION_FILE).exists() or is_session_valid():
        state["authorized"] = True

    # Bug 8 Fix: only auto-start if session file is valid
    if cfg.get("auto_start_backup") and cfg.get("channel_id") and cfg.get("photos_path"):
        if is_session_valid():
            state["status"] = "running"
            push_log("\U0001f680 Auto-starting backup...")
            threading.Thread(target=start_daemon, daemon=True).start()
        else:
            push_log("\u26a0\ufe0f Auto-start skipped: not logged in yet. Please log in from the Telegram Login tab.")

    print(f"\n{'='*50}")
    print(f"  Telegram Backup Pro v{APP_VERSION}")
    print(f"  🔒 Security-hardened build")
    print(f"  Open: http://localhost:{PORT}")
    print(f"{'='*50}\n")

    if "--silent" not in sys.argv:
        import webbrowser
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()

    threading.Thread(target=_update_check_loop, daemon=True).start()

    try:
        server = HTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        msg = (f"Cannot start Telegram Backup Pro.\n\n"
               f"Port {PORT} is already in use — another instance may be running.\n\n"
               f"Open Task Manager, find TelegramBackup.exe, and End Task, "
               f"then launch this app again.\n\nError: {e}")
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, "Telegram Backup Pro — Error", 0x10)
        else:
            print(msg)
        sys.exit(1)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        db_pool.close_all()
        if observer:
            stop_file_watcher()
