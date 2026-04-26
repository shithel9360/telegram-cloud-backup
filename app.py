import os
import sys
import json
import sqlite3
import subprocess
import tkinter as tk
from tkinter import messagebox, scrolledtext

# Paths
CONFIG_FILE = os.path.expanduser("~/.tele_backup_config.json")
DB_FILE     = os.path.expanduser("~/.tele_backup_state.db")
PLIST_FILE  = os.path.expanduser("~/Library/LaunchAgents/com.user.telegrambackup.plist")
LOG_FILE    = os.path.expanduser("~/Library/Logs/TelegramPhotosBackup/launchd.err")
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
SERVICE_SCRIPT = os.path.join(SCRIPT_DIR, "backup_service.py")
PYTHON_EXEC = sys.executable
PHOTOS_ORIGINALS = os.path.expanduser("~/Pictures/Photos Library.photoslibrary/originals")


def format_size(b):
    if b >= 1024**3: return f"{b/1024**3:.2f} GB"
    if b >= 1024**2: return f"{b/1024**2:.1f} MB"
    if b >= 1024:    return f"{b/1024:.1f} KB"
    return f"{b} B"


def get_db_stats():
    if not os.path.exists(DB_FILE):
        return 0, 0
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT COUNT(*), SUM(file_size) FROM uploads WHERE filename NOT LIKE 'SKIPPED%'")
        row = c.fetchone()
        conn.close()
        return row[0] or 0, row[1] or 0
    except Exception:
        return 0, 0


class TelegramBackupGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Telegram iCloud Backup")
        self.root.geometry("440x560")
        self.root.resizable(False, False)

        self.config_vars = {
            "api_id":     tk.StringVar(),
            "api_hash":   tk.StringVar(),
            "channel_id": tk.StringVar(),
        }

        self.build_ui()
        self.load_config()

    def build_ui(self):
        # ── Title ──────────────────────────────────────────────────────────
        tk.Label(self.root, text="📸 Telegram iCloud Backup",
                 font=("Helvetica", 15, "bold")).pack(pady=(12, 4))
        tk.Label(self.root, text="Automatic backup of iPhone photos to Telegram",
                 fg="gray", font=("Helvetica", 10)).pack()

        # ── Config frame ───────────────────────────────────────────────────
        frame = tk.LabelFrame(self.root, text=" Configuration ", padx=10, pady=8)
        frame.pack(padx=16, pady=10, fill="x")

        labels = {
            "api_id":     "Telegram API ID:",
            "api_hash":   "Telegram API Hash:",
            "channel_id": "Channel ID (e.g. -1001234567890):",
        }

        for key, text in labels.items():
            tk.Label(frame, text=text, anchor="w").pack(fill="x", pady=(4, 0))
            show = "*" if key == "api_hash" else ""
            tk.Entry(frame, textvariable=self.config_vars[key], show=show).pack(fill="x", pady=(0, 4))

        tk.Label(frame, text="Get API credentials at: my.telegram.org",
                 fg="#3366cc", font=("Helvetica", 9), cursor="hand2").pack(anchor="w")

        # ── Stats bar ──────────────────────────────────────────────────────
        self.stats_label = tk.Label(self.root, text="", fg="#555555",
                                    font=("Helvetica", 10))
        self.stats_label.pack(pady=(2, 0))
        self.refresh_stats()

        # ── Buttons ────────────────────────────────────────────────────────
        btn_frame = tk.Frame(self.root)
        btn_frame.pack(pady=10)

        buttons = [
            ("💾 Save Config",       self.save_config,     0, 0),
            ("⚙️ Install Service",    self.install_launchd, 0, 1),
            ("▶ Run Now",            self.run_now,         1, 0),
            ("🔄 Restart Service",   self.restart_launchd, 1, 1),
            ("⏹ Stop Service",       self.stop_launchd,    2, 0),
            ("📄 View Logs",         self.view_logs,       2, 1),
        ]
        for label, cmd, r, c in buttons:
            tk.Button(btn_frame, text=label, command=cmd, width=18,
                      pady=4).grid(row=r, column=c, padx=5, pady=4)

        # ── Status bar ─────────────────────────────────────────────────────
        self.status_label = tk.Label(self.root, text="Service Status: checking…",
                                     fg="blue", font=("Helvetica", 11))
        self.status_label.pack(side="bottom", pady=8)
        self.check_status()

    # ── Config ──────────────────────────────────────────────────────────────
    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    config = json.load(f)
                for key in self.config_vars:
                    if key in config:
                        self.config_vars[key].set(config[key])
            except Exception as e:
                messagebox.showerror("Error", f"Could not load config: {e}")

    def save_config(self):
        config = {key: var.get().strip() for key, var in self.config_vars.items()}
        if not all(config.values()):
            messagebox.showwarning("Incomplete", "Please fill out all fields.")
            return
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(config, f, indent=4)
            messagebox.showinfo("Saved", "Configuration saved successfully!")
        except Exception as e:
            messagebox.showerror("Error", f"Could not save config: {e}")

    # ── Plist generation (up-to-date with WatchPaths + KeepAlive) ───────────
    def generate_plist(self):
        photos_db = os.path.expanduser("~/Pictures/Photos Library.photoslibrary/database")
        cloudsync  = os.path.expanduser("~/Pictures/Photos Library.photoslibrary/resources/cpl/cloudsync.noindex")
        log_err    = os.path.expanduser("~/Library/Logs/TelegramPhotosBackup/launchd.err")
        log_out    = os.path.expanduser("~/Library/Logs/TelegramPhotosBackup/launchd.out")
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.telegrambackup</string>
    <key>ProgramArguments</key>
    <array>
        <string>{PYTHON_EXEC}</string>
        <string>{SERVICE_SCRIPT}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>WatchPaths</key>
    <array>
        <string>{PHOTOS_ORIGINALS}</string>
        <string>{photos_db}</string>
        <string>{cloudsync}</string>
    </array>
    <key>StartInterval</key>
    <integer>60</integer>
    <key>KeepAlive</key>
    <dict>
        <key>Crashed</key>
        <true/>
    </dict>
    <key>StandardErrorPath</key>
    <string>{log_err}</string>
    <key>StandardOutPath</key>
    <string>{log_out}</string>
</dict>
</plist>
"""

    # ── Service control ─────────────────────────────────────────────────────
    def install_launchd(self):
        try:
            os.makedirs(os.path.dirname(PLIST_FILE), exist_ok=True)
            os.makedirs(os.path.expanduser("~/Library/Logs/TelegramPhotosBackup"), exist_ok=True)
            with open(PLIST_FILE, 'w') as f:
                f.write(self.generate_plist())
            subprocess.run(['launchctl', 'unload', PLIST_FILE], capture_output=True)
            res = subprocess.run(['launchctl', 'load', '-w', PLIST_FILE],
                                 capture_output=True, text=True)
            if res.returncode == 0:
                messagebox.showinfo("Installed", "Service installed and started!\n\nIt will now automatically backup new photos to Telegram.")
                self.check_status()
            else:
                messagebox.showerror("Error", f"Failed to load service:\n{res.stderr}")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def restart_launchd(self):
        if not os.path.exists(PLIST_FILE):
            messagebox.showwarning("Not Installed", "Click 'Install Service' first.")
            return
        subprocess.run(['launchctl', 'unload', PLIST_FILE], capture_output=True)
        res = subprocess.run(['launchctl', 'load', '-w', PLIST_FILE],
                             capture_output=True, text=True)
        if res.returncode == 0:
            messagebox.showinfo("Restarted", "Service restarted successfully.")
            self.check_status()
        else:
            messagebox.showerror("Error", f"Failed:\n{res.stderr}")

    def stop_launchd(self):
        if os.path.exists(PLIST_FILE):
            res = subprocess.run(['launchctl', 'unload', '-w', PLIST_FILE],
                                 capture_output=True, text=True)
            if res.returncode == 0:
                messagebox.showinfo("Stopped", "Service stopped.")
            else:
                messagebox.showerror("Error", f"Failed:\n{res.stderr}")
        else:
            messagebox.showinfo("Info", "Service is not installed.")
        self.check_status()

    def run_now(self):
        """Run a backup immediately in background."""
        try:
            subprocess.Popen(
                [PYTHON_EXEC, SERVICE_SCRIPT],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            messagebox.showinfo("Running", "Backup started in background!\nCheck logs for progress.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def view_logs(self):
        if os.path.exists(LOG_FILE):
            # Show last 60 lines in a popup
            win = tk.Toplevel(self.root)
            win.title("Recent Logs")
            win.geometry("700x400")
            txt = scrolledtext.ScrolledText(win, font=("Courier", 10), wrap="none")
            txt.pack(fill="both", expand=True, padx=8, pady=8)
            try:
                with open(LOG_FILE, 'r') as f:
                    lines = f.readlines()
                txt.insert("end", "".join(lines[-60:]))
                txt.see("end")
                txt.config(state="disabled")
            except Exception as e:
                txt.insert("end", f"Error reading log: {e}")
        else:
            messagebox.showinfo("Logs", "Log file doesn't exist yet. Run the service first.")

    # ── Status bar ──────────────────────────────────────────────────────────
    def check_status(self):
        result = subprocess.run(['launchctl', 'list', 'com.user.telegrambackup'],
                                capture_output=True, text=True)
        if result.returncode == 0:
            self.status_label.config(text="✅ Service: Running & Watching", fg="#1a7f37")
        else:
            self.status_label.config(text="❌ Service: Not Running", fg="red")
        self.root.after(5000, self.check_status)

    def refresh_stats(self):
        count, size = get_db_stats()
        if count:
            self.stats_label.config(text=f"📦 Total backed up: {count} files  ({format_size(size)})")
        else:
            self.stats_label.config(text="📦 No backups yet")
        self.root.after(10000, self.refresh_stats)


if __name__ == "__main__":
    os.makedirs(os.path.expanduser("~/Library/Logs/TelegramPhotosBackup"), exist_ok=True)
    root = tk.Tk()
    app = TelegramBackupGUI(root)
    root.mainloop()
