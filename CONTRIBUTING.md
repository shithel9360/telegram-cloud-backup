# Contributing to Telegram Backup Pro

Thank you for your interest in contributing! Here's how you can help.

## Code Standards

- **Python Version:** 3.8+
- **Style:** Follow PEP 8
- **Comments:** Add comments for complex logic
- **Type Hints:** Use type hints where possible
- **Error Handling:** Always include try/except for I/O operations

## Security First

- ⛔ Never commit credentials, API keys, or session files
- ✅ Always validate user input
- ✅ Use parameterized queries for database operations
- ✅ Test path validation with malicious inputs
- ✅ Report security issues privately to: security-telegram-backup@protonmail.com

## Testing

Before submitting:

```bash
# Install dependencies
pip install -r requirements.txt

# Run the app
python app_web.py

# Test all features:
# 1. Login flow
# 2. Settings save/load
# 3. File detection
# 4. Upload simulation
# 5. Error handling
```

## Pull Request Process

1. **Fork** the repository
2. **Create** a feature branch: `git checkout -b feature/your-feature`
3. **Commit** with clear messages: `git commit -m "Add feature: ..."`
4. **Push** to your fork
5. **Create** a pull request with:
   - Clear description of changes
   - Why this change is needed
   - Any related issues

## Bug Reports

When reporting bugs, include:
- Your OS version
- Steps to reproduce
- Expected behavior
- Actual behavior
- Relevant logs from `%APPDATA%/TelegramBackupPro/backup.log`

## Feature Requests

For new features, describe:
- Use case
- How it would work
- Any security/performance implications

## Code Review

All submissions will be reviewed for:
- ✅ Security (no vulnerabilities)
- ✅ Performance (no memory leaks)
- ✅ Code quality (readable and maintainable)
- ✅ Tests (functionality verified)

## License

By contributing, you agree your code will be licensed under the same license as the project.

---

**Questions?** Open an [Issue](../../issues/new) and we'll help!

Happy coding! 🚀
