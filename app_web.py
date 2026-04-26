"""
Telegram Backup Pro — Web Edition
Single-file web app. Run with: python app_web.py
Then open: http://localhost:7878
"""

import os, sys, json, time, asyncio, threading, logging, shutil, tempfile, sqlite3
from pathlib import Path
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_FILE  = Path.home() / ".tele_backup_config.json"
DB_FILE      = Path.home() / ".tele_backup_state.db"
SESSION_FILE = str(Path.home() / ".tele_backup_session")
APPDATA      = Path(os.environ.get("APPDATA", Path.home())) / "TelegramBackupPro"
APPDATA.mkdir(parents=True, exist_ok=True)
LOG_FILE     = APPDATA / "backup.log"
PORT         = 7878

TELEGRAM_API_ID   = 36355055
TELEGRAM_API_HASH = "9b819327f0403ce37b08e316a8464cb6"

IMAGE_EXT = ('.jpg','.jpeg','.png','.heic','.gif','.raw','.dng','.bmp','.webp')
VIDEO_EXT = ('.mov','.mp4','.m4v','.avi','.mkv')
ALL_EXT   = IMAGE_EXT + VIDEO_EXT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()]
)
logger = logging.getLogger("BackupPro")

# ── Shared state ──────────────────────────────────────────────────────────────
state = {
    "status": "idle",        # idle | running | stopped
    "logs":   [],            # last 200 log lines
    "count":  0,
    "size_str": "0 B",
    "authorized": False,
}

def push_log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    state["logs"].append(line)
    if len(state["logs"]) > 200:
        state["logs"].pop(0)
    logger.info(msg)

# ── DB helpers ────────────────────────────────────────────────────────────────
def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_FILE), check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS uploads (
        uuid TEXT PRIMARY KEY, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        filename TEXT, file_size INTEGER DEFAULT 0)""")
    conn.commit()
    return conn

def is_uploaded(conn, fhash): return bool(conn.execute("SELECT 1 FROM uploads WHERE uuid=?", (fhash,)).fetchone())
def mark_uploaded(conn, fhash, fname, size): conn.execute("REPLACE INTO uploads VALUES(?,CURRENT_TIMESTAMP,?,?)",(fhash,fname,size)); conn.commit()
def get_stats(conn):
    r = conn.execute("SELECT COUNT(*),SUM(file_size) FROM uploads WHERE filename NOT LIKE 'SKIPPED%'").fetchone()
    return r[0] or 0, r[1] or 0

# ── Config helpers ────────────────────────────────────────────────────────────
def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}

def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

def fmt_size(b):
    if b>=(1<<30): return f"{b/(1<<30):.2f} GB"
    if b>=(1<<20): return f"{b/(1<<20):.1f} MB"
    if b>=(1<<10): return f"{b/(1<<10):.1f} KB"
    return f"{b} B"

def detect_icloud():
    candidates = [
        Path.home()/"Pictures"/"iCloud Photos"/"Photos",
        Path.home()/"Pictures"/"iCloud Photos",
        Path.home()/"Pictures",
    ]
    for c in candidates:
        if c.exists(): return str(c)
    return str(Path.home()/"Pictures")

# ── Backup daemon ─────────────────────────────────────────────────────────────
_daemon_loop: asyncio.AbstractEventLoop = None
_daemon_task = None

async def _daemon(cfg):
    from telethon import TelegramClient
    from telethon.errors import FloodWaitError

    channel_id  = int(cfg["channel_id"])
    photos_path = cfg.get("photos_path", "")
    api_id      = int(cfg.get("api_id", TELEGRAM_API_ID))
    api_hash    = cfg.get("api_hash", TELEGRAM_API_HASH)

    if not photos_path or not Path(photos_path).exists():
        push_log(f"❌ Photos folder not found: {photos_path}")
        state["status"] = "stopped"
        return

    client = TelegramClient(SESSION_FILE, api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        push_log("❌ Not authorized. Please log in first.")
        state["status"] = "stopped"
        await client.disconnect()
        return

    sem    = asyncio.Semaphore(2)
    lock   = asyncio.Lock()
    conn   = db_connect()
    export = Path(tempfile.gettempdir()) / "tele_backup_export"
    export.mkdir(exist_ok=True)
    push_log(f"✅ Connected! Watching: {photos_path}")

    while state["status"] == "running":
        try:
            files = []
            for root, dirs, fnames in os.walk(photos_path):
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                for fn in fnames:
                    if fn.lower().endswith(ALL_EXT):
                        files.append(os.path.join(root, fn))
            files.sort(key=os.path.getmtime)

            now = time.time()
            tasks = []
            for fp in files:
                fname = os.path.basename(fp)
                try:
                    sz    = os.path.getsize(fp)
                    mtime = os.path.getmtime(fp)
                except OSError:
                    continue
                fhash = f"{fname}_{sz}"
                async with lock:
                    if is_uploaded(conn, fhash): continue
                if now - mtime < 3: continue
                tasks.append(_upload_one(client, conn, channel_id, fp, sz, fhash, sem, lock, export))

            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                ok = sum(1 for r in results if r is True)
                if ok: push_log(f"✅ Uploaded {ok} new file(s).")
            else:
                push_log("🔍 No new files. Waiting 15s…")

            cnt, raw = get_stats(conn)
            state["count"]    = cnt
            state["size_str"] = fmt_size(raw)

        except FloodWaitError as e:
            push_log(f"⏳ Flood wait {e.seconds}s")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            push_log(f"⚠️ Error: {e}")

        await asyncio.sleep(15)

    await client.disconnect()
    push_log("🛑 Daemon stopped.")

async def _upload_one(client, conn, channel_id, fp, sz, fhash, sem, lock, export_dir):
    fname = os.path.basename(fp)
    async with lock:
        if is_uploaded(conn, fhash): return False
        mark_uploaded(conn, fhash, f"IN_FLIGHT_{fname}", sz)
    async with sem:
        try:
            ts   = int(time.time()*1000)
            tmp  = export_dir / f"{ts}_{fname}"
            shutil.copy2(fp, tmp)
            date_str = datetime.fromtimestamp(os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M")
            ext  = os.path.splitext(fname)[1].upper().lstrip('.')
            cap  = f"📁 {fname}\n📅 {date_str}\n🏷 {ext}  •  {fmt_size(sz)}"
            push_log(f"⬆️  Uploading {fname}…")
            await client.send_file(channel_id, str(tmp), caption=cap, force_document=True)
            async with lock: mark_uploaded(conn, fhash, fname, sz)
            if tmp.exists(): tmp.unlink()
            return True
        except Exception as e:
            push_log(f"❌ Failed {fname}: {e}")
            async with lock:
                conn.execute("DELETE FROM uploads WHERE uuid=?", (fhash,)); conn.commit()
            if tmp.exists(): tmp.unlink()
            return False

def start_daemon():
    global _daemon_loop
    cfg = load_config()
    _daemon_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_daemon_loop)
    _daemon_loop.run_until_complete(_daemon(cfg))

# ── OTP login ─────────────────────────────────────────────────────────────────
_login_state = {"step": "idle", "phone": "", "hash": ""}

async def _send_otp(phone):
    from telethon import TelegramClient
    client = TelegramClient(SESSION_FILE, TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await client.connect()
    sent = await client.send_code_request(phone)
    _login_state["hash"] = sent.phone_code_hash
    _login_state["phone"] = phone
    await client.disconnect()

async def _verify_otp(code):
    from telethon import TelegramClient
    client = TelegramClient(SESSION_FILE, TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await client.connect()
    await client.sign_in(_login_state["phone"], code, phone_code_hash=_login_state["hash"])
    me = await client.get_me()
    state["authorized"] = True
    await client.disconnect()
    return me.first_name

# ── HTTP Server ───────────────────────────────────────────────────────────────
from http.server import BaseHTTPRequestHandler, HTTPServer
import urllib.parse as urlparse

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass  # suppress access logs

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html: str):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self.send_html(DASHBOARD_HTML)
        elif path == "/api/state":
            self.send_json({**state, "logs": state["logs"][-50:]})
        elif path == "/api/config":
            self.send_json(load_config())
        elif path == "/api/detect_icloud":
            self.send_json({"path": detect_icloud()})
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length) or b"{}")
        path   = self.path

        if path == "/api/send_otp":
            phone = body.get("phone","").strip()
            try:
                asyncio.run(_send_otp(phone))
                _login_state["step"] = "waiting_otp"
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)})

        elif path == "/api/verify_otp":
            code = body.get("code","").strip()
            try:
                name = asyncio.run(_verify_otp(code))
                cfg = load_config()
                cfg.update({"api_id": TELEGRAM_API_ID, "api_hash": TELEGRAM_API_HASH,
                             "phone": _login_state["phone"]})
                save_config(cfg)
                self.send_json({"ok": True, "name": name})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)})

        elif path == "/api/save_config":
            cfg = load_config()
            cfg.update(body)
            save_config(cfg)
            self.send_json({"ok": True})

        elif path == "/api/start":
            if state["status"] != "running":
                state["status"] = "running"
                push_log("🚀 Backup started.")
                threading.Thread(target=start_daemon, daemon=True).start()
            self.send_json({"ok": True})

        elif path == "/api/stop":
            state["status"] = "stopped"
            self.send_json({"ok": True})

        elif path == "/api/quit":
            state["status"] = "stopped"
            self.send_json({"ok": True})
            def _shutdown():
                time.sleep(1)
                os._exit(0)
            threading.Thread(target=_shutdown).start()

        else:
            self.send_response(404); self.end_headers()

# ── Dashboard HTML (single-page) ──────────────────────────────────────────────
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Telegram Backup Pro</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#e6edf3;min-height:100vh}
  .app{max-width:900px;margin:0 auto;padding:24px 16px}
  h1{font-size:1.6rem;font-weight:700;margin-bottom:4px}
  .sub{color:#8b949e;font-size:.9rem;margin-bottom:28px}
  .card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:24px;margin-bottom:20px}
  .card h2{font-size:1rem;font-weight:600;margin-bottom:16px;color:#58a6ff}
  .row{display:flex;gap:12px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
  label{min-width:140px;color:#8b949e;font-size:.875rem}
  input{flex:1;background:#0d1117;border:1px solid #30363d;border-radius:8px;
        padding:9px 12px;color:#e6edf3;font-size:.9rem;outline:none;min-width:200px}
  input:focus{border-color:#58a6ff}
  button{padding:10px 22px;border:none;border-radius:8px;cursor:pointer;font-size:.9rem;font-weight:600;transition:.2s}
  .btn-primary{background:#238636;color:#fff}
  .btn-primary:hover{background:#2ea043}
  .btn-blue{background:#1f6feb;color:#fff}
  .btn-blue:hover{background:#388bfd}
  .btn-red{background:#b62324;color:#fff}
  .btn-red:hover{background:#da3633}
  .btn-sm{padding:7px 14px;font-size:.8rem}
  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:20px}
  .stat{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:16px;text-align:center}
  .stat .val{font-size:1.6rem;font-weight:700;color:#58a6ff}
  .stat .key{font-size:.8rem;color:#8b949e;margin-top:4px}
  #logbox{background:#0d1117;border:1px solid #30363d;border-radius:8px;
           padding:12px;height:260px;overflow-y:auto;font-family:'Cascadia Code','Courier New',monospace;
           font-size:.8rem;color:#7ee787;line-height:1.6}
  .badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:.75rem;font-weight:600}
  .badge.idle{background:#21262d;color:#8b949e}
  .badge.running{background:#1a4720;color:#3fb950}
  .badge.stopped{background:#3d1a1a;color:#f85149}
  .tabs{display:flex;gap:4px;margin-bottom:20px;border-bottom:1px solid #30363d;padding-bottom:0}
  .tab{padding:8px 20px;cursor:pointer;border-radius:8px 8px 0 0;font-size:.9rem;color:#8b949e}
  .tab.active{background:#161b22;color:#e6edf3;border:1px solid #30363d;border-bottom:1px solid #161b22;margin-bottom:-1px}
  .pane{display:none}.pane.active{display:block}
  .msg{padding:10px 14px;border-radius:8px;margin-top:10px;font-size:.875rem}
  .msg.ok{background:#1a4720;color:#3fb950}
  .msg.err{background:#3d1a1a;color:#f85149}
</style>
</head>
<body>
<div class="app">
  <h1>📸 Telegram Backup Pro</h1>
  <p class="sub">Backup your photos & videos to Telegram — automatically.<br><span style="color:#58a6ff;font-weight:600;">Developed by Shithel</span></p>

  <div class="tabs">
    <div class="tab active" onclick="showTab('dashboard')">Dashboard</div>
    <div class="tab" onclick="showTab('login')">Telegram Login</div>
    <div class="tab" onclick="showTab('settings')">Settings</div>
  </div>

  <!-- DASHBOARD -->
  <div class="pane active" id="tab-dashboard">
    <div class="stats">
      <div class="stat"><div class="val" id="s-count">0</div><div class="key">Files Backed Up</div></div>
      <div class="stat"><div class="val" id="s-size">0 B</div><div class="key">Total Size</div></div>
      <div class="stat"><div class="val"><span class="badge idle" id="s-status">Idle</span></div><div class="key">Status</div></div>
    </div>
    <div class="card">
      <h2>Controls</h2>
      <div class="row">
        <button class="btn-primary" onclick="startBackup()">▶ Start Backup</button>
        <button class="btn-red" onclick="stopBackup()">⏹ Stop Backup</button>
        <button style="background:#555;color:#fff;" onclick="quitApp()">⏏ Exit App Completely</button>
      </div>
    </div>
    <div class="card">
      <h2>Live Log</h2>
      <div id="logbox">Waiting for activity…</div>
    </div>
  </div>

  <!-- LOGIN -->
  <div class="pane" id="tab-login">
    <div class="card">
      <h2>Connect Your Telegram Account</h2>
      <div class="row"><label>Phone Number</label><input id="phone" placeholder="+8801XXXXXXXXX" /></div>
      <div class="row"><label>Channel ID</label><input id="channel_id" placeholder="-100XXXXXXXXXX" /></div>
      <p style="color:#8b949e;font-size:.8rem;margin-bottom:14px">
        Tip: Forward any message from your channel to @userinfobot to get the Channel ID.
      </p>
      <button class="btn-blue" onclick="sendOtp()">Send Login Code →</button>
      <div id="otp-row" style="display:none;margin-top:16px">
        <div class="row"><label>Enter OTP</label><input id="otp" placeholder="Code from Telegram" /></div>
        <button class="btn-primary" onclick="verifyOtp()">✓ Verify Code</button>
      </div>
      <div id="login-msg"></div>
    </div>
  </div>

  <!-- SETTINGS -->
  <div class="pane" id="tab-settings">
    <div class="card">
      <h2>Backup Folder</h2>
      <div class="row">
        <label>Photos Path</label>
        <input id="photos_path" placeholder="C:\\Users\\You\\Pictures\\iCloud Photos\\Photos" />
        <button class="btn-blue btn-sm" onclick="detectiCloud()">Auto-Detect iCloud</button>
      </div>
      <button class="btn-primary" onclick="saveSettings()">Save Settings</button>
      <div id="settings-msg"></div>
    </div>
  </div>
</div>

<script>
function showTab(t){
  document.querySelectorAll('.tab').forEach((el,i)=>{el.classList.remove('active')});
  document.querySelectorAll('.pane').forEach(el=>el.classList.remove('active'));
  document.querySelector('#tab-'+t).classList.add('active');
  event.target.classList.add('active');
}

async function api(method,path,body){
  const r=await fetch(path,{method,headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
  return r.json();
}

async function sendOtp(){
  const phone=document.getElementById('phone').value.trim();
  const ch=document.getElementById('channel_id').value.trim();
  if(!phone){alert('Enter your phone number'); return;}
  setMsg('login-msg','Sending code…','');
  const r=await api('POST','/api/send_otp',{phone});
  if(r.ok){
    document.getElementById('otp-row').style.display='block';
    setMsg('login-msg','✅ Code sent! Check Telegram.','ok');
    if(ch) await api('POST','/api/save_config',{channel_id:ch});
  } else {
    setMsg('login-msg','❌ '+r.error,'err');
  }
}

async function verifyOtp(){
  const code=document.getElementById('otp').value.trim();
  setMsg('login-msg','Verifying…','');
  const r=await api('POST','/api/verify_otp',{code});
  if(r.ok){
    setMsg('login-msg','✅ Logged in as '+r.name+'! Go to Settings to set your folder, then start backup.','ok');
  } else {
    setMsg('login-msg','❌ '+r.error,'err');
  }
}

async function detectiCloud(){
  const r=await api('GET','/api/detect_icloud');
  document.getElementById('photos_path').value=r.path;
}

async function saveSettings(){
  const path=document.getElementById('photos_path').value.trim();
  const r=await api('POST','/api/save_config',{photos_path:path});
  setMsg('settings-msg', r.ok?'✅ Saved!':'❌ Error','ok');
}

async function startBackup(){ await api('POST','/api/start',{}); }
async function stopBackup(){ await api('POST','/api/stop',{}); }
async function quitApp(){ 
  if(confirm('This will stop the backup and completely close the background engine. Are you sure?')) {
    await api('POST','/api/quit',{}); 
    document.body.innerHTML = '<h2 style="padding:40px;text-align:center;color:#8b949e">App closed. You can now close this browser tab.</h2>';
  }
}

function setMsg(id,text,type){
  const el=document.getElementById(id);
  el.innerHTML='<div class="msg '+type+'">'+text+'</div>';
}

// ── Polling ───────────────────────────────────────────────────────────────────
let lastLogLen=0;
async function poll(){
  try{
    const d=await api('GET','/api/state');
    document.getElementById('s-count').textContent=d.count;
    document.getElementById('s-size').textContent=d.size_str;
    const badge=document.getElementById('s-status');
    badge.textContent=d.status.charAt(0).toUpperCase()+d.status.slice(1);
    badge.className='badge '+d.status;
    if(d.logs.length!==lastLogLen){
      lastLogLen=d.logs.length;
      const box=document.getElementById('logbox');
      box.innerHTML=d.logs.map(l=>'<div>'+l+'</div>').join('');
      box.scrollTop=box.scrollHeight;
    }
  }catch(e){}
}

// Load saved config into settings tab
async function loadConfig(){
  try{
    const c=await api('GET','/api/config');
    if(c.phone) document.getElementById('phone').value=c.phone;
    if(c.channel_id) document.getElementById('channel_id').value=c.channel_id;
    if(c.photos_path) document.getElementById('photos_path').value=c.photos_path;
  }catch(e){}
}

loadConfig();
setInterval(poll,2500);
poll();
</script>
</body>
</html>"""

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Check if already authorized
    if Path(SESSION_FILE).exists():
        state["authorized"] = True

    print(f"\n{'='*50}")
    print(f"  Telegram Backup Pro")
    print(f"  Open: http://localhost:{PORT}")
    print(f"{'='*50}\n")

    import webbrowser
    threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()

    server = HTTPServer(("127.0.0.1", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
