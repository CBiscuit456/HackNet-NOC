import sqlite3
import os
import secrets
from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from typing import Optional
from dotenv import load_dotenv

# ==========================================
# SECRETS & CONFIG
# ==========================================
load_dotenv()
ADMIN_SECRET = os.getenv('ADMIN_SECRET_KEY', 'change_me')
DB_NAME = 'metrics.db'

app = FastAPI(title="HackNet NOC API", version="2.0.0")

# 1. CORS HARDENING: Strip the wildcard, allow only internal/local network origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8000", 
        "http://127.0.0.1:8000", 
        "http://10.10.10.109:8000",
        "http://hacknet-noc:8000"
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ==========================================
# STRICT PYDANTIC SCHEMAS (Input Validation)
# ==========================================
class TargetCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=50, description="Sensor Name")
    entity_type: str = Field(..., pattern="^(network|app|game|host|vm|lxc)$")
    target_address: Optional[str] = ""
    port: Optional[int] = 0
    uuid: Optional[str] = ""
    cooldown_interval: Optional[int] = 3600
    cpu_threshold: Optional[float] = 90.0
    mem_threshold: Optional[float] = 90.0

class TargetUpdate(TargetCreate):
    old_name: str = Field(..., min_length=1)
    old_type: str = Field(..., min_length=1)

class TargetAction(BaseModel):
    name: str = Field(..., min_length=1)
    entity_type: str = Field(..., min_length=1)

class SysSettingsUpdate(BaseModel):
    global_mute: Optional[int] = Field(None, ge=0, le=1)

# ==========================================
# SECURITY LAYER (Cryptographic Auth)
# ==========================================
def verify_admin(x_api_key: str = Header(None)):
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing Authorization Header.")
    # 2. TIMING ATTACK PREVENTION: Constant-time string comparison
    if not secrets.compare_digest(x_api_key, ADMIN_SECRET):
        raise HTTPException(status_code=401, detail="Unauthorized Access.")

# ==========================================
# PUBLIC ENDPOINTS
# ==========================================
@app.get("/api/live")
def live_data():
    try:
        conn = sqlite3.connect(DB_NAME, timeout=15)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT t.name AS entity_name, t.entity_type, t.is_enabled, t.down_since, t.target_address, t.port, t.uuid, 
                   t.cooldown_interval, t.cpu_threshold, t.mem_threshold,
                   m.status, m.cpu_usage, m.mem_used, m.mem_total, m.ping_ms
            FROM monitored_targets t
            LEFT JOIN metrics m ON m.entity_name = t.name AND m.entity_type = t.entity_type
            AND m.id = (SELECT MAX(id) FROM metrics WHERE entity_name = t.name AND entity_type = t.entity_type)
            ORDER BY CASE t.entity_type WHEN 'network' THEN 1 WHEN 'host' THEN 2 WHEN 'lxc' THEN 3 WHEN 'vm' THEN 4 WHEN 'app' THEN 5 WHEN 'game' THEN 6 ELSE 7 END, LOWER(t.name) ASC
        ''')
        targets = [dict(r) for r in cursor.fetchall()]

        cursor.execute('SELECT key, value FROM sys_settings')
        settings = {r['key']: r['value'] for r in cursor.fetchall()}

        pihole = {}
        try:
            r = cursor.execute('SELECT * FROM pihole_stats LIMIT 1').fetchone()
            if r: pihole = dict(r)
        except Exception: pass

        wan_history = []
        try:
            cursor.execute('''
                SELECT status, ping_ms FROM metrics 
                WHERE entity_name = 'WAN_DNS' AND entity_type = 'network' 
                ORDER BY timestamp DESC LIMIT 60
            ''')
            rows = cursor.fetchall()
            for r in reversed(rows):
                wan_history.append(r['ping_ms'] if r['status'] == 'running' else 0.0)
        except Exception: pass

        conn.close()
        return {
            "status": "success", 
            "data": targets, 
            "settings": settings, 
            "pihole": pihole,
            "wan_history": wan_history
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": "Database Read Failure"})

# ==========================================
# SECURE ADMIN ENDPOINTS
# ==========================================
@app.get("/api/admin/events", dependencies=[Depends(verify_admin)])
def get_audit_logs():
    conn = sqlite3.connect(DB_NAME, timeout=15)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT timestamp, entity_name, event_type, message FROM event_logs ORDER BY timestamp DESC LIMIT 50')
        logs = [dict(r) for r in cursor.fetchall()]
    except Exception:
        logs = []
    finally:
        conn.close()
    return {"status": "success", "data": logs}

@app.post("/api/admin/targets", dependencies=[Depends(verify_admin)])
async def add_admin_target(payload: TargetCreate):
    conn = sqlite3.connect(DB_NAME, timeout=15)
    conn.execute('''INSERT OR REPLACE INTO monitored_targets 
                    (name, entity_type, target_address, port, uuid, is_enabled, down_since, cooldown_interval, cpu_threshold, mem_threshold) 
                    VALUES (?, ?, ?, ?, ?, 1, 0.0, ?, ?, ?)''', 
                 (payload.name, payload.entity_type, payload.target_address, payload.port, payload.uuid, 
                  payload.cooldown_interval, payload.cpu_threshold, payload.mem_threshold))
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.put("/api/admin/targets", dependencies=[Depends(verify_admin)])
async def edit_admin_target(payload: TargetUpdate):
    conn = sqlite3.connect(DB_NAME, timeout=15)
    
    # Update core config
    conn.execute('''UPDATE monitored_targets 
                    SET name=?, entity_type=?, target_address=?, port=?, uuid=?, cooldown_interval=?, cpu_threshold=?, mem_threshold=? 
                    WHERE name=? AND entity_type=?''', 
                 (payload.name, payload.entity_type, payload.target_address, payload.port, payload.uuid, 
                  payload.cooldown_interval, payload.cpu_threshold, payload.mem_threshold, 
                  payload.old_name, payload.old_type))
    
    # Safe cascade updates for historical data
    conn.execute('UPDATE metrics SET entity_name=?, entity_type=? WHERE entity_name=? AND entity_type=?', 
                 (payload.name, payload.entity_type, payload.old_name, payload.old_type))
    try:
        conn.execute('UPDATE event_logs SET entity_name=? WHERE entity_name=?', (payload.name, payload.old_name))
    except Exception: pass
    
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.delete("/api/admin/targets", dependencies=[Depends(verify_admin)])
async def delete_admin_target(payload: TargetAction):
    conn = sqlite3.connect(DB_NAME, timeout=15)
    conn.execute('DELETE FROM monitored_targets WHERE name = ? AND entity_type = ?', (payload.name, payload.entity_type))
    conn.execute('DELETE FROM metrics WHERE entity_name = ? AND entity_type = ?', (payload.name, payload.entity_type))
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.post("/api/admin/toggle", dependencies=[Depends(verify_admin)])
async def toggle_target(payload: TargetAction):
    conn = sqlite3.connect(DB_NAME, timeout=15)
    row = conn.execute('SELECT is_enabled FROM monitored_targets WHERE name = ? AND entity_type = ?', (payload.name, payload.entity_type)).fetchone()
    if row:
        new_state = 0 if row[0] == 1 else 1
        conn.execute('UPDATE monitored_targets SET is_enabled = ?, down_since = 0.0 WHERE name = ? AND entity_type = ?', (new_state, payload.name, payload.entity_type))
        conn.commit()
        conn.close()
        return {"status": "success", "new_state": new_state}
    conn.close()
    raise HTTPException(status_code=404, detail="Target not found")

@app.post("/api/admin/force-poll", dependencies=[Depends(verify_admin)])
async def force_poll():
    conn = sqlite3.connect(DB_NAME, timeout=15)
    conn.execute("UPDATE sys_settings SET value='1' WHERE key='force_poll'")
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.post("/api/admin/sys-settings", dependencies=[Depends(verify_admin)])
async def update_sys_settings(payload: SysSettingsUpdate):
    conn = sqlite3.connect(DB_NAME, timeout=15)
    if payload.global_mute is not None:
        conn.execute("UPDATE sys_settings SET value=? WHERE key='global_mute'", (str(payload.global_mute),))
    conn.commit()
    conn.close()
    return {"status": "success"}

# ==========================================
# WEB UI ROUTES
# ==========================================
@app.get("/")
def serve_dashboard(): 
    return FileResponse("static/index.html")

@app.get("/admin")
def serve_admin(): 
    return FileResponse("static/admin.html")

    




    