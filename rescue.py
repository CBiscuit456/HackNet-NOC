import sqlite3
import os

print("Rebuilding database schema...")
conn = sqlite3.connect('metrics.db')
c = conn.cursor()

# 1. Force create the missing tables that I accidentally removed
c.execute('CREATE TABLE IF NOT EXISTS metrics (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, entity_name TEXT, entity_type TEXT, status TEXT, cpu_usage REAL, mem_used REAL, mem_total REAL, disk_used REAL, disk_total REAL, net_in REAL, net_out REAL, ping_ms REAL)')
c.execute('CREATE TABLE IF NOT EXISTS alert_state (entity_metric TEXT PRIMARY KEY, is_alerting INTEGER, first_alert_time REAL, last_alert_time REAL, count INTEGER)')
c.execute('CREATE TABLE IF NOT EXISTS monitored_targets (name TEXT, entity_type TEXT, target_address TEXT, port INTEGER, uuid TEXT, is_enabled INTEGER DEFAULT 1, down_since REAL DEFAULT 0.0, PRIMARY KEY(name, entity_type))')

# 2. Salvage newer sensors from the corrupt DB (in case you added some after July)
try:
    if os.path.exists('metrics.db.corrupt'):
        old = sqlite3.connect('metrics.db.corrupt')
        targets = old.execute('SELECT * FROM monitored_targets').fetchall()
        added = 0
        for t in targets:
            try:
                c.execute('INSERT OR IGNORE INTO monitored_targets VALUES (?,?,?,?,?,?,?)', t)
                added += 1
            except: pass
        if added > 0:
            print(f"✅ Rescued {added} sensors from the corrupted database!")
except Exception as e:
    pass

conn.commit()
conn.close()
print("✅ Database is fully repaired and ready.")
