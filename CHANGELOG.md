# Changelog

All notable changes to Telegram Backup Pro are documented in this file.

## [3.1.0] — 2026-05-07

### 🔴 Critical Fixes
- **FIX 1 — 2FA / Two-Step Verification**: Login flow now handles `SessionPasswordNeededError`. When 2FA is enabled the UI shows a password prompt; `POST /api/verify_2fa` completes the sign-in using `client.sign_in(password=...)`. `PasswordHashInvalidError` surfaces a clean error message.
- **FIX 2 — iCloud Placeholder Corruption**: Files ending in `.icloud`, files with Windows attributes `OFFLINE | RECALL_ON_DATA_ACCESS | RECALL_ON_OPEN`, and images/videos under 10 KB are all skipped before hashing. Prevents 4 KB stub files from being uploaded as corrupt backups.
- **FIX 3 — CSRF Token on POST Endpoints**: A random `secrets.token_hex(16)` token is generated at startup and injected into the dashboard HTML. Every `fetch()` POST includes `X-Backup-Token`. Server rejects any request without a matching token with `403 Unauthorized`.

### 🟠 High-Priority Fixes
- **FIX 4 — Log Rotation**: Replaced `FileHandler` with `RotatingFileHandler` (5 MB × 3 backups = max 15 MB total). Log files no longer grow unbounded.
- **FIX 5 — Temp File Cleanup**: Upload temp directory moved from system temp to `APPDATA/upload_temp`. `cleanup_temp_on_startup()` removes leftover files from crashed sessions. `check_temp_disk_usage()` warns if the directory exceeds 500 MB.
- **FIX 6 — LRU Hash Cache**: `_hash_cache` replaced with `LRUHashCache(maxsize=10000)` backed by `OrderedDict`. Evicts oldest entries when the limit is hit, preventing unbounded RAM growth. Hash chunk size increased from 8 KB → 64 KB (8× faster on large videos).

### 🟡 UX Improvements
- **FIX 7 — Notifications**: `send_windows_notification()` fires a Windows 10/11 toast via PowerShell when backup completes or stops unexpectedly. Browser `Notification` permission is requested on page load; a browser notification fires when status transitions from `running` → `stopped`.
- **Logout Button**: Added `POST /api/logout` endpoint and a red "🔓 Logout from Telegram" button on the Login tab. Deletes the `.session` file and resets login state.
- **Placeholders Skipped Stat**: New stat card on the Dashboard shows how many iCloud placeholder files were skipped.
- **Localhost security note**: Footer on the Login tab reminds users the dashboard is only accessible from this computer.

---

## [3.0.0] — 2026-05-07

### Bug Fixes (10)
- **BUG 1**: `validate_path()` rewritten — accepts any absolute readable path; `detect_icloud()` searches all known Windows iCloud locations; `try_delete_file_after_backup()` falls back to hash-based DB check.
- **BUG 2A**: `clean_inflight_entries()` removes stuck `IN_FLIGHT_*` rows on startup.
- **BUG 2B**: `compute_file_hash()` skips `.icloud` placeholders and sub-1 KB stubs; mtime-aware cache prevents stale hashes.
- **BUG 2C**: `_uploading_now` set + `_uploading_lock` prevents concurrent duplicate uploads.
- **BUG 2D**: Full `os.walk` scan throttled to once per 60 seconds.
- **BUG 3**: `do_self_update()` downloads the new `.exe` and replaces the running process via a batch script.
- **BUG 4**: Daemon drains watcher events first, then merges periodic walk — no more ping-pong.
- **BUG 5**: 5-second cooldown after each upload batch eliminates CPU spin.
- **BUG 6**: Delete-after-backup yields and verifies by hash before deleting.
- **BUG 7**: SQLite uses `check_same_thread=False` + global `_db_lock`.
- **BUG 8**: Auto-start checks `is_session_valid()` — skipped if not logged in.
- **BUG 9**: `cleanup_icloud_storage()` skips files outside the current backup folder.
- **BUG 10**: Update banner replaced with `POST /api/do_update` button; non-frozen builds show git instructions.

### New Features
- **Upload Retry**: Exponential backoff (3 attempts: 5 s, 15 s, 45 s) for `ConnectionError`/`TimeoutError`/`OSError`.
- **Failed Uploads DB**: `failed_uploads` table + `GET /api/failed_files` + per-file Retry / Retry All UI.
- **Real Upload Progress**: Telethon `progress_callback` reports every 10%; animated progress bars in the Dashboard.

---

## [2.2.3] — 2026-05-06

- Bundled `watchdog` in the PyInstaller `.exe` (fixed crash on launch).

## [2.2.2] — 2026-05-06

- Instant delete-after-backup feature.
- SQL injection prevention, path traversal protection, input validation.
- Event loop cleanup, database error handling, resource management.
