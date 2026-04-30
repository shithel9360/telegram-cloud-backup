# Security Policy

## Supported Versions

| Version | Status | Security Updates |
|---------|--------|------------------|
| 2.2.1   | Current | ✅ Active |
| 2.2.0   | Superseded | ⚠️ Limited |
| < 2.2.0 | Unsupported | ❌ No |

---

## Reporting Security Vulnerabilities

**IMPORTANT:** Do not report security issues via GitHub Issues!

If you discover a security vulnerability, please report it to:

📧 **Email:** security-telegram-backup@protonmail.com

Please include:
- Description of the vulnerability
- Steps to reproduce
- Affected version(s)
- Potential impact
- Suggested fix (if available)

We will acknowledge your report within **24 hours** and provide updates every **48 hours** on progress.

---

## Known Security Measures

### Implemented (v2.2.1+)
✅ **SQL Injection Prevention** - All queries use parameterized statements  
✅ **Path Traversal Protection** - User paths are validated  
✅ **Input Validation** - Phone, channel ID, and config values are validated  
✅ **Payload Size Limits** - Max 1MB request size  
✅ **JSON Validation** - Malformed JSON is rejected  
✅ **Memory Leak Prevention** - All resources properly cleaned up  
✅ **Error Handling** - Graceful failures, detailed logging  

### Best Practices
- Session files stored locally only (`.tele_backup_session`)
- No data sent to third-party servers
- Configuration stored in user home directory only
- All file operations validated

---

## Privacy & Data Protection

- ✅ No telemetry or analytics
- ✅ No data collection
- ✅ Open source - you can audit the code
- ✅ Files uploaded directly to your Telegram channel
- ✅ No intermediate storage

---

## Dependency Security

We use:
- **telethon** - Official Telegram client library (actively maintained)
- **watchdog** - File system monitoring (popular, well-maintained)

These dependencies are regularly updated. Check `requirements.txt` for versions.

---

## Update Policy

- Security patches released ASAP
- Version bumped as needed
- Updates announced in Releases tab
- Changelog documents all changes

---

## Questions?

If you have security concerns or questions, email: security-telegram-backup@protonmail.com

Thank you for helping keep this project secure! 🔒
