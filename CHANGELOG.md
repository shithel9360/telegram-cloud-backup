# Changelog

All notable changes to this project will be documented in this file.

## [2.2.2] - 2026-05-06

### ✨ Features
- **Instant Delete After Backup** - NEW! Immediately delete files from iCloud after successful Telegram upload
- New Settings checkbox: "⚡ Delete from iCloud immediately after successful backup"
- Dashboard banner shows when instant delete is enabled
- Live log provides per-file feedback (✅ uploaded, 🗑️ deleted, ⚠️ errors)

### 🔒 Security & Safety
- Comprehensive safety checks before deletion:
  - ✅ File must be confirmed in SQLite as successfully uploaded
  - ✅ Extension must be supported media type (images/videos only)
  - ✅ Path must be within backup folder (no directory escape)
  - ✅ File must still exist on disk
- Permission error handling (skips locked files gracefully)
- Only deletes after confirmed successful upload (failed uploads NOT deleted)
- Feature is disabled by default (opt-in)

### 🐛 Bug Fixes & Improvements
- Enhanced error handling for edge cases
- Better logging for deletion operations
- Improved state tracking (deleted_count counter)

### 📝 Documentation
- Updated version to 2.2.2
- Added comprehensive comments in deletion logic
- Documented all safety checks

---

## [2.2.1] - 2026-04-30

### 🔒 Security
- **Fixed SQL Injection**: Converted all LIKE query operations to use parameterized queries
- **Added Path Traversal Protection**: New `validate_path()` function prevents directory escape attacks
- **Input Validation**: Added strict validation for phone numbers, channel IDs, and file paths

### 🐛 Bug Fixes
- **Event Loop Memory Leak**: Fixed unclosed asyncio event loops in OTP login handler
- **Database Connection Leak**: Daemon event loop now properly closes on exit
- **Config File Parsing**: Added error recovery for corrupted JSON config files
- **DB Error Handling**: All database operations now catch and handle SQLite errors gracefully
- **Directory Scanning**: Protected against permission errors during file system walks
- **File Deletion**: Enhanced error handling in storage cleanup function
- **HTTP Request Parsing**: Added protection against malformed JSON/missing headers

### ✨ Improvements
- **Better Error Messages**: Users now see specific validation errors in UI
- **Comprehensive Logging**: All exceptions now logged with context for debugging
- **Config Validation**: Server-side validation on cleanup_days (1-365 range)
- **Code Quality**: Added None checks on all database connections

### 📝 Documentation
- Added `BUGFIXES.md` with detailed explanation of all fixes
- Updated version to 2.2.1

---

## [2.2.0] - 2026-04-30

### ✨ Features
- Zero-setup portable Windows application
- Web-based dashboard UI
- Auto-detect iCloud Photos on Windows
- Upload files as documents (preserves quality)
- Smart resume - remembers uploaded files
- Fully private - no third-party servers
- Auto-start on Windows boot
- Auto-cleanup of backed-up files
- Live backup logs
- Update checker

---

**Note:** v2.2.0 and v2.2.1 are superseded by v2.2.2. Please upgrade to v2.2.2 for the instant delete feature and security improvements.
