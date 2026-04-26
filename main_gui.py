import os
import threading
import asyncio
import customtkinter as ctk
from PIL import Image
import pystray
from pystray import MenuItem as item
import sys
import json
import webbrowser

from src.core.daemon import run_daemon
from src.api.server import start_server, state as api_state
from src.core.db import get_stats, init_db
from src.utils import format_size, load_config
from src.config import CONFIG_FILE

class BackupProApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Telegram Backup Pro")
        self.geometry("700x600")
        ctk.set_appearance_mode("dark")
        
        # Header
        self.header = ctk.CTkLabel(self, text="📸 Telegram Backup Pro", font=("Inter", 24, "bold"))
        self.header.pack(pady=20)
        
        # Tabs
        self.tabview = ctk.CTkTabview(self)
        self.tabview.pack(pady=10, padx=20, fill="both", expand=True)
        
        self.tab_dashboard = self.tabview.add("Dashboard")
        self.tab_settings = self.tabview.add("Settings")
        
        # --- Dashboard Tab ---
        self.stats_frame = ctk.CTkFrame(self.tab_dashboard)
        self.stats_frame.pack(pady=20, padx=20, fill="x")
        
        self.stats_label = ctk.CTkLabel(self.stats_frame, text="Total Backed Up: 0 files", font=("Inter", 16))
        self.stats_label.pack(pady=10)
        
        self.size_label = ctk.CTkLabel(self.stats_frame, text="Total Size: 0 B", font=("Inter", 16))
        self.size_label.pack(pady=10)
        
        self.log_box = ctk.CTkTextbox(self.tab_dashboard, height=200)
        self.log_box.pack(pady=10, padx=20, fill="both", expand=True)
        
        self.btn_frame = ctk.CTkFrame(self.tab_dashboard, fg_color="transparent")
        self.btn_frame.pack(pady=20)
        
        self.start_btn = ctk.CTkButton(self.btn_frame, text="Start Backup", command=self.toggle_backup, width=200, height=40)
        self.start_btn.pack(side="left", padx=10)
        
        self.web_btn = ctk.CTkButton(self.btn_frame, text="Web Dashboard", command=self.open_dashboard, width=200, height=40)
        self.web_btn.pack(side="left", padx=10)
        
        # --- Settings Tab ---
        self.scroll_settings = ctk.CTkScrollableFrame(self.tab_settings)
        self.scroll_settings.pack(fill="both", expand=True, padx=10, pady=10)
        
        ctk.CTkLabel(self.scroll_settings, text="Telegram Credentials", font=("Inter", 16, "bold")).pack(pady=10)
        
        self.api_id_entry = self.add_setting("API ID", "30449447")
        self.api_hash_entry = self.add_setting("API Hash", "ec0f8e959edb27bc595b05f6b465bf04")
        self.channel_id_entry = self.add_setting("Channel ID", "-1001810631058")
        
        ctk.CTkLabel(self.scroll_settings, text="Folder Settings", font=("Inter", 16, "bold")).pack(pady=10)
        
        self.path_entry = self.add_setting("Photos Folder", PHOTOS_ORIGINALS)
        
        btn_path_frame = ctk.CTkFrame(self.scroll_settings, fg_color="transparent")
        btn_path_frame.pack(pady=5)
        
        ctk.CTkButton(btn_path_frame, text="Select Folder", command=self.select_folder).pack(side="left", padx=5)
        ctk.CTkButton(btn_path_frame, text="Auto-Detect iCloud", command=self.auto_detect_icloud, fg_color="green").pack(side="left", padx=5)
        
        self.save_btn = ctk.CTkButton(self.tab_settings, text="Save Settings", command=self.save_settings)
        self.save_btn.pack(pady=20)

        # State
        self.running = False
        self.daemon_thread = None
        self.load_settings()
        self.update_ui_stats()

    def add_setting(self, label, default=""):
        frame = ctk.CTkFrame(self.scroll_settings, fg_color="transparent")
        frame.pack(fill="x", pady=5)
        ctk.CTkLabel(frame, text=label, width=120, anchor="w").pack(side="left", padx=10)
        entry = ctk.CTkEntry(frame)
        entry.pack(side="left", fill="x", expand=True, padx=10)
        entry.insert(0, default)
        return entry

    def select_folder(self):
        folder = ctk.filedialog.askdirectory()
        if folder:
            self.path_entry.delete(0, "end")
            self.path_entry.insert(0, folder)

    def auto_detect_icloud(self):
        from src.config import get_icloud_path
        path = get_icloud_path()
        self.path_entry.delete(0, "end")
        self.path_entry.insert(0, path)
        self.log(f"Auto-detected path: {path}")

    def load_settings(self):
        try:
            config = load_config()
            self.api_id_entry.delete(0, "end")
            self.api_id_entry.insert(0, str(config.get("api_id", "")))
            self.api_hash_entry.delete(0, "end")
            self.api_hash_entry.insert(0, config.get("api_hash", ""))
            self.channel_id_entry.delete(0, "end")
            self.channel_id_entry.insert(0, str(config.get("channel_id", "")))
        except Exception:
            pass

    def save_settings(self):
        config = {
            "api_id": self.api_id_entry.get(),
            "api_hash": self.api_hash_entry.get(),
            "channel_id": self.channel_id_entry.get(),
            "photos_path": self.path_entry.get(),
            "upload_mode": "Both",
            "convert_heic": True,
            "compress_videos": True,
            "free_up_space": False
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=4)
        self.log("Settings saved!")

    def log(self, message):
        self.log_box.insert("end", f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {message}\n")
        self.log_box.see("end")

    def update_ui_stats(self):
        try:
            conn = init_db()
            count, size = get_stats(conn)
            self.stats_label.configure(text=f"Total Backed Up: {count} files")
            self.size_label.configure(text=f"Total Size: {format_size(size)}")
            api_state["total_files"] = count
            api_state["total_size"] = format_size(size)
        except Exception:
            pass
        self.after(5000, self.update_ui_stats)

    def toggle_backup(self):
        if not self.running:
            self.running = True
            self.start_btn.configure(text="Stop Backup", fg_color="red")
            self.log("Backup process started...")
            
            def run():
                asyncio.run(run_daemon())
                
            self.daemon_thread = threading.Thread(target=run, daemon=True)
            self.daemon_thread.start()
        else:
            self.log("Stopping... (Please restart app to fully stop)")
            self.running = False
            self.start_btn.configure(text="Start Backup", fg_color="#1f538d")

    def open_dashboard(self):
        webbrowser.open("http://127.0.0.1:8000")

    def hide_window(self):
        self.withdraw()
        self.show_tray_icon()

    def show_tray_icon(self):
        def on_open(icon, item):
            icon.stop()
            self.deiconify()
        def on_exit(icon, item):
            icon.stop()
            self.quit()
        image = Image.new('RGB', (64, 64), color=(0, 136, 204))
        menu = pystray.Menu(item('Open', on_open), item('Exit', on_exit))
        self.icon = pystray.Icon("TelegramBackupPro", image, "Backup Pro", menu)
        threading.Thread(target=self.icon.run, daemon=True).start()

import datetime
def main():
    threading.Thread(target=start_server, daemon=True).start()
    app = BackupProApp()
    app.protocol('WM_DELETE_WINDOW', app.hide_window)
    app.mainloop()

if __name__ == "__main__":
    main()
