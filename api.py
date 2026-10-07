import sqlite3
import os
import uuid
import hashlib
import secrets
import requests
import base64
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from dotenv import load_dotenv
from proxmoxer import ProxmoxAPI
import urllib3

# Suppress insecure HTTPS warnings for local self-signed homelab APIs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

load_dotenv()
DB_NAME = os.getenv('DB_NAME', 'data/metrics.db')

app = FastAPI(title="HackNet NOC API", version="2.8.ENT")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

os.makedirs("static", exist_ok=True)
os.makedirs(os.path.dirname(DB_NAME) or ".", exist_ok=True)

# ==========================================
# DATABASE INITIALIZATION
# ==========================================
@app.on_event("startup")
def init_db():
    conn = sqlite3.connect(DB_NAME, timeout=15)
    cursor = conn.cursor()
    cursor.execute('PRAGMA journal_mode=WAL;')
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS monitored_targets (
        uid TEXT PRIMARY KEY,
        name TEXT, 
        entity_type TEXT, 
        target_address TEXT, 
        port INTEGER, 
        is_enabled INTEGER DEFAULT 1, 
        down_since REAL DEFAULT 0.0,
        cooldown_interval INTEGER DEFAULT 3600,
        cpu_threshold REAL DEFAULT 90.0,
        mem_threshold REAL DEFAULT 90.0,
        location TEXT DEFAULT 'Local',
        uuid TEXT DEFAULT ''
    )''')
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS sys_settings (key TEXT PRIMARY KEY, value TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS metrics (id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, entity_name TEXT, entity_type TEXT, status TEXT, cpu_usage REAL, mem_used REAL, mem_total REAL, disk_used REAL, disk_total REAL, net_in REAL, net_out REAL, ping_ms REAL)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS latest_metrics (uid TEXT PRIMARY KEY, entity_name TEXT, entity_type TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, status TEXT, cpu_usage REAL, mem_used REAL, mem_total REAL, disk_used REAL, disk_total REAL, net_in REAL, net_out REAL, ping_ms REAL)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS alert_state (uid TEXT PRIMARY KEY, is_alerting INTEGER, first_alert_time REAL, last_alert_time REAL, count INTEGER)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS event_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, entity_name TEXT, event_type TEXT, message TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS pihole_stats (id INTEGER PRIMARY KEY, status TEXT, domains_blocked INTEGER, queries_today INTEGER, ads_blocked INTEGER, ads_percentage REAL)''')
    
    cursor.execute('INSERT OR IGNORE INTO sys_settings (key, value) VALUES ("global_mute", "0")')
    cursor.execute('INSERT OR IGNORE INTO sys_settings (key, value) VALUES ("force_poll", "0")')
    cursor.execute('INSERT OR IGNORE INTO sys_settings (key, value) VALUES ("is_setup_complete", "0")')
    conn.commit()
    conn.close()

# ==========================================
# AUTHENTICATION LAYER
# ==========================================
def verify_admin(x_api_key: str = Header(None)):
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing Authorization Header.")
    
    conn = sqlite3.connect(DB_NAME, timeout=15)
    pwd_hash = conn.execute("SELECT value FROM sys_settings WHERE key='admin_hash'").fetchone()
    pwd_salt = conn.execute("SELECT value FROM sys_settings WHERE key='admin_salt'").fetchone()
    conn.close()

    if not pwd_hash or not pwd_salt:
        raise HTTPException(status_code=401, detail="System not initialized. Please complete setup.")

    attempt_key = hashlib.pbkdf2_hmac('sha256', x_api_key.encode('utf-8'), bytes.fromhex(pwd_salt[0]), 100000)
    
    if not secrets.compare_digest(attempt_key.hex(), pwd_hash[0]):
        raise HTTPException(status_code=401, detail="Invalid Security Key.")

# ==========================================
# SCHEMAS
# ==========================================
class InitPayload(BaseModel):
    password: str = Field(..., min_length=8)

class SetupPayload(BaseModel):
    wan_target: str
    wan_name: str
    hud_priorities: List[str]
    plugins: Dict[str, Any]
    notifications: Dict[str, Any]

class TestPayload(BaseModel):
    plugin: str
    data: Dict[str, str]

class TargetCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    entity_type: str = Field(..., pattern="^(network|app|http)$")
    target_address: Optional[str] = ""
    port: Optional[int] = 0
    cooldown_interval: Optional[int] = 3600
    location: Optional[str] = "Local"

class TargetUpdate(BaseModel):
    uid: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    target_address: Optional[str] = ""
    port: Optional[int] = 0
    cooldown_interval: Optional[int] = 3600
    cpu_threshold: Optional[float] = 90.0
    mem_threshold: Optional[float] = 90.0
    location: Optional[str] = "Local"

class TargetAction(BaseModel):
    uid: str = Field(..., min_length=1)

class SysSettingsUpdate(BaseModel):
    global_mute: Optional[int] = Field(None, ge=0, le=1)

# ==========================================
# SETUP & INITIALIZATION ENDPOINTS
# ==========================================
@app.get("/api/setup/status")
def get_setup_status():
    conn = sqlite3.connect(DB_NAME, timeout=15)
    has_admin = conn.execute("SELECT value FROM sys_settings WHERE key='admin_hash'").fetchone() is not None
    is_complete = conn.execute("SELECT value FROM sys_settings WHERE key='is_setup_complete'").fetchone()
    conn.close()
    
    return {
        "admin_exists": has_admin,
        "is_setup_complete": bool(int(is_complete[0])) if is_complete else False
    }

@app.post("/api/setup/init")
def initialize_admin(payload: InitPayload):
    conn = sqlite3.connect(DB_NAME, timeout=15)
    if conn.execute("SELECT value FROM sys_settings WHERE key='admin_hash'").fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Admin account already exists.")
    
    salt = os.urandom(32)
    pwd_hash = hashlib.pbkdf2_hmac('sha256', payload.password.encode('utf-8'), salt, 100000)
    
    conn.execute("INSERT INTO sys_settings (key, value) VALUES ('admin_salt', ?)", (salt.hex(),))
    conn.execute("INSERT INTO sys_settings (key, value) VALUES ('admin_hash', ?)", (pwd_hash.hex(),))
    conn.commit()
    conn.close()
    return {"status": "success", "message": "Admin credentials secured."}

@app.post("/api/setup/test-connection")
def test_connection(payload: TestPayload):
    """ Dynamically validates integration credentials before allowing a deploy. """
    try:
        plugin = payload.plugin
        data = payload.data

        if plugin == "proxmox":
            p = ProxmoxAPI(data['ip'], user=data['token_id'].split('!')[0], token_name=data['token_id'].split('!')[1], token_value=data['token_secret'], verify_ssl=False, timeout=3)
            if p.nodes.get(): 
                return {"status": "success", "message": "Proxmox Authenticated."}

        elif plugin == "pterodactyl":
            res = requests.get(f"{data['url']}/api/client", headers={"Authorization": f"Bearer {data['token']}", "Accept": "application/json"}, timeout=3)
            if res.status_code == 200: 
                return {"status": "success", "message": "Pterodactyl Authenticated."}
            return {"status": "error", "detail": f"HTTP {res.status_code} Unauthorized"}

        elif plugin == "pihole":
            # Some PiHole instances use auth=("", app_password), others use URL queries. We attempt standard auth here.
            res = requests.get(f"http://{data['ip']}/api/stats/summary", auth=("", data.get('app_password', '')), timeout=3, verify=False)
            if res.status_code == 200: 
                return {"status": "success", "message": "Pi-Hole Connected."}
            return {"status": "error", "detail": f"HTTP {res.status_code}"}

        elif plugin == "truenas":
            res = requests.get(f"http://{data['ip']}/api/v2.0/system/info", headers={"Authorization": f"Bearer {data['token']}"}, timeout=3, verify=False)
            if res.status_code == 200: 
                return {"status": "success", "message": "TrueNAS Connected."}
            return {"status": "error", "detail": f"HTTP {res.status_code}"}

        elif plugin == "esxi":
            auth_str = f"{data['user']}:{data['password']}"
            b64_auth = base64.b64encode(auth_str.encode()).decode()
            res_auth = requests.post(f"https://{data['ip']}/rest/com/vmware/cis/session", headers={"Authorization": f"Basic {b64_auth}"}, timeout=3, verify=False)
            res_auth.raise_for_status()
            session_token = res_auth.json().get('value')
            
            res_host = requests.get(f"https://{data['ip']}/rest/vcenter/host", headers={"vmware-api-session-id": session_token}, timeout=3, verify=False)
            if res_host.status_code == 200:
                return {"status": "success", "message": "ESXi Authenticated."}
            return {"status": "error", "detail": f"HTTP {res_host.status_code}"}

        elif plugin == "plex":
            res = requests.get(f"{data['url']}/status/sessions?X-Plex-Token={data['token']}", headers={"Accept": "application/json"}, timeout=3, verify=False)
            if res.status_code == 200: 
                return {"status": "success", "message": "Plex Authenticated."}
            return {"status": "error", "detail": f"HTTP {res.status_code}"}

    except Exception as e:
        return {"status": "error", "detail": f"Connection Failed: {str(e)}"}
    
    return {"status": "error", "detail": "Unknown plugin or validation failed."}

@app.post("/api/setup/finalize", dependencies=[Depends(verify_admin)])
async def finalize_setup(payload: SetupPayload):
    conn = sqlite3.connect(DB_NAME, timeout=15)
    
    # 1. Setup the WAN Target
    wan_uid = "mnl_wan_primary"
    conn.execute("DELETE FROM monitored_targets WHERE uid=?", (wan_uid,))
    conn.execute('''INSERT INTO monitored_targets 
                    (uid, name, entity_type, target_address, is_enabled, cooldown_interval, location) 
                    VALUES (?, ?, 'network', ?, 1, 600, 'WAN')''', 
                 (wan_uid, payload.wan_name or "WAN Gateway", payload.wan_target))

    # 2. Save HUD Priorities
    conn.execute("INSERT OR REPLACE INTO sys_settings (key, value) VALUES ('hud_priorities', ?)", (",".join(payload.hud_priorities),))

    # 3. Save Plugins & Notifications
    conn.execute("DELETE FROM sys_settings WHERE key LIKE 'plugin_%' OR key LIKE 'notify_%'")
    for k, v in payload.plugins.items():
        if v and "***" not in str(v): conn.execute("INSERT INTO sys_settings (key, value) VALUES (?, ?)", (f"plugin_{k}", str(v)))
    for k, v in payload.notifications.items():
        if v and "***" not in str(v): conn.execute("INSERT INTO sys_settings (key, value) VALUES (?, ?)", (f"notify_{k}", str(v)))
    
    conn.execute("INSERT OR REPLACE INTO sys_settings (key, value) VALUES ('is_setup_complete', '1')")
    
    conn.commit()
    conn.close()
    return {"status": "success"}

# ==========================================
# PUBLIC & DASHBOARD ENDPOINTS
# ==========================================
@app.get("/api/live")
def live_data(x_api_key: str = Header(None)):
    try:
        if x_api_key: verify_admin(x_api_key)
        
        conn = sqlite3.connect(DB_NAME, timeout=15)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        is_setup = cursor.execute('SELECT value FROM sys_settings WHERE key="is_setup_complete"').fetchone()
        if not is_setup or is_setup['value'] == '0':
            return {"status": "success", "is_setup_complete": False}

        cursor.execute('''
            SELECT t.uid, t.name AS entity_name, t.entity_type, t.is_enabled, t.down_since, t.target_address, t.port, 
                   t.cooldown_interval, t.cpu_threshold, t.mem_threshold, t.location,
                   m.status, m.cpu_usage, m.mem_used, m.mem_total, m.disk_used, m.disk_total, m.ping_ms
            FROM monitored_targets t
            LEFT JOIN latest_metrics m ON m.uid = t.uid
            ORDER BY CASE t.entity_type WHEN 'network' THEN 1 WHEN 'host' THEN 2 WHEN 'storage' THEN 3 WHEN 'lxc' THEN 4 WHEN 'vm' THEN 5 WHEN 'app' THEN 6 WHEN 'http' THEN 7 WHEN 'game' THEN 8 ELSE 9 END, LOWER(t.name) ASC
        ''')
        targets = [dict(r) for r in cursor.fetchall()]

        cursor.execute('SELECT key, value FROM sys_settings')
        settings = {r['key']: r['value'] for r in cursor.fetchall()}

        pihole = {}
        try:
            r = cursor.execute('SELECT * FROM pihole_stats LIMIT 1').fetchone()
            if r: pihole = dict(r)
        except Exception: pass

        sparklines = {}
        try:
            cursor.execute('''SELECT uid, ping_ms, cpu_usage FROM metrics WHERE timestamp >= datetime('now', '-10 minutes') ORDER BY timestamp ASC''')
            for r in cursor.fetchall():
                key = r['uid']
                if key not in sparklines: sparklines[key] = {'ping': [], 'cpu': []}
                sparklines[key]['ping'].append(r['ping_ms'] or 0)
                sparklines[key]['cpu'].append(r['cpu_usage'] or 0)
            for k in sparklines:
                sparklines[k]['ping'] = sparklines[k]['ping'][-60:]
                sparklines[k]['cpu'] = sparklines[k]['cpu'][-60:]
        except Exception: pass
        
        wan_history = sparklines.get("mnl_wan_primary", {}).get('ping', [])

        conn.close()
        return {
            "status": "success", 
            "is_setup_complete": True,
            "data": targets, 
            "settings": settings, 
            "pihole": pihole,
            "wan_history": wan_history,
            "sparklines": sparklines
        }
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content={"status": "error", "detail": e.detail})
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

# ==========================================
# SECURE ADMIN ENDPOINTS
# ==========================================
def mask_secret(secret: str) -> str:
    if not secret: return ""
    if len(secret) <= 8: return "********"
    return secret[:4] + "..." + secret[-4:]

@app.get("/api/admin/config", dependencies=[Depends(verify_admin)])
def get_integrations():
    conn = sqlite3.connect(DB_NAME, timeout=15)
    conn.row_factory = sqlite3.Row
    config = {r['key']: r['value'] for r in conn.execute('SELECT key, value FROM sys_settings').fetchall()}
    conn.close()
    
    def get_mask(k): return mask_secret(config.get(k, "")) if any(x in k for x in ["token", "secret", "password", "webhook"]) else config.get(k, "")
    
    return {
        "status": "success",
        "data": {k: get_mask(k) for k in config.keys() if k.startswith('plugin_') or k.startswith('notify_')}
    }

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
    new_uid = f"mnl_{uuid.uuid4().hex[:12]}"
    conn = sqlite3.connect(DB_NAME, timeout=15)
    conn.execute('''INSERT INTO monitored_targets 
                    (uid, name, entity_type, target_address, port, is_enabled, down_since, cooldown_interval, location) 
                    VALUES (?, ?, ?, ?, ?, 1, 0.0, ?, ?)''', 
                 (new_uid, payload.name, payload.entity_type, payload.target_address, payload.port, 
                  payload.cooldown_interval, payload.location))
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.put("/api/admin/targets", dependencies=[Depends(verify_admin)])
async def edit_admin_target(payload: TargetUpdate):
    conn = sqlite3.connect(DB_NAME, timeout=15)
    conn.execute('''UPDATE monitored_targets 
                    SET name=?, target_address=?, port=?, cooldown_interval=?, cpu_threshold=?, mem_threshold=?, location=? 
                    WHERE uid=?''', 
                 (payload.name, payload.target_address, payload.port, payload.cooldown_interval, 
                  payload.cpu_threshold, payload.mem_threshold, payload.location, payload.uid))
    conn.execute('UPDATE latest_metrics SET entity_name=? WHERE uid=?', (payload.name, payload.uid))
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.delete("/api/admin/targets", dependencies=[Depends(verify_admin)])
async def delete_admin_target(payload: TargetAction):
    conn = sqlite3.connect(DB_NAME, timeout=15)
    conn.execute('DELETE FROM monitored_targets WHERE uid = ?', (payload.uid,))
    conn.execute('DELETE FROM metrics WHERE uid = ?', (payload.uid,))
    conn.execute('DELETE FROM latest_metrics WHERE uid = ?', (payload.uid,))
    conn.execute('DELETE FROM alert_state WHERE uid = ?', (payload.uid,))
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.post("/api/admin/toggle", dependencies=[Depends(verify_admin)])
async def toggle_target(payload: TargetAction):
    conn = sqlite3.connect(DB_NAME, timeout=15)
    row = conn.execute('SELECT is_enabled FROM monitored_targets WHERE uid = ?', (payload.uid,)).fetchone()
    if row:
        new_state = 0 if row[0] == 1 else 1
        conn.execute('UPDATE monitored_targets SET is_enabled = ?, down_since = 0.0 WHERE uid = ?', (new_state, payload.uid))
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
        conn.execute("INSERT OR REPLACE INTO sys_settings (key, value) VALUES ('global_mute', ?)", (str(payload.global_mute),))
    conn.commit()
    conn.close()
    return {"status": "success"}

# ==========================================
# WEB UI ROUTES
# ==========================================
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def serve_dashboard(): 
    return FileResponse("static/index.html")

@app.get("/setup")
def serve_setup(): 
    return FileResponse("static/setup.html")

@app.get("/admin")
def serve_admin(): 
    return FileResponse("static/admin.html")

    


    

    




    