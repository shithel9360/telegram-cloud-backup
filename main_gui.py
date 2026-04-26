import os
import threading
import asyncio
import customtkinter as ctk
from PIL import Image
import pystray
from pystray import MenuItem as item
import sys

from src.core.daemon import run_daemon
from src.api.server import start_server, state as api_state
from src.core.db import get_stats, init_db
from src.utils import format_size

class BackupProApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Telegram Backup Pro")
        self.geometry("600x500")
        ctk.set_appearance_mode("dark")
        
        # UI Elements
        self.header = ctk.CTkLabel(self, text="📸 Backup Pro", font=("Inter", 24, "bold"))
        self.header.pack(pady=20)
        
        self.status_frame = ctk.CTkFrame(self)
        self.status_frame.pack(pady=10, padx=20, fill="both", expand=True)
        
        self.stats_label = ctk.CTkLabel(self.status_frame, text="Total Backed Up: 0 files", font=("Inter", 16))
        self.stats_label.pack(pady=10)
        
        self.size_label = ctk.CTkLabel(self.status_frame, text="Total Size: 0 B", font=("Inter", 16))
        self.size_label.pack(pady=10)
        
        self.log_box = ctk.CTkTextbox(self, height=150)
        self.log_box.pack(pady=20, padx=20, fill="both")
        
        self.btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.btn_frame.pack(pady=20)
        
        self.start_btn = ctk.CTkButton(self.btn_frame, text="Start Backup", command=self.toggle_backup)
        self.start_btn.pack(side="left", padx=10)
        
        self.web_btn = ctk.CTkButton(self.btn_frame, text="Open Dashboard", command=self.open_dashboard)
        self.web_btn.pack(side="left", padx=10)
        
        # State
        self.running = False
        self.daemon_thread = None
        
        # Periodic update
        self.update_ui_stats()

    def log(self, message):
        self.log_box.insert("end", f"{message}\n")
        self.log_box.see("end")

    def update_ui_stats(self):
        try:
            conn = init_db()
            count, size = get_stats(conn)
            self.stats_label.configure(text=f"Total Backed Up: {count} files")
            self.size_label.configure(text=f"Total Size: {format_size(size)}")
            
            # Sync to API state
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
            
            # Start daemon in background thread
            def run():
                asyncio.run(run_daemon())
                
            self.daemon_thread = threading.Thread(target=run, daemon=True)
            self.daemon_thread.start()
        else:
            # For simplicity, we just restart the app or suggest closing
            self.log("Stopping backup... (Please restart app to fully stop)")
            self.running = False
            self.start_btn.configure(text="Start Backup", fg_color="#1f538d")

    def open_dashboard(self):
        import webbrowser
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

        # Create a simple icon (you can replace with a real image file)
        image = Image.new('RGB', (64, 64), color=(0, 136, 204))
        menu = pystray.Menu(item('Open', on_open), item('Exit', on_exit))
        self.icon = pystray.Icon("TelegramBackupPro", image, "Backup Pro", menu)
        threading.Thread(target=self.icon.run, daemon=True).start()

def main():
    # Start FastAPI in background
    threading.Thread(target=start_server, daemon=True).start()
    
    app = BackupProApp()
    # Override close button to hide to tray
    app.protocol('WM_DELETE_WINDOW', app.hide_window)
    app.mainloop()

if __name__ == "__main__":
    main()
