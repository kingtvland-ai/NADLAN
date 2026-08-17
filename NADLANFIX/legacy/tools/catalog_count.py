import sqlite3

import db


conn = sqlite3.connect(db.DB_PATH, timeout=2)
try:
    print(conn.execute("SELECT COUNT(*) FROM property_catalog").fetchone()[0])
finally:
    conn.close()
