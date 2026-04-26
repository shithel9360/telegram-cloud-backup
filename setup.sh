#!/bin/bash

echo "🚀 Installing dependencies for iOS Telegram Backup System..."

# Ensure pip is installed
if ! command -v pip3 &> /dev/null
then
    echo "❌ Python pip is not installed. Please install Python 3."
    exit 1
fi

# Install required Python packages
pip3 install telethon osxphotos

echo "✅ Dependencies installed!"
echo "➡️ Please run the GUI configuration tool to setup your credentials:"
echo "python3 /Users/shithel/.gemini/antigravity/scratch/ios_telegram_backup/app.py"
