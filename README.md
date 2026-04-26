# 📸 Telegram Backup Pro

**Developed by Shithel**

A zero-setup, portable application to automatically back up your photos and videos (from iCloud, Google Drive, or any local folder) directly to a private Telegram channel. 

Built for Windows. No installation required.

---

## ✨ Features

- **Zero Installation:** Runs from a single `.exe` file. No need to install Python or any dependencies.
- **Web-Based UI:** Beautiful, easy-to-use dashboard right in your browser.
- **Auto-Detect iCloud:** Automatically finds your Apple iCloud Photos folder on Windows.
- **Original Quality:** Uploads files as Documents to preserve 100% original quality (no Telegram compression).
- **Smart Resume:** Remembers what was uploaded. If you stop and start, it won't upload duplicates.
- **Fully Private:** Your photos go directly from your PC to your private Telegram channel. No third-party servers.

---

## 🚀 How to Use (Step-by-Step)

### Step 1: Download
1. Go to the **[Releases](../../releases/latest)** page.
2. Download the `TelegramBackup.exe` file.

### Step 2: Run the App
1. Double-click `TelegramBackup.exe`.
2. A small black window will open (this is the background engine running).
3. Your web browser will automatically open the App Dashboard.

### Step 3: Connect to Telegram
1. In the browser, go to the **Telegram Login** tab.
2. Enter your Phone Number (e.g., `+8801XXXXXXXXX`).
3. Enter your Target **Channel ID** (e.g., `-100123456789`). 
   *Tip: Forward any message from your private channel to `@userinfobot` on Telegram to get its ID.*
4. Click **Send Login Code** and enter the code sent to your Telegram app.

### Step 4: Choose Backup Folder
1. Go to the **Settings** tab.
2. Click **Auto-Detect iCloud** to automatically find your Apple photos, OR manually type the path to any folder.
3. Click **Save Settings**.

### Step 5: Start Backup!
1. Go to the **Dashboard** tab.
2. Click **▶ Start Backup**.
3. You will see the live logs showing your files being uploaded to your Telegram channel!

---

## 🛠️ Troubleshooting

- **Black window closes immediately:** Make sure you are connected to the internet.
- **Browser didn't open:** Open your browser manually and go to `http://localhost:7878`
- **Is it safe?** Yes! This is an open-source tool. Your login session is saved locally on your computer in a file called `~/.tele_backup_session`. 
