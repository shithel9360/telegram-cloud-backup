"""
Telegram Backup Pro — Web Edition (v2.2.3)
Single-file web app. Run with: python app_web.py
Then open: http://localhost:7878

IMPROVEMENTS IN v2.2.3:
- FIX: Bundled watchdog module in .exe (fixes 'No module named watchdog' crash on launch)

IMPROVEMENTS IN v2.2.2:
- NEW: Instant delete after backup feature (immediately frees iCloud space)
- Security: SQL injection prevention, path traversal protection, input validation
- Stability: Event loop cleanup, database error handling, resource management
- Robustness: Permission error handling, file cleanup, config validation
"""

import os, sys, json, time, asyncio, threading, logging, shutil, tempfile, sqlite3, hashlib
from pathlib import Path
from datetime import datetime
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ── Config ────────────────────────────────────────────────────────────
CONFIG_FILE  = Path.home() / ".tele_backup_config.json"
DB_FILE      = Path.home() / ".tele_backup_state.db"
SESSION_FILE = str(Path.home() / ".tele_backup_session")
APPDATA      = Path(os.environ.get("APPDATA", Path.home())) / "TelegramBackupPro"
APPDATA.mkdir(parents=True, exist_ok=True)
LOG_FILE     = APPDATA / "backup.log"
PORT         = 7878

TELEGRAM_API_ID   = 36355055
TELEGRAM_API_HASH = "9b819327f0403ce37b08e316a8464cb6"

APP_VERSION  = "2.2.3"          # fix: watchdog bundled in .exe
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()]
)
logger = logging.getLogger("BackupPro")

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
    """Validate path is within allowed directories (Security Fix #2)"""
    if not user_path or len(user_path) > 260:  # Windows path limit
        return False
    try:
        user_resolved = Path(user_path).resolve()
        # Allow paths in Pictures, Documents, or Downloads
        allowed_bases = [
            Path.home() / "Pictures",
            Path.home() / "Documents",
            Path.home() / "Downloads",
        ]
        for base in allowed_bases:
            if str(user_resolved).startswith(str(base)):
                return True
        return False
    except (OSError, ValueError):
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

# ── Database Connection Pool (fix #9) ──────────────────────────────────
class DBConnectionPool:
    def __init__(self, db_path, pool_size=5):
        self.db_path = db_path
        self.pool = []
        self.lock = threading.Lock()
        for _ in range(pool_size):
            try:
                conn = sqlite3.connect(str(db_path), check_same_thread=True, timeout=30)
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
            conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Database initialization failed: {e}")
    
    def get_connection(self):
        with self.lock:
            if self.pool:
                return self.pool.pop()
        try:
            return sqlite3.connect(str(self.db_path), check_same_thread=True, timeout=30)
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
    "logs":   deque(maxlen=200),
    "count":  0,
    "size_str": "0 B",
    "authorized": False,
    "cleanup_enabled": False,
    "cleanup_count": 0,
    "delete_after_backup_enabled": False,
    "deleted_count": 0,
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
        return bool(conn.execute("SELECT 1 FROM uploads WHERE uuid=?", (fhash,)).fetchone())
    except sqlite3.Error as e:
        logger.error(f"DB query failed: {e}")
        return False

def are_uploaded_batch(conn, fhashes):
    """Batch check - fix #7: N+1 queries"""
    if conn is None or not fhashes:
        return set()
    try:
        placeholders = ','.join(['?'] * len(fhashes))
        query = f"SELECT uuid FROM uploads WHERE uuid IN ({placeholders})"
        results = conn.execute(query, fhashes).fetchall()
        return {row[0] for row in results}
    except sqlite3.Error as e:
        logger.error(f"Batch query failed: {e}")
        return set()

def mark_uploaded(conn, fhash, fname, size, local_path=""):
    if conn is None:
        return
    try:
        conn.execute("REPLACE INTO uploads VALUES(?,CURRENT_TIMESTAMP,?,?,?)", 
                    (fhash, fname, size, local_path))
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Failed to mark uploaded: {e}")
        try:
            conn.rollback()
        except:
            pass

def get_uploaded_files(conn):
    """Get all successfully uploaded files with their local paths"""
    if conn is None:
        return []
    try:
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
        result = conn.execute(
            "SELECT 1 FROM uploads WHERE local_path=? AND filename NOT LIKE 'IN_FLIGHT_%' AND filename NOT LIKE 'SKIPPED_%'",
            (local_path,)
        ).fetchone()
        return bool(result)
    except sqlite3.Error as e:
        logger.error(f"DB check failed: {e}")
        return False

def get_stats(conn):
    if conn is None:
        return 0, 0
    try:
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
    candidates = [
        Path.home()/"Pictures"/"iCloud Photos"/"Photos",
        Path.home()/"Pictures"/"iCloud Photos",
        Path.home()/"Pictures",
    ]
    for c in candidates:
        if c.exists(): return str(c)
    return str(Path.home()/"Pictures")

# ── File hashing (fix #3) ──────────────────────────────────────────────
_hash_cache = {}
_hash_cache_lock = threading.Lock()

def compute_file_hash(filepath, use_cache=True):
    """Compute MD5 hash of file content"""
    if use_cache:
        with _hash_cache_lock:
            if filepath in _hash_cache:
                return _hash_cache[filepath]
    
    try:
        hash_obj = hashlib.md5()
        with open(filepath, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                hash_obj.update(chunk)
        result = hash_obj.hexdigest()
        
        if use_cache:
            with _hash_cache_lock:
                _hash_cache[filepath] = result
        
        return result
    except (OSError, IOError) as e:
        logger.error(f"Failed to hash file: {e}")
        return None

# ── File system watcher (fix #1) ───────────────────────────────────────
pending_files = set()
pending_files_lock = threading.Lock()

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

# ── Instant Delete After Backup (NEW - v2.2.2) ─────────────────────────
def try_delete_file_after_backup(conn, local_path: str, backup_folder: str) -> bool:
    """
    Safely delete a file after successful backup with comprehensive checks.
    
    Returns True if deletion succeeded, False otherwise.
    """
    if not local_path or not os.path.exists(local_path):
        return False
    
    try:
        # ✅ Check 1: File must be in DB as successfully uploaded
        if not is_file_in_db(conn, local_path):
            logger.warning(f"[DELETE] File not confirmed in DB: {local_path}")
            return False
        
        # ✅ Check 2: File extension must be supported
        file_ext = os.path.splitext(local_path)[1].lower()
        if file_ext not in ALL_EXT:
            logger.warning(f"[DELETE] Unsupported extension: {local_path}")
            return False
        
        # ✅ Check 3: Path must be within backup folder (path traversal check)
        local_resolved = Path(local_path).resolve()
        backup_resolved = Path(backup_folder).resolve()
        if not str(local_resolved).startswith(str(backup_resolved)):
            logger.error(f"[DELETE] Path traversal attempt blocked: {local_path}")
            return False
        
        # ✅ Check 4: File still exists
        if not os.path.exists(local_path):
            logger.info(f"[DELETE] File already deleted: {local_path}")
            return False
        
        # ✅ All checks passed - delete the file
        os.remove(local_path)
        push_log(f"🗑️  Deleted from iCloud: {os.path.basename(local_path)}")
        state["deleted_count"] += 1
        logger.info(f"[DELETE] Successfully deleted: {local_path}")
        return True
        
    except PermissionError:
        logger.warning(f"[DELETE] Permission denied: {local_path}")
        push_log(f"⚠️  Could not delete (permission denied): {os.path.basename(local_path)}")
        return False
    except FileNotFoundError:
        logger.info(f"[DELETE] File already deleted: {local_path}")
        return False
    except Exception as e:
        logger.error(f"[DELETE] Error deleting {local_path}: {e}")
        push_log(f"⚠️  Could not delete: {os.path.basename(local_path)} ({type(e).__name__})")
        return False

# ── Storage cleanup ────────────────────────────────────────────────────
def cleanup_icloud_storage(conn, cleanup_older_than_days=30):
    """Clean up backed-up files from iCloud storage after confirmation"""
    if conn is None:
        return 0, 0
    
    try:
        uploaded_files = get_uploaded_files(conn)
        cutoff_time = time.time() - (cleanup_older_than_days * 86400)
        deleted_count = 0
        deleted_size = 0
        failed_files = []
        
        for uuid, filename, file_size, local_path in uploaded_files:
            if not local_path:
                continue
            
            try:
                file_path = Path(local_path)
                if not file_path.exists():
                    push_log(f"⏭️  Skipped (not found): {filename}")
                    continue
                
                if file_path.stat().st_mtime > cutoff_time:
                    push_log(f"⏭️  Too new to delete: {filename}")
                    continue
                
                file_path.unlink()
                deleted_count += 1
                deleted_size += file_size or 0
                push_log(f"🗑️  Deleted backed-up file: {filename}")
                
            except PermissionError:
                failed_files.append((filename, "Permission denied"))
                push_log(f"⚠️  Permission denied: {filename}")
            except FileNotFoundError:
                push_log(f"⏭️  Already deleted: {filename}")
            except Exception as e:
                failed_files.append((filename, str(e)))
                push_log(f"⚠️  Failed to delete {filename}: {e}")
        
        if deleted_count > 0:
            push_log(f"✅ Cleanup complete: {deleted_count} files deleted ({fmt_size(deleted_size)})")
            state["cleanup_count"] += deleted_count
        
        if failed_files:
            push_log(f"⚠️  {len(failed_files)} files could not be deleted")
        
        return deleted_count, deleted_size
    
    except Exception as e:
        push_log(f"❌ Cleanup failed: {e}")
        return 0, 0

# ── Auto-updater ──────────────────────────────────────────────────────
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
        channel_id  = int(cfg["channel_id"])
    except (ValueError, KeyError):
        push_log("❌ Invalid channel ID")
        state["status"] = "stopped"
        return

    photos_path = cfg.get("photos_path", "")
    
    # Security Fix #2: Validate path
    if not validate_path(photos_path):
        push_log(f"❌ Invalid photos path: {photos_path}")
        state["status"] = "stopped"
        return
    
    if not Path(photos_path).exists():
        push_log(f"❌ Photos folder not found: {photos_path}")
        state["status"] = "stopped"
        return

    api_id      = int(cfg.get("api_id", TELEGRAM_API_ID))
    api_hash    = cfg.get("api_hash", TELEGRAM_API_HASH)
    cleanup_after_backup = cfg.get("cleanup_after_backup", False)
    delete_after_backup = cfg.get("delete_after_backup", False)
    
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
        push_log("❌ Not authorized. Please log in first.")
        state["status"] = "stopped"
        await client.disconnect()
        stop_file_watcher()
        return

    sem    = asyncio.Semaphore(2)
    conn   = db_pool.get_connection()
    if conn is None:
        push_log("❌ Cannot connect to database")
        state["status"] = "stopped"
        await client.disconnect()
        stop_file_watcher()
        return
    
    export = Path(tempfile.gettempdir()) / "tele_backup_export"
    try:
        export.mkdir(exist_ok=True)
    except Exception as e:
        push_log(f"⚠️ Failed to create temp directory: {e}")
        export = Path(tempfile.gettempdir())
    
    push_log(f"✅ Connected! Watching: {photos_path}")
    state["cleanup_enabled"] = cleanup_after_backup
    state["delete_after_backup_enabled"] = delete_after_backup

    try:
        while state["status"] == "running":
            try:
                files_to_check = []
                
                with pending_files_lock:
                    files_to_check = list(pending_files)
                    pending_files.clear()
                
                if len(files_to_check) == 0:
                    try:
                        for root, dirs, fnames in os.walk(photos_path):
                            dirs[:] = [d for d in dirs if not d.startswith('.')]
                            for fn in fnames:
                                if fn.lower().endswith(ALL_EXT):
                                    files_to_check.append(os.path.join(root, fn))
                    except PermissionError as e:
                        push_log(f"⚠️ Permission error scanning files: {e}")
                    except Exception as e:
                        push_log(f"⚠️ Error scanning files: {e}")
                
                now = time.time()
                tasks = []
                
                file_hashes = {}
                for fp in files_to_check:
                    try:
                        sz = os.path.getsize(fp)
                        mtime = os.path.getmtime(fp)
                    except OSError:
                        continue
                    
                    if now - mtime < 3:
                        continue
                    
                    fhash = compute_file_hash(fp, use_cache=True)
                    if fhash:
                        file_hashes[fp] = fhash
                
                if not file_hashes:
                    if cleanup_after_backup:
                        await asyncio.sleep(1)
                        cleanup_icloud_storage(conn, cleanup_days)
                    
                    push_log("🔍 No new files. Waiting 15s…")
                    await asyncio.sleep(15)
                    continue
                
                hashes_list = list(file_hashes.values())
                uploaded_set = are_uploaded_batch(conn, hashes_list)
                
                for fp, fhash in file_hashes.items():
                    if fhash in uploaded_set:
                        continue
                    
                    try:
                        sz = os.path.getsize(fp)
                    except OSError:
                        continue
                    
                    fname = os.path.basename(fp)
                    tasks.append(_upload_one(client, conn, channel_id, fp, sz, fhash, sem, export, fp, photos_path, delete_after_backup))

                if tasks:
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    ok = sum(1 for r in results if r is True)
                    if ok: 
                        push_log(f"✅ Uploaded {ok} new file(s).")
                        if cleanup_after_backup:
                            await asyncio.sleep(2)
                            cleanup_icloud_storage(conn, cleanup_days)
                
                cnt, raw = get_stats(conn)
                state["count"]    = cnt
                state["size_str"] = fmt_size(raw)

            except FloodWaitError as e:
                push_log(f"⏳ Flood wait {e.seconds}s")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                push_log(f"⚠️ Error: {e}")

            await asyncio.sleep(15)

    finally:
        await client.disconnect()
        db_pool.return_connection(conn)
        stop_file_watcher()
        push_log("🛑 Daemon stopped.")

async def _upload_one(client, conn, channel_id, fp, sz, fhash, sem, export_dir, local_path="", backup_folder="", delete_after_backup=False):
    fname = os.path.basename(fp)
    if is_uploaded(conn, fhash):
        return False
    
    mark_uploaded(conn, fhash, f"IN_FLIGHT_{fname}", sz, local_path)
    tmp = None
    async with sem:
        try:
            ts   = int(time.time()*1000)
            tmp  = export_dir / f"{ts}_{fname}"
            
            await asyncio.get_event_loop().run_in_executor(thread_pool, shutil.copy2, fp, tmp)
            
            date_str = datetime.fromtimestamp(os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M")
            ext  = os.path.splitext(fname)[1].upper().lstrip('.')
            cap  = f"📁 {fname}\n📅 {date_str}\n🏷 {ext}  •  {fmt_size(sz)}"
            push_log(f"⬆️  Uploading {fname}…")
            await client.send_file(channel_id, str(tmp), caption=cap, force_document=True)
            mark_uploaded(conn, fhash, fname, sz, local_path)
            
            # ✅ NEW: If delete_after_backup enabled, try to delete file immediately
            if delete_after_backup and backup_folder:
                try_delete_file_after_backup(conn, local_path, backup_folder)
            
            return True
        except Exception as e:
            push_log(f"❌ Failed {fname}: {e}")
            try:
                conn.execute("DELETE FROM uploads WHERE uuid=?", (fhash,))
                conn.commit()
            except:
                pass
            return False
        finally:
            if tmp and tmp.exists():
                try:
                    tmp.unlink()
                except Exception:
                    pass

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
    from telethon import TelegramClient
    client = TelegramClient(SESSION_FILE, TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await client.connect()
    await client.sign_in(_login_state["phone"], code, phone_code_hash=_login_state["hash"])
    me = await client.get_me()
    state["authorized"] = True
    await client.disconnect()
    return me.first_name

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
                self.send_html(DASHBOARD_HTML)
            elif path == "/api/state":
                self.send_json({**state, "logs": list(state["logs"])[-50:]})
            elif path == "/api/config":
                self.send_json(load_config())
            elif path == "/api/detect_icloud":
                self.send_json({"path": detect_icloud()})
            elif path == "/api/update_info":
                self.send_json({**update_state, "current": APP_VERSION})
            else:
                self.send_response(404)
                self.end_headers()
        except Exception as e:
            logger.error(f"GET error: {e}")
            self.send_json({"error": "Internal error"}, 500)

    def do_POST(self):
        try:
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
                code = body.get("code","").strip()
                try:
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
  <h1>📸 Telegram Backup Pro v2.2.3</h1>
  <p class="sub">Backup your photos & videos to Telegram — automatically.<br><span style="color:#58a6ff;font-weight:600;">Developed by Shithel</span></p>

  <div id="update-banner" style="display:none;background:#1c2a14;border:1px solid #3fb950;border-radius:10px;padding:12px 18px;margin-bottom:16px;display:none;align-items:center;gap:12px;">
    <span style="font-size:1.1rem;">🆕</span>
    <span id="update-text" style="flex:1;font-size:.9rem;"></span>
    <a id="update-link" href="#" target="_blank" style="background:#238636;color:#fff;padding:7px 16px;border-radius:6px;text-decoration:none;font-size:.85rem;font-weight:600;">Download Update</a>
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
      <button class="btn-blue" onclick="sendOtp()">Send Login Code →</button>
      <div id="otp-row" style="display:none;margin-top:16px">
        <div class="row"><label>Enter OTP</label><input id="otp" placeholder="Code from Telegram" /></div>
        <button class="btn-primary" onclick="verifyOtp()">✓ Verify Code</button>
      </div>
      <div id="login-msg"></div>
    </div>
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
  const r=await fetch(path,{method,headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
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
    setMsg('login-msg','✅ Logged in as '+r.name+'! Go to Settings to set your folder, then start backup.','ok');
  } else {
    setMsg('login-msg','❌ '+(r.error||'Error'),'err');
  }
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

async function checkUpdate(){
  try{
    const u=await api('GET','/api/update_info');
    if(u.available){
      const b=document.getElementById('update-banner');
      document.getElementById('update-text').textContent=`New version v${u.latest} is available! (You have v${u.current})`;
      document.getElementById('update-link').href=u.url;
      b.style.display='flex';
    }
  }catch(e){}
}
checkUpdate();
setInterval(checkUpdate,300000);
</script>
</body>
</html>"""

# ── Entry point ────────────────────────────────────────────────────────
if __name__ == "__main__":
    cfg = load_config()

    if Path(SESSION_FILE).exists():
        state["authorized"] = True

    if cfg.get("auto_start_backup") and cfg.get("channel_id") and cfg.get("photos_path"):
        state["status"] = "running"
        push_log("🚀 Auto-starting backup...")
        threading.Thread(target=start_daemon, daemon=True).start()

    print(f"\n{'='*50}")
    print(f"  Telegram Backup Pro v{APP_VERSION}")
    print(f"  🔒 Security-hardened build")
    print(f"  Open: http://localhost:{PORT}")
    print(f"{'='*50}\n")

    if "--silent" not in sys.argv:
        import webbrowser
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()

    threading.Thread(target=_update_check_loop, daemon=True).start()

    server = HTTPServer(("127.0.0.1", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        db_pool.close_all()
        if observer:
            stop_file_watcher()
