import db
conn = db.get_conn()
rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
print([r[0] for r in rows])
conn.close()
