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
# STRICT PYDANTIC SCHEMAS
# ==========================================
class TargetCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=50, description="Sensor Name")
    entity_type: str = Field(..., pattern="^(network|app|game|host|vm|lxc|storage)$")
    target_address: Optional[str] = ""
    port: Optional[int] = 0
    uuid: Optional[str] = ""
    cooldown_interval: Optional[int] = 3600
    cpu_threshold: Optional[float] = 90.0
    mem_threshold: Optional[float] = 90.0
    location: Optional[str] = "Local"

class TargetUpdate(TargetCreate):
    old_name: str = Field(..., min_length=1)
    old_type: str = Field(..., min_length=1)

class TargetAction(BaseModel):
    name: str = Field(..., min_length=1)
    entity_type: str = Field(..., min_length=1)

class SysSettingsUpdate(BaseModel):
    global_mute: Optional[int] = Field(None, ge=0, le=1)

# ==========================================
# SECURITY LAYER
# ==========================================
def verify_admin(x_api_key: str = Header(None)):
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing Authorization Header.")
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
        
        # High-performance join using the new latest_metrics state table
        cursor.execute('''
            SELECT t.name AS entity_name, t.entity_type, t.is_enabled, t.down_since, t.target_address, t.port, t.uuid, 
                   t.cooldown_interval, t.cpu_threshold, t.mem_threshold, t.location,
                   m.status, m.cpu_usage, m.mem_used, m.mem_total, m.disk_used, m.disk_total, m.ping_ms
            FROM monitored_targets t
            LEFT JOIN latest_metrics m ON m.entity_name = t.name AND m.entity_type = t.entity_type
            ORDER BY CASE t.entity_type WHEN 'network' THEN 1 WHEN 'host' THEN 2 WHEN 'storage' THEN 3 WHEN 'lxc' THEN 4 WHEN 'vm' THEN 5 WHEN 'app' THEN 6 WHEN 'game' THEN 7 ELSE 8 END, LOWER(t.name) ASC
        ''')
        targets = [dict(r) for r in cursor.fetchall()]

        cursor.execute('SELECT key, value FROM sys_settings')
        settings = {r['key']: r['value'] for r in cursor.fetchall()}

        pihole = {}
        try:
            r = cursor.execute('SELECT * FROM pihole_stats LIMIT 1').fetchone()
            if r: pihole = dict(r)
        except Exception: pass

        # Fetch efficient historical sparkline data (last 10 mins)
        sparklines = {}
        try:
            cursor.execute('''
                SELECT entity_name, entity_type, ping_ms, cpu_usage 
                FROM metrics 
                WHERE timestamp >= datetime('now', '-10 minutes')
                ORDER BY timestamp ASC
            ''')
            rows = cursor.fetchall()
            for r in rows:
                key = f"{r['entity_name']}_{r['entity_type']}"
                if key not in sparklines: sparklines[key] = {'ping': [], 'cpu': []}
                sparklines[key]['ping'].append(r['ping_ms'] or 0)
                sparklines[key]['cpu'].append(r['cpu_usage'] or 0)
            
            # Bound array size for UI efficiency
            for k in sparklines:
                sparklines[k]['ping'] = sparklines[k]['ping'][-60:]
                sparklines[k]['cpu'] = sparklines[k]['cpu'][-60:]
        except Exception: pass
        
        wan_history = []
        try:
            wan_key = "WAN_DNS_network"
            if wan_key in sparklines: wan_history = sparklines[wan_key]['ping']
        except Exception: pass

        conn.close()
        return {
            "status": "success", 
            "data": targets, 
            "settings": settings, 
            "pihole": pihole,
            "wan_history": wan_history,
            "sparklines": sparklines
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

# ==========================================
# SECURE ADMIN ENDPOINTS
# ==========================================
@app.get("/api/admin/events", dependencies=[Depends(verify_admin)])
def get_audit_logs():
    conn = sqlite3.connect(DB_NAME, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        logs = [dict(r) for r in conn.execute('SELECT timestamp, entity_name, event_type, message FROM event_logs ORDER BY timestamp DESC LIMIT 50').fetchall()]
    except Exception:
        logs = []
    finally:
        conn.close()
    return {"status": "success", "data": logs}

@app.post("/api/admin/targets", dependencies=[Depends(verify_admin)])
async def add_admin_target(payload: TargetCreate):
    conn = sqlite3.connect(DB_NAME, timeout=15)
    conn.execute('''INSERT OR REPLACE INTO monitored_targets 
                    (name, entity_type, target_address, port, uuid, is_enabled, down_since, cooldown_interval, cpu_threshold, mem_threshold, location) 
                    VALUES (?, ?, ?, ?, ?, 1, 0.0, ?, ?, ?, ?)''', 
                 (payload.name, payload.entity_type, payload.target_address, payload.port, payload.uuid, 
                  payload.cooldown_interval, payload.cpu_threshold, payload.mem_threshold, payload.location))
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.put("/api/admin/targets", dependencies=[Depends(verify_admin)])
async def edit_admin_target(payload: TargetUpdate):
    conn = sqlite3.connect(DB_NAME, timeout=15)
    
    conn.execute('''UPDATE monitored_targets 
                    SET name=?, entity_type=?, target_address=?, port=?, uuid=?, cooldown_interval=?, cpu_threshold=?, mem_threshold=?, location=? 
                    WHERE name=? AND entity_type=?''', 
                 (payload.name, payload.entity_type, payload.target_address, payload.port, payload.uuid, 
                  payload.cooldown_interval, payload.cpu_threshold, payload.mem_threshold, payload.location, 
                  payload.old_name, payload.old_type))
    
    # Safe cascade updates
    conn.execute('UPDATE metrics SET entity_name=?, entity_type=? WHERE entity_name=? AND entity_type=?', 
                 (payload.name, payload.entity_type, payload.old_name, payload.old_type))
    conn.execute('UPDATE latest_metrics SET entity_name=?, entity_type=? WHERE entity_name=? AND entity_type=?', 
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
    conn.execute('DELETE FROM latest_metrics WHERE entity_name = ? AND entity_type = ?', (payload.name, payload.entity_type))
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

@app.get("/")
def serve_dashboard(): 
    return FileResponse("static/index.html")

@app.get("/admin")
def serve_admin(): 
    return FileResponse("static/admin.html")

    

    




    