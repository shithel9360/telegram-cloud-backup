# Changelog

All notable changes to this project will be documented in this file.

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

## [2.2.0] - Initial Release

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
