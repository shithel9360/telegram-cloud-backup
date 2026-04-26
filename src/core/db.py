import sqlite3
import src.config

def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(src.config.DB_FILE, check_same_thread=False)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS uploads (
                    uuid      TEXT PRIMARY KEY,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    filename  TEXT,
                    file_size INTEGER DEFAULT 0
                 )""")
    try:
        c.execute("ALTER TABLE uploads ADD COLUMN file_size INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    return conn

def is_uploaded(conn: sqlite3.Connection, file_hash: str) -> bool:
    c = conn.cursor()
    c.execute("SELECT 1 FROM uploads WHERE uuid = ?", (file_hash,))
    return bool(c.fetchone())

def mark_uploaded(conn: sqlite3.Connection, file_hash: str, filename: str, size: int = 0):
    c = conn.cursor()
    c.execute("REPLACE INTO uploads (uuid, filename, file_size) VALUES (?, ?, ?)",
              (file_hash, filename, size))
    conn.commit()

def get_stats(conn: sqlite3.Connection):
    c = conn.cursor()
    c.execute("SELECT COUNT(*), SUM(file_size) FROM uploads WHERE filename NOT LIKE 'SKIPPED%'")
    r = c.fetchone()
    return r[0] or 0, r[1] or 0
