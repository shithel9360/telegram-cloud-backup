import os
import threading
import asyncio
import sys
import json
import webbrowser
import datetime
import logging

import customtkinter as ctk
from PIL import Image
import pystray
from pystray import MenuItem as item

from src.core.daemon import run_daemon
from src.api.server import start_server, state as api_state
from src.core.db import get_stats, init_db
from src.utils import format_size, load_config
from src.config import (
    CONFIG_FILE, PHOTOS_ORIGINALS, SESSION_FILE,
    TELEGRAM_API_ID, TELEGRAM_API_HASH, get_icloud_path
)

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("TelegramBackup")


class BackupProApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Telegram Backup Pro")
        self.geometry("820x620")
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.running = False
        self.daemon_thread = None

        # ── main container ──────────────────────────────────────────────────
        self.container = ctk.CTkFrame(self)
        self.container.pack(fill="both", expand=True, padx=20, pady=20)

        self.frames: dict = {}
        self.current_frame = None

        self._build_all_frames()

        # If already logged-in, skip to dashboard
        if os.path.exists(SESSION_FILE):
            self.show_frame("Dashboard")
        else:
            self.show_frame("Welcome")

    # ── Frame factory ─────────────────────────────────────────────────────────

    def _build_all_frames(self):
        self.frames["Welcome"]   = self._frame_welcome()
        self.frames["Login"]     = self._frame_login()
        self.frames["Source"]    = self._frame_source()
        self.frames["Config"]    = self._frame_config()
        self.frames["Dashboard"] = self._frame_dashboard()

    def show_frame(self, name: str):
        if self.current_frame:
            self.current_frame.pack_forget()
        self.current_frame = self.frames[name]
        self.current_frame.pack(fill="both", expand=True)

    # ── Screen 1 — Welcome ────────────────────────────────────────────────────

    def _frame_welcome(self):
        f = ctk.CTkFrame(self.container)
        ctk.CTkLabel(f, text="📸 Telegram Backup Pro",
                     font=("Inter", 32, "bold")).pack(pady=50)
        ctk.CTkLabel(f,
                     text="Automatically back up your photos & videos to a private Telegram channel.\nSimple. Fast. Secure.",
                     font=("Inter", 15), wraplength=600).pack(pady=10)
        ctk.CTkButton(f, text="Get Started  →", command=lambda: self.show_frame("Login"),
                      width=260, height=54, font=("Inter", 16)).pack(pady=50)
        return f

    # ── Screen 2 — Telegram Login ─────────────────────────────────────────────

    def _frame_login(self):
        f = ctk.CTkFrame(self.container)
        ctk.CTkLabel(f, text="Step 1 — Connect to Telegram",
                     font=("Inter", 22, "bold")).pack(pady=25)
        ctk.CTkLabel(f,
                     text="Enter your phone number and the Channel ID where backups will be saved.",
                     font=("Inter", 13), wraplength=600).pack()

        form = ctk.CTkFrame(f, fg_color="transparent")
        form.pack(fill="x", padx=60, pady=20)

        self._phone_var      = ctk.StringVar(value="+880")
        self._channel_id_var = ctk.StringVar(value="")

        self._make_row(form, "📱  Phone Number",  self._phone_var)
        self._make_row(form, "📢  Channel ID",    self._channel_id_var)

        ctk.CTkLabel(f, text="Tip: forward any message from your channel to @userinfobot to get its ID.",
                     font=("Inter", 11, "italic"), text_color="gray").pack(pady=4)

        self._login_btn = ctk.CTkButton(
            f, text="Send Login Code", command=self._do_login,
            fg_color="#0088cc", width=260, height=48, font=("Inter", 15))
        self._login_btn.pack(pady=20)

        self._login_status = ctk.CTkLabel(f, text="", font=("Inter", 13))
        self._login_status.pack()
        return f

    # ── Screen 3 — Source Selection ───────────────────────────────────────────

    def _frame_source(self):
        f = ctk.CTkFrame(self.container)
        ctk.CTkLabel(f, text="Step 2 — Choose Backup Source",
                     font=("Inter", 22, "bold")).pack(pady=30)

        for label, cmd in [
            ("☁️  iCloud Photos",         lambda: self._select_source("iCloud")),
            ("📂  Local / Custom Folder", lambda: self._select_source("Local")),
        ]:
            ctk.CTkButton(f, text=label, command=cmd,
                          width=340, height=64, font=("Inter", 16)).pack(pady=10)

        ctk.CTkButton(f, text="🟢  Google Drive  (coming soon)", state="disabled",
                      width=340, height=64, font=("Inter", 16)).pack(pady=10)
        return f

    # ── Screen 4 — Folder Config ──────────────────────────────────────────────

    def _frame_config(self):
        f = ctk.CTkFrame(self.container)
        self._cfg_title = ctk.CTkLabel(f, text="Step 3 — Confirm Folder",
                                       font=("Inter", 22, "bold"))
        self._cfg_title.pack(pady=20)

        self._cfg_info = ctk.CTkLabel(f, text="", wraplength=640, font=("Inter", 13))
        self._cfg_info.pack(pady=8)

        self._path_var = ctk.StringVar(value=PHOTOS_ORIGINALS)
        path_entry = ctk.CTkEntry(f, textvariable=self._path_var, width=560)
        path_entry.pack(pady=10)

        btns = ctk.CTkFrame(f, fg_color="transparent")
        btns.pack(pady=8)
        ctk.CTkButton(btns, text="📁  Browse",
                      command=self._browse_folder, width=150).pack(side="left", padx=8)
        self._auto_btn = ctk.CTkButton(btns, text="🔍  Auto-Detect iCloud",
                                       command=self._auto_detect_icloud,
                                       fg_color="green", width=180)
        self._auto_btn.pack(side="left", padx=8)

        ctk.CTkButton(f, text="✅  Save & Go to Dashboard",
                      command=self._finish_setup,
                      width=300, height=50, font=("Inter", 15),
                      fg_color="#1a7f37").pack(pady=40)
        return f

    # ── Screen 5 — Dashboard ──────────────────────────────────────────────────

    def _frame_dashboard(self):
        f = ctk.CTkFrame(self.container)

        # Header row
        hdr = ctk.CTkFrame(f, fg_color="transparent")
        hdr.pack(fill="x", pady=10, padx=10)
        ctk.CTkLabel(hdr, text="📸 Telegram Backup Pro",
                     font=("Inter", 22, "bold")).pack(side="left")
        ctk.CTkButton(hdr, text="⚙ Settings",
                      command=lambda: self.show_frame("Login"),
                      width=100, height=32).pack(side="right")

        # Stats row
        stats = ctk.CTkFrame(f)
        stats.pack(fill="x", padx=10, pady=4)
        self._stats_label = ctk.CTkLabel(stats, text="Backed up: 0 files",
                                         font=("Inter", 15))
        self._stats_label.pack(side="left", padx=20, pady=8)
        self._size_label = ctk.CTkLabel(stats, text="Total size: 0 B",
                                        font=("Inter", 15))
        self._size_label.pack(side="left", padx=20, pady=8)
        self._status_label = ctk.CTkLabel(stats, text="Status: Idle",
                                          font=("Inter", 15))
        self._status_label.pack(side="right", padx=20, pady=8)

        # Log box
        self._log_box = ctk.CTkTextbox(f, height=300, state="disabled")
        self._log_box.pack(fill="both", expand=True, padx=10, pady=8)

        # Buttons
        btns = ctk.CTkFrame(f, fg_color="transparent")
        btns.pack(pady=10)
        self._start_btn = ctk.CTkButton(
            btns, text="▶  Start Backup", command=self._toggle_backup,
            width=220, height=48, font=("Inter", 15), fg_color="#1a7f37")
        self._start_btn.pack(side="left", padx=10)

        ctk.CTkButton(btns, text="🌐  Web Dashboard",
                      command=lambda: webbrowser.open("http://127.0.0.1:8000"),
                      width=180, height=48, font=("Inter", 15)).pack(side="left", padx=10)

        self._update_stats_loop()
        return f

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _make_row(self, parent, label: str, var: ctk.StringVar):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=6)
        ctk.CTkLabel(row, text=label, width=160, anchor="w",
                     font=("Inter", 14)).pack(side="left", padx=6)
        ctk.CTkEntry(row, textvariable=var, font=("Inter", 14)).pack(
            side="left", fill="x", expand=True, padx=6)

    def _gui_log(self, msg: str):
        """Thread-safe log to the textbox."""
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._log_box.configure(state="normal")
        self._log_box.insert("end", f"[{ts}]  {msg}\n")
        self._log_box.see("end")
        self._log_box.configure(state="disabled")

    def _update_stats_loop(self):
        try:
            conn  = init_db()
            count, size = get_stats(conn)
            self._stats_label.configure(text=f"Backed up: {count} files")
            self._size_label.configure(text=f"Total size: {format_size(size)}")
            self._status_label.configure(
                text=f"Status: {api_state.get('status', 'Idle')}")
        except Exception:
            pass
        self.after(4000, self._update_stats_loop)

    # ── Login flow ────────────────────────────────────────────────────────────

    def _do_login(self):
        phone = self._phone_var.get().strip()
        if not phone or len(phone) < 7:
            self._login_status.configure(text="⚠️ Enter a valid phone number.", text_color="orange")
            return

        self._login_btn.configure(state="disabled", text="Connecting…")

        def run():
            from telethon import TelegramClient
            client = TelegramClient(SESSION_FILE, TELEGRAM_API_ID, TELEGRAM_API_HASH)
            asyncio.run(self._auth_flow(client, phone))

        threading.Thread(target=run, daemon=True).start()

    async def _auth_flow(self, client, phone: str):
        try:
            await client.connect()
            if not await client.is_user_authorized():
                self._set_login_status("📨  Sending code to " + phone + "…", "yellow")
                await client.send_code_request(phone)

                # Ask OTP in main thread
                self._otp_event = threading.Event()
                self._otp_result = None
                self.after(100, self._ask_otp)
                self._otp_event.wait()

                if self._otp_result:
                    try:
                        await client.sign_in(phone, self._otp_result)
                        self._set_login_status("✅  Authorized!", "green")
                        # Save phone in config
                        self._save_initial_config(phone)
                        self.after(1200, lambda: self.show_frame("Source"))
                    except Exception as e:
                        self._set_login_status(f"❌  {e}", "red")
                else:
                    self._set_login_status("Cancelled.", "gray")
            else:
                self._set_login_status("✅  Already logged in!", "green")
                self.after(1200, lambda: self.show_frame("Source"))
        except Exception as e:
            self._set_login_status(f"❌  {e}", "red")
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
            self.after(0, lambda: self._login_btn.configure(
                state="normal", text="Send Login Code"))

    def _set_login_status(self, msg: str, color: str):
        self.after(0, lambda: self._login_status.configure(
            text=msg, text_color=color))

    def _ask_otp(self):
        d = ctk.CTkInputDialog(text="Enter the code Telegram sent you:", title="Verification Code")
        self._otp_result = d.get_input()
        self._otp_event.set()

    def _save_initial_config(self, phone: str):
        """Persist phone and channel to config file."""
        cfg = {}
        try:
            cfg = load_config()
        except Exception:
            pass
        cfg.update({
            "api_id":   TELEGRAM_API_ID,
            "api_hash": TELEGRAM_API_HASH,
            "phone":    phone,
            "channel_id": self._channel_id_var.get().strip() or cfg.get("channel_id", ""),
            "photos_path": cfg.get("photos_path", PHOTOS_ORIGINALS),
            "upload_mode": "Both",
            "convert_heic": True,
            "compress_videos": True,
            "free_up_space": False,
        })
        with open(CONFIG_FILE, "w") as fp:
            json.dump(cfg, fp, indent=4)

    # ── Source selection ──────────────────────────────────────────────────────

    def _select_source(self, source: str):
        self.show_frame("Config")
        if source == "iCloud":
            self._cfg_info.configure(
                text="Make sure 'iCloud for Windows' is installed and you are signed in with your Apple ID.\n"
                     "Click Auto-Detect to find the folder automatically, or Browse to choose manually.")
            self._auto_btn.configure(state="normal")
            self._path_var.set(get_icloud_path())
        else:
            self._cfg_info.configure(
                text="Choose the folder that contains the photos and videos you want to back up.")
            self._auto_btn.configure(state="disabled")
            self._path_var.set("")

    def _browse_folder(self):
        p = ctk.filedialog.askdirectory()
        if p:
            self._path_var.set(p)

    def _auto_detect_icloud(self):
        p = get_icloud_path()
        self._path_var.set(p)
        self._cfg_info.configure(
            text=f"Auto-detected path:\n{p}\n\nIf this is wrong, click Browse to select manually.")

    # ── Finish wizard ─────────────────────────────────────────────────────────

    def _finish_setup(self):
        cfg = {}
        try:
            cfg = load_config()
        except Exception:
            pass
        cfg.update({
            "api_id":         TELEGRAM_API_ID,
            "api_hash":       TELEGRAM_API_HASH,
            "channel_id":     self._channel_id_var.get().strip() or cfg.get("channel_id", ""),
            "photos_path":    self._path_var.get().strip(),
            "upload_mode":    "Both",
            "convert_heic":   True,
            "compress_videos": True,
            "free_up_space":  False,
        })
        with open(CONFIG_FILE, "w") as fp:
            json.dump(cfg, fp, indent=4)
        self.show_frame("Dashboard")

    # ── Backup control ────────────────────────────────────────────────────────

    def _toggle_backup(self):
        if not self.running:
            self.running = True
            self._start_btn.configure(text="⏹  Stop Backup", fg_color="red")
            self._gui_log("Starting backup engine…")

            def run():
                asyncio.run(run_daemon(log_callback=lambda m: self.after(0, lambda: self._gui_log(m))))

            self.daemon_thread = threading.Thread(target=run, daemon=True)
            self.daemon_thread.start()
        else:
            self.running = False
            self._start_btn.configure(text="▶  Start Backup", fg_color="#1a7f37")
            self._gui_log("Backup stopped. (Restart app to fully stop background tasks.)")

    # ── System tray ───────────────────────────────────────────────────────────

    def hide_window(self):
        self.withdraw()
        img  = Image.new("RGB", (64, 64), color=(0, 136, 204))

        def on_open(icon, _item):
            icon.stop()
            self.after(0, self.deiconify)

        def on_exit(icon, _item):
            icon.stop()
            self.after(0, self.quit)

        menu = pystray.Menu(item("Open", on_open), item("Exit", on_exit))
        self._tray_icon = pystray.Icon("BackupPro", img, "Backup Pro", menu)
        threading.Thread(target=self._tray_icon.run, daemon=True).start()


def main():
    # Start FastAPI dashboard server in background
    threading.Thread(target=start_server, daemon=True).start()
    app = BackupProApp()
    app.protocol("WM_DELETE_WINDOW", app.hide_window)
    app.mainloop()


if __name__ == "__main__":
    main()
