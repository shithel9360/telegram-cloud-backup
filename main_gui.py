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
import datetime

from src.core.daemon import run_daemon
from src.api.server import start_server, state as api_state
from src.core.db import get_stats, init_db
from src.utils import format_size, load_config
from src.config import CONFIG_FILE, PHOTOS_ORIGINALS, SESSION_FILE

class BackupProApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Telegram Backup Pro - Setup Wizard")
        self.geometry("800x600")
        ctk.set_appearance_mode("dark")
        
        # Shared State
        self.running = False
        self.daemon_thread = None
        
        # Container for screens
        self.container = ctk.CTkFrame(self)
        self.container.pack(fill="both", expand=True, padx=20, pady=20)
        
        self.frames = {}
        self.current_frame = None
        
        # Initialize Frames
        self.init_wizard()
        
        # Start in Wizard or Dashboard?
        if os.path.exists(SESSION_FILE):
            self.show_frame("Dashboard")
        else:
            self.show_frame("Welcome")

    def init_wizard(self):
        # 1. Welcome Screen
        self.frames["Welcome"] = self.create_welcome_frame()
        # 2. Telegram Login
        self.frames["Login"] = self.create_login_frame()
        # 3. Source Selection
        self.frames["Source"] = self.create_source_frame()
        # 4. Path Config
        self.frames["Config"] = self.create_config_frame()
        # 5. Dashboard
        self.frames["Dashboard"] = self.create_dashboard_frame()

    def show_frame(self, name):
        if self.current_frame:
            self.current_frame.pack_forget()
        self.current_frame = self.frames[name]
        self.current_frame.pack(fill="both", expand=True)

    # --- FRAME CREATORS ---

    def create_welcome_frame(self):
        frame = ctk.CTkFrame(self.container)
        ctk.CTkLabel(frame, text="📸 Welcome to Backup Pro", font=("Inter", 32, "bold")).pack(pady=40)
        ctk.CTkLabel(frame, text="Synchronize your memories to Telegram automatically.\nFollow this simple wizard to get started.", font=("Inter", 16)).pack(pady=20)
        
        ctk.CTkButton(frame, text="Start Setup Wizard", command=lambda: self.show_frame("Login"), width=250, height=50).pack(pady=40)
        return frame

    def create_login_frame(self):
        frame = ctk.CTkFrame(self.container)
        ctk.CTkLabel(frame, text="Step 1: Telegram Authorization", font=("Inter", 24, "bold")).pack(pady=20)
        
        scroll = ctk.CTkScrollableFrame(frame)
        scroll.pack(fill="both", expand=True, padx=20, pady=10)
        
        self.api_id_val = self.add_entry(scroll, "API ID", "")
        self.api_hash_val = self.add_entry(scroll, "API Hash", "")
        self.phone_val = self.add_entry(scroll, "Phone Number", "+880")
        self.channel_id_val = self.add_entry(scroll, "Channel ID", "")
        
        self.login_btn = ctk.CTkButton(frame, text="Connect to Telegram", command=self.do_login, fg_color="#0088cc", width=200)
        self.login_btn.pack(pady=20)
        
        self.login_status = ctk.CTkLabel(frame, text="")
        self.login_status.pack()
        
        return frame

    def create_source_frame(self):
        frame = ctk.CTkFrame(self.container)
        ctk.CTkLabel(frame, text="Step 2: Choose Backup Source", font=("Inter", 24, "bold")).pack(pady=20)
        
        ctk.CTkButton(frame, text="☁️ iCloud Photos", command=lambda: self.select_source("iCloud"), width=300, height=60).pack(pady=10)
        ctk.CTkButton(frame, text="📂 Local Folders", command=lambda: self.select_source("Local"), width=300, height=60).pack(pady=10)
        ctk.CTkButton(frame, text="📂 Google Drive (Coming Soon)", state="disabled", width=300, height=60).pack(pady=10)
        
        return frame

    def create_config_frame(self):
        frame = ctk.CTkFrame(self.container)
        self.config_title = ctk.CTkLabel(frame, text="Step 3: Source Configuration", font=("Inter", 24, "bold"))
        self.config_title.pack(pady=20)
        
        self.config_info = ctk.CTkLabel(frame, text="", wraplength=500)
        self.config_info.pack(pady=10)
        
        self.path_val = ctk.CTkEntry(frame, width=500)
        self.path_val.pack(pady=10)
        
        btn_box = ctk.CTkFrame(frame, fg_color="transparent")
        btn_box.pack(pady=10)
        
        ctk.CTkButton(btn_box, text="Select Folder", command=self.manual_select_folder).pack(side="left", padx=10)
        self.auto_btn = ctk.CTkButton(btn_box, text="Auto-Detect iCloud", command=self.auto_detect_icloud, fg_color="green")
        self.auto_btn.pack(side="left", padx=10)
        
        ctk.CTkButton(frame, text="Everything is Ready! Go to Dashboard", command=self.finish_setup, width=300, height=50, fg_color="#1f538d").pack(pady=40)
        
        return frame

    def create_dashboard_frame(self):
        frame = ctk.CTkFrame(self.container)
        ctk.CTkLabel(frame, text="📸 Telegram Backup Pro", font=("Inter", 24, "bold")).pack(pady=10)
        
        stats = ctk.CTkFrame(frame)
        stats.pack(fill="x", pady=10)
        self.stats_label = ctk.CTkLabel(stats, text="Total Backed Up: 0 files", font=("Inter", 16))
        self.stats_label.pack(pady=5)
        self.size_label = ctk.CTkLabel(stats, text="Total Size: 0 B", font=("Inter", 16))
        self.size_label.pack(pady=5)
        
        self.log_box = ctk.CTkTextbox(frame, height=250)
        self.log_box.pack(fill="both", expand=True, pady=10)
        
        btns = ctk.CTkFrame(frame, fg_color="transparent")
        btns.pack(pady=10)
        self.start_btn = ctk.CTkButton(btns, text="Start Backup Now", command=self.toggle_backup, width=200, height=45, fg_color="green")
        self.start_btn.pack(side="left", padx=10)
        ctk.CTkButton(btns, text="Settings", command=lambda: self.show_frame("Login"), width=150, height=45).pack(side="left", padx=10)
        
        self.update_ui_stats()
        return frame

    # --- HELPERS & LOGIC ---

    def add_entry(self, parent, label, default=""):
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.pack(fill="x", pady=5)
        ctk.CTkLabel(f, text=label, width=120, anchor="w").pack(side="left", padx=5)
        e = ctk.CTkEntry(f)
        e.pack(side="left", fill="x", expand=True, padx=5)
        e.insert(0, default)
        return e

    def do_login(self):
        phone = self.phone_val.get()
        api_id = int(self.api_id_val.get())
        api_hash = self.api_hash_val.get()
        
        from telethon import TelegramClient
        def run():
            client = TelegramClient(SESSION_FILE, api_id, api_hash)
            async def auth():
                await client.connect()
                if not await client.is_user_authorized():
                    self.login_status.configure(text=f"Sending code to {phone}...", text_color="yellow")
                    await client.send_code_request(phone)
                    self.otp_done = threading.Event()
                    self.otp_val = None
                    self.after(100, self.ask_otp)
                    self.otp_done.wait()
                    if self.otp_val:
                        try:
                            await client.sign_in(phone, self.otp_val)
                            self.login_status.configure(text="✅ Authorized!", text_color="green")
                            self.after(1000, lambda: self.show_frame("Source"))
                        except Exception as e: self.login_status.configure(text=f"Error: {e}", text_color="red")
                    else: self.login_status.configure(text="Cancelled", text_color="red")
                else:
                    self.login_status.configure(text="✅ Already Authorized!", text_color="green")
                    self.after(1000, lambda: self.show_frame("Source"))
                await client.disconnect()
            asyncio.run(auth())
        threading.Thread(target=run, daemon=True).start()

    def ask_otp(self):
        d = ctk.CTkInputDialog(text="Enter Telegram OTP:", title="Auth")
        self.otp_val = d.get_input()
        self.otp_done.set()

    def select_source(self, source):
        self.show_frame("Config")
        try:
            config = load_config()
            if source == "iCloud":
                self.config_info.configure(text="Please ensure 'iCloud for Windows' is installed and you are logged into your Apple ID.\nClick Auto-Detect to find the folder automatically.")
                self.auto_btn.configure(state="normal")
                self.api_hash_val.delete(0, "end")
                self.api_hash_val.insert(0, config.get("api_hash", ""))
                self.channel_id_val.delete(0, "end")
                self.channel_id_val.insert(0, str(config.get("channel_id", "")))
                self.path_val.delete(0, "end")
                self.path_val.insert(0, PHOTOS_ORIGINALS)
            else:
                self.config_info.configure(text="Please select the local folder containing your photos/videos.")
                self.auto_btn.configure(state="disabled")
        except Exception:
            pass

    def manual_select_folder(self):
        p = ctk.filedialog.askdirectory()
        if p:
            self.path_val.delete(0, "end")
            self.path_val.insert(0, p)

    def auto_detect_icloud(self):
        from src.config import get_icloud_path
        p = get_icloud_path()
        self.path_val.delete(0, "end")
        self.path_val.insert(0, p)

    def finish_setup(self):
        config = {
            "api_id": self.api_id_val.get(),
            "api_hash": self.api_hash_val.get(),
            "channel_id": self.channel_id_val.get(), # Dynamic from user input
            "photos_path": self.path_val.get(),
            "upload_mode": "Both", "convert_heic": True, "compress_videos": True, "free_up_space": False
        }
        with open(CONFIG_FILE, "w") as f: json.dump(config, f, indent=4)
        self.show_frame("Dashboard")

    def log(self, message):
        self.log_box.insert("end", f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {message}\n")
        self.log_box.see("end")

    def update_ui_stats(self):
        try:
            conn = init_db()
            count, size = get_stats(conn)
            self.stats_label.configure(text=f"Total Backed Up: {count} files")
            self.size_label.configure(text=f"Total Size: {format_size(size)}")
        except Exception: pass
        self.after(5000, self.update_ui_stats)

    def toggle_backup(self):
        if not self.running:
            self.running = True
            self.start_btn.configure(text="Stop Backup", fg_color="red")
            self.log("Starting background engine...")
            def r(): asyncio.run(run_daemon())
            self.daemon_thread = threading.Thread(target=r, daemon=True)
            self.daemon_thread.start()
        else:
            self.running = False
            self.start_btn.configure(text="Start Backup Now", fg_color="green")

    def hide_window(self):
        self.withdraw()
        image = Image.new('RGB', (64, 64), color=(0, 136, 204))
        def on_open(icon, item): icon.stop(); self.deiconify()
        def on_exit(icon, item): icon.stop(); self.quit()
        menu = pystray.Menu(item('Open', on_open), item('Exit', on_exit))
        self.icon = pystray.Icon("BackupPro", image, "Backup Pro", menu)
        threading.Thread(target=self.icon.run, daemon=True).start()

def main():
    threading.Thread(target=start_server, daemon=True).start()
    app = BackupProApp()
    app.protocol('WM_DELETE_WINDOW', app.hide_window)
    app.mainloop()

if __name__ == "__main__":
    main()
