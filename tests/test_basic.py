import os
import unittest
import sqlite3
from src.db import init_db, is_uploaded, mark_uploaded, get_stats
from src.utils import format_size, build_caption
import tempfile

class TestBackupModules(unittest.TestCase):
    def setUp(self):
        # Override DB_FILE in config for isolation
        import src.config
        self.temp_db = tempfile.mktemp()
        src.config.DB_FILE = self.temp_db

    def tearDown(self):
        if os.path.exists(self.temp_db):
            os.remove(self.temp_db)

    def test_database_logic(self):
        conn = init_db()
        self.assertFalse(is_uploaded(conn, "test_hash"))
        
        mark_uploaded(conn, "test_hash", "test.jpg", 1024)
        self.assertTrue(is_uploaded(conn, "test_hash"))
        
        count, size = get_stats(conn)
        self.assertEqual(count, 1)
        self.assertEqual(size, 1024)
        conn.close()

    def test_utils(self):
        self.assertEqual(format_size(1024), "1.0 KB")
        self.assertIn("MB", format_size(1024*1024*5))
        
        cap = build_caption("IMG.heic", "/dummy/path/IMG.heic", 2048)
        self.assertIn("IMG.heic", cap)
        self.assertIn("HEIC", cap)
        self.assertIn("KB", cap)

if __name__ == "__main__":
    unittest.main()
