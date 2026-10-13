# HackNet NOC - A lightweight, headless network monitoring dashboard for homelabs.
# Copyright (C) 2026 CB456-Dev
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import sqlite3
import os
import uuid
import hashlib
import secrets
import requests
import base64
import time
import jwt
import urllib.parse
from cryptography.fernet import Fernet
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List, Tuple
from dotenv import load_dotenv
from proxmoxer import ProxmoxAPI
import urllib3
import difflib

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
# DATABASE INITIALIZATION & MIGRATIONS
# ==========================================
@app.on_event("startup")
def init_db():
    conn = sqlite3.connect(DB_NAME, timeout=15)
    cursor = conn.cursor()
    cursor.execute('PRAGMA journal_mode=WAL;')
    
    # Core Infrastructure Table
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
        uuid TEXT DEFAULT '',
        parent_uid TEXT DEFAULT '',
        stack_group TEXT DEFAULT ''
    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS sys_settings (key TEXT PRIMARY KEY, value TEXT)''')
    
    # Metrics with WAN Jitter/Packet Loss support
    cursor.execute('''CREATE TABLE IF NOT EXISTS metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        uid TEXT, 
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, 
        entity_name TEXT, 
        entity_type TEXT, 
        status TEXT, 
        cpu_usage REAL, 
        mem_used REAL, 
        mem_total REAL, 
        disk_used REAL, 
        disk_total REAL, 
        net_in REAL, 
        net_out REAL, 
        ping_ms REAL,
        packet_loss REAL DEFAULT 0.0,
        jitter_ms REAL DEFAULT 0.0
    )''')
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS latest_metrics (
        uid TEXT PRIMARY KEY, 
        entity_name TEXT, 
        entity_type TEXT, 
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, 
        status TEXT, 
        cpu_usage REAL, 
        mem_used REAL, 
        mem_total REAL, 
        disk_used REAL, 
        disk_total REAL, 
        net_in REAL, 
        net_out REAL, 
        ping_ms REAL,
        packet_loss REAL DEFAULT 0.0,
        jitter_ms REAL DEFAULT 0.0
    )''')
    
    # Alerting & Queues
    cursor.execute('''CREATE TABLE IF NOT EXISTS alert_state (uid TEXT PRIMARY KEY, is_alerting INTEGER, first_alert_time REAL, last_alert_time REAL, count INTEGER)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS event_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, entity_name TEXT, event_type TEXT, message TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS notification_queue (id INTEGER PRIMARY KEY AUTOINCREMENT, message TEXT, added_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS pihole_stats (id INTEGER PRIMARY KEY, status TEXT, domains_blocked INTEGER, queries_today INTEGER, ads_blocked INTEGER, ads_percentage REAL)''')
    
    # Defaults
    cursor.execute('INSERT OR IGNORE INTO sys_settings (key, value) VALUES ("global_mute", "0")')
    cursor.execute('INSERT OR IGNORE INTO sys_settings (key, value) VALUES ("force_poll", "0")')
    cursor.execute('INSERT OR IGNORE INTO sys_settings (key, value) VALUES ("trigger_report", "0")')
    cursor.execute('INSERT OR IGNORE INTO sys_settings (key, value) VALUES ("is_setup_complete", "0")')
    
    # Check for missing columns (Migration safety)
    cursor.execute("PRAGMA table_info(metrics)")
    existing_metrics_cols = [info[1] for info in cursor.fetchall()]
    if 'packet_loss' not in existing_metrics_cols:
        cursor.execute("ALTER TABLE metrics ADD COLUMN packet_loss REAL DEFAULT 0.0")
        cursor.execute("ALTER TABLE metrics ADD COLUMN jitter_ms REAL DEFAULT 0.0")
    
    cursor.execute("PRAGMA table_info(latest_metrics)")
    existing_latest_cols = [info[1] for info in cursor.fetchall()]
    if 'packet_loss' not in existing_latest_cols:
        cursor.execute("ALTER TABLE latest_metrics ADD COLUMN packet_loss REAL DEFAULT 0.0")
        cursor.execute("ALTER TABLE latest_metrics ADD COLUMN jitter_ms REAL DEFAULT 0.0")

    conn.commit()
    conn.close()

# ==========================================
# HELPER FUNCTIONS
# ==========================================
def ensure_url(url: str, default_port: int = 80) -> str:
    """ Ensures URLs have http:// if missing, preventing requests.get crashes. """
    if not url: return ""
    url = url.strip().rstrip('/')
    if not url.startswith("http://") and not url.startswith("https://"):
        if ":" not in url:
            return f"http://{url}:{default_port}"
        return f"http://{url}"
    return url

# ==========================================
# SECURITY LAYER (JWT & FERNET ENCRYPTION)
# ==========================================
def get_sys_setting(key: str) -> str:
    conn = sqlite3.connect(DB_NAME, timeout=15)
    row = conn.execute("SELECT value FROM sys_settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else None

def get_fernet_cipher():
    key = get_sys_setting('sys_encryption_key')
    if not key: raise Exception("System Encryption Key missing.")
    return Fernet(key.encode())

def encrypt_secret(plain_text: str) -> str:
    if not plain_text or plain_text == "********": return plain_text
    return get_fernet_cipher().encrypt(plain_text.encode()).decode()

def decrypt_secret(cipher_text: str) -> str:
    if not cipher_text: return ""
    try:
        return get_fernet_cipher().decrypt(cipher_text.encode()).decode()
    except Exception:
        return cipher_text # Graceful fallback if plain text during migration

def _get_decrypted_plugin_val(plugin_key: str, key_name: str, provided_val: str) -> str:
    """ Used during test-connection to fetch the real DB password if the frontend sends ******** """
    if provided_val != "********":
        return provided_val
    db_key = f"plugin_{plugin_key}_{key_name}"
    conn = sqlite3.connect(DB_NAME, timeout=15)
    row = conn.execute("SELECT value FROM sys_settings WHERE key=?", (db_key,)).fetchone()
    conn.close()
    db_val = row[0] if row else ""
    if db_val:
        is_secret = any(x in db_key.lower() for x in ['token', 'secret', 'password']) and 'token_id' not in db_key.lower()
        return decrypt_secret(db_val) if is_secret else db_val
    return ""

def verify_admin(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid JWT token.")
    
    token = authorization.split(" ")[1]
    jwt_secret = get_sys_setting('jwt_secret')
    
    if not jwt_secret:
        raise HTTPException(status_code=401, detail="System not initialized.")
        
    try:
        jwt.decode(token, jwt_secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired. Please log in again.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token. Access Denied.")

# ==========================================
# SCHEMAS
# ==========================================
class InitPayload(BaseModel):
    password: str = Field(..., min_length=8)

class LoginPayload(BaseModel):
    password: str

class SetupPayload(BaseModel):
    wan_target: str
    wan_name: str
    hud_priorities: List[str]
    plugins: Dict[str, Any]
    notifications: Dict[str, Any]
    discovered_targets: Optional[List[Dict[str, Any]]] = []

class TestPayload(BaseModel):
    plugin: str
    data: Dict[str, str]

class DiscoverPayload(BaseModel):
    plugins: Dict[str, Any]

class TargetCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    entity_type: str = Field(..., pattern="^(network|app|http)$")
    target_address: Optional[str] = ""
    port: Optional[int] = 0
    cooldown_interval: Optional[int] = 3600
    location: Optional[str] = "Local"
    parent_uid: Optional[str] = ""

class TargetUpdate(BaseModel):
    uid: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    target_address: Optional[str] = ""
    port: Optional[int] = 0
    cooldown_interval: Optional[int] = 3600
    cpu_threshold: Optional[float] = 90.0
    mem_threshold: Optional[float] = 90.0
    location: Optional[str] = "Local"
    parent_uid: Optional[str] = ""
    stack_group: Optional[str] = ""

class TargetAction(BaseModel):
    uid: str = Field(..., min_length=1)

class SysSettingsUpdate(BaseModel):
    global_mute: Optional[int] = Field(None, ge=0, le=1)

# ==========================================
# CORE DISCOVERY LOGIC (Used by Setup & Admin)
# ==========================================
def _perform_discovery(plugins: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    discovered = []
    errors = []
    
    # 1. Proxmox Discovery
    px_data = plugins.get('proxmox')
    if px_data and px_data.get('ip') and px_data.get('token_id') and px_data.get('token_secret'):
        try:
            p = ProxmoxAPI(px_data['ip'], user=px_data['token_id'].split('!')[0], token_name=px_data['token_id'].split('!')[1], token_value=px_data['token_secret'], verify_ssl=False, timeout=5)
            nodes = p.nodes.get()
            if nodes:
                my_node = nodes[0]['node']
                my_node_uid = f"px_host_{my_node}"
                discovered.append({"uid": my_node_uid, "name": my_node, "type": "host", "layer": 1})
                
                for vm in p.nodes(my_node).qemu.get():
                    discovered.append({"uid": f"px_vm_{vm.get('vmid')}", "name": vm.get('name'), "type": "vm", "layer": 2, "parent_uid": my_node_uid, "stack_group": my_node})
                    
                for lxc in p.nodes(my_node).lxc.get():
                    discovered.append({"uid": f"px_lxc_{lxc.get('vmid')}", "name": lxc.get('name'), "type": "lxc", "layer": 2, "parent_uid": my_node_uid, "stack_group": my_node})
        except Exception as e:
            errors.append(f"Proxmox Discovery Failed: {str(e)}")

    # 2. ESXi Discovery
    esxi_data = plugins.get('esxi')
    if esxi_data and esxi_data.get('ip') and esxi_data.get('user') and esxi_data.get('password'):
        try:
            base_url = ensure_url(esxi_data['ip'], 443).replace("http://", "https://")
            auth_str = f"{esxi_data['user']}:{esxi_data['password']}"
            b64_auth = base64.b64encode(auth_str.encode()).decode()
            res_auth = requests.post(f"{base_url}/com/vmware/cis/session", headers={"Authorization": f"Basic {b64_auth}"}, timeout=5, verify=False)
            if res_auth.status_code == 200:
                session_token = res_auth.json().get('value')
                headers = {"vmware-api-session-id": session_token}
                h_uid = "esxi_host"
                h_name = "ESXi Host"
                discovered.append({"uid": h_uid, "name": h_name, "type": "host", "layer": 1})
                
                res_vms = requests.get(f"{base_url}/vcenter/vm", headers=headers, timeout=5, verify=False)
                if res_vms.status_code == 200:
                    for vm in res_vms.json().get('value', []):
                        discovered.append({"uid": f"esxi_vm_{vm.get('vm')}", "name": vm.get('name'), "type": "vm", "layer": 2, "parent_uid": h_uid, "stack_group": h_name})
        except Exception as e:
            errors.append(f"ESXi Discovery Failed: {str(e)}")

    # 3. TrueNAS Discovery
    tn_data = plugins.get('truenas')
    if tn_data and tn_data.get('ip') and tn_data.get('token'):
        try:
            safe_url = ensure_url(tn_data['ip'], 80)
            headers = {"Authorization": f"Bearer {tn_data['token']}", "Accept": "application/json"}
            base_url = f"{safe_url}/api/v2.0"
            
            res_info = requests.get(f"{base_url}/system/info", headers=headers, timeout=5, verify=False)
            if res_info.status_code == 200:
                hostname = res_info.json().get('hostname', 'TrueNAS')
                h_uid = "truenas_host"
                discovered.append({"uid": h_uid, "name": hostname, "type": "host", "layer": 1})
                
                res_pools = requests.get(f"{base_url}/pool", headers=headers, timeout=5, verify=False)
                if res_pools.status_code == 200:
                    for pool in res_pools.json():
                        p_name = pool.get('name', 'Unknown_Pool')
                        discovered.append({"uid": f"truenas_pool_{p_name}", "name": f"Pool-{p_name}", "type": "storage", "layer": 2, "parent_uid": h_uid, "stack_group": hostname})
        except Exception as e:
            errors.append(f"TrueNAS Discovery Failed: {str(e)}")

    # 4. Portainer (Docker)
    pt_data = plugins.get('portainer')
    if pt_data and pt_data.get('url') and pt_data.get('token'):
        try:
            url = ensure_url(pt_data['url'], 9000)
            headers = {"X-API-Key": pt_data['token']}
            eps = requests.get(f"{url}/api/endpoints", headers=headers, timeout=4, verify=False).json()
            for ep in eps:
                ep_id = ep['Id']
                conts = requests.get(f"{url}/api/endpoints/{ep_id}/docker/containers/json?all=1", headers=headers, timeout=4, verify=False).json()
                for c in conts:
                    c_name = c['Names'][0].lstrip('/')
                    discovered.append({"uid": f"dkr_{ep_id}_{c['Id'][:8]}", "name": c_name, "type": "app", "layer": 3, "sub_type": "docker"})
        except Exception as e:
            errors.append(f"Docker/Portainer Discovery Failed: {str(e)}")

    # 5. Pterodactyl
    pter_data = plugins.get('pterodactyl')
    if pter_data and pter_data.get('url') and pter_data.get('token'):
        try:
            url = ensure_url(pter_data['url'])
            headers = {"Authorization": f"Bearer {pter_data['token']}", "Accept": "application/json"}
            res = requests.get(f"{url}/api/client", headers=headers, timeout=5, verify=False)
            if res.status_code == 200:
                for server in res.json().get('data', []):
                    s_uuid = server['attributes']['uuid']
                    s_name = server['attributes']['name']
                    discovered.append({"uid": f"pt_{s_uuid}", "name": s_name, "type": "game", "layer": 3, "sub_type": "pterodactyl"})
        except Exception as e:
            errors.append(f"Pterodactyl Discovery Failed: {str(e)}")

    # 6. Synology & QNAP
    syn_data = plugins.get('synology')
    if syn_data and syn_data.get('ip') and syn_data.get('user'):
        base_url = ensure_url(syn_data['ip'], 5000)
        clean_ip = urllib.parse.urlparse(base_url).hostname or syn_data['ip']
        discovered.append({"uid": f"syn_{clean_ip}", "name": "Synology DSM", "type": "storage", "layer": 1})

    qnap_data = plugins.get('qnap')
    if qnap_data and qnap_data.get('ip') and qnap_data.get('user'):
        base_url = ensure_url(qnap_data['ip'], 8080)
        clean_ip = urllib.parse.urlparse(base_url).hostname or qnap_data['ip']
        discovered.append({"uid": f"qnap_{clean_ip}", "name": "QNAP QTS", "type": "storage", "layer": 1})

    # 7. Media & DNS
    jelly_data = plugins.get('jellyfin')
    if jelly_data and jelly_data.get('url') and jelly_data.get('token'):
        discovered.append({"uid": "app_jellyfin", "name": "Jellyfin Media Server", "type": "app", "layer": 4, "sub_type": "api"})

    plex_data = plugins.get('plex')
    if plex_data and plex_data.get('url') and plex_data.get('token'):
        discovered.append({"uid": "app_plex", "name": "Plex Media Server", "type": "app", "layer": 4, "sub_type": "api"})

    pi_data = plugins.get('pihole')
    if pi_data and pi_data.get('ip'):
        clean_ip = urllib.parse.urlparse(ensure_url(pi_data['ip'], 80)).hostname or pi_data['ip']
        discovered.append({"uid": f"pihole_{clean_ip}", "name": "Pi-Hole DNS", "type": "app", "layer": 4, "sub_type": "api"})

    # Fuzzy Matching for Apps
    stacks = []
    for item in discovered:
        if item['layer'] > 2:
            best_match = None
            best_ratio = 0.0
            
            for potential_parent in discovered:
                if potential_parent['layer'] < item['layer']:
                    ratio = difflib.SequenceMatcher(None, item['name'].lower(), potential_parent['name'].lower()).ratio()
                    if item['name'].lower() in potential_parent['name'].lower() or potential_parent['name'].lower() in item['name'].lower():
                        ratio += 0.3
                    
                    if ratio > 0.75 and ratio > best_ratio:
                        best_ratio = ratio
                        best_match = potential_parent

            if best_match:
                stacks.append({
                    "parent": best_match,
                    "child": item,
                    "confidence": best_ratio
                })

    return discovered, stacks, errors

# ==========================================
# SETUP & AUTHENTICATION ENDPOINTS
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
    
    # Security Initialization
    salt = os.urandom(32)
    pwd_hash = hashlib.pbkdf2_hmac('sha256', payload.password.encode('utf-8'), salt, 100000)
    
    # Generate System Encryption Key & JWT Secret
    sys_encryption_key = Fernet.generate_key().decode()
    jwt_secret = secrets.token_hex(32)
    
    conn.execute("INSERT INTO sys_settings (key, value) VALUES ('admin_salt', ?)", (salt.hex(),))
    conn.execute("INSERT INTO sys_settings (key, value) VALUES ('admin_hash', ?)", (pwd_hash.hex(),))
    conn.execute("INSERT INTO sys_settings (key, value) VALUES ('sys_encryption_key', ?)", (sys_encryption_key,))
    conn.execute("INSERT INTO sys_settings (key, value) VALUES ('jwt_secret', ?)", (jwt_secret,))
    conn.commit()
    conn.close()
    
    # Automatically issue token on init (Expires in 1 Hour)
    token = jwt.encode(
        {"sub": "admin", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        jwt_secret, algorithm="HS256"
    )
    return {"status": "success", "message": "Admin credentials secured.", "token": token}

@app.post("/api/admin/login")
def login(payload: LoginPayload):
    conn = sqlite3.connect(DB_NAME, timeout=15)
    pwd_hash = conn.execute("SELECT value FROM sys_settings WHERE key='admin_hash'").fetchone()
    pwd_salt = conn.execute("SELECT value FROM sys_settings WHERE key='admin_salt'").fetchone()
    jwt_secret = conn.execute("SELECT value FROM sys_settings WHERE key='jwt_secret'").fetchone()
    conn.close()

    if not pwd_hash or not pwd_salt or not jwt_secret:
        raise HTTPException(status_code=401, detail="System not initialized.")

    attempt_key = hashlib.pbkdf2_hmac('sha256', payload.password.encode('utf-8'), bytes.fromhex(pwd_salt[0]), 100000)
    
    if not secrets.compare_digest(attempt_key.hex(), pwd_hash[0]):
        raise HTTPException(status_code=401, detail="Invalid Security Key.")

    # 1 Hour Expiration Token
    token = jwt.encode(
        {"sub": "admin", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        jwt_secret[0], algorithm="HS256"
    )
    
    return {"status": "success", "token": token}

@app.post("/api/setup/test-connection")
def test_connection(payload: TestPayload):
    try:
        plugin = payload.plugin
        
        # Hydrate any masked fields from the database securely
        data = {}
        for k, v in payload.data.items():
            data[k] = _get_decrypted_plugin_val(plugin, k, v)

        if plugin == "proxmox":
            p = ProxmoxAPI(data['ip'], user=data['token_id'].split('!')[0], token_name=data['token_id'].split('!')[1], token_value=data['token_secret'], verify_ssl=False, timeout=3)
            if p.nodes.get(): 
                return {"status": "success", "message": "Proxmox Authenticated."}

        elif plugin == "portainer":
            url = ensure_url(data['url'], 9000)
            res = requests.get(f"{url}/api/system/info", headers={"X-API-Key": data['token']}, timeout=5, verify=False)
            if res.status_code == 200:
                return {"status": "success", "message": "Portainer API Authenticated."}
            return {"status": "error", "detail": f"HTTP {res.status_code} Unauthorized"}

        elif plugin == "jellyfin":
            url = ensure_url(data['url'], 8096)
            res = requests.get(f"{url}/System/Info", headers={"Authorization": f"MediaBrowser Token=\"{data['token']}\""}, timeout=5, verify=False)
            if res.status_code == 200:
                return {"status": "success", "message": "Jellyfin API Authenticated."}
            return {"status": "error", "detail": f"HTTP {res.status_code} Unauthorized"}

        elif plugin == "pterodactyl":
            url = ensure_url(data['url'])
            res = requests.get(f"{url}/api/client", headers={"Authorization": f"Bearer {data['token']}", "Accept": "application/json"}, timeout=4, verify=False)
            if res.status_code == 200: 
                return {"status": "success", "message": "Pterodactyl Authenticated."}
            return {"status": "error", "detail": f"HTTP {res.status_code} Unauthorized"}

        elif plugin == "pihole":
            url = ensure_url(data['ip'], 80)
            auth = ("", data.get('app_password', '')) if data.get('app_password') else None
            res = requests.get(f"{url}/api/stats/summary", auth=auth, timeout=3, verify=False)
            # If v6 endpoint fails, fall back to v5
            if res.status_code == 404:
                res = requests.get(f"{url}/admin/api.php?summary", auth=auth, timeout=3, verify=False)
            if res.status_code == 200: 
                return {"status": "success", "message": "Pi-Hole Connected."}
            return {"status": "error", "detail": f"HTTP {res.status_code}"}

        elif plugin == "truenas":
            url = ensure_url(data['ip'], 80)
            res = requests.get(f"{url}/api/v2.0/system/info", headers={"Authorization": f"Bearer {data['token']}"}, timeout=3, verify=False)
            if res.status_code == 200: 
                return {"status": "success", "message": "TrueNAS Connected."}
            return {"status": "error", "detail": f"HTTP {res.status_code}"}

        elif plugin == "esxi":
            base_url = ensure_url(data['ip'], 443).replace("http://", "https://")
            auth_str = f"{data['user']}:{data['password']}"
            b64_auth = base64.b64encode(auth_str.encode()).decode()
            res_auth = requests.post(f"{base_url}/rest/com/vmware/cis/session", headers={"Authorization": f"Basic {b64_auth}"}, timeout=4, verify=False)
            res_auth.raise_for_status()
            session_token = res_auth.json().get('value')
            
            res_host = requests.get(f"{base_url}/rest/vcenter/host", headers={"vmware-api-session-id": session_token}, timeout=3, verify=False)
            if res_host.status_code == 200:
                return {"status": "success", "message": "ESXi Authenticated."}
            return {"status": "error", "detail": f"HTTP {res_host.status_code}"}

        elif plugin == "synology":
            base_url = ensure_url(data['ip'], 5000)
            encoded_user = urllib.parse.quote(data['user'])
            encoded_pwd = urllib.parse.quote(data['password'])
            url = f"{base_url}/webapi/auth.cgi?api=SYNO.API.Auth&version=3&method=login&account={encoded_user}&passwd={encoded_pwd}"
            
            res = requests.get(url, timeout=5, verify=False)
            if res.status_code == 200 and res.json().get('success'):
                return {"status": "success", "message": "Synology DSM Authenticated."}
            return {"status": "error", "detail": "Invalid Credentials or Host Unreachable"}

        elif plugin == "qnap":
            base_url = ensure_url(data['ip'], 8080)
            encoded_user = urllib.parse.quote(data['user'])
            b64_pwd = base64.b64encode(data['password'].encode()).decode()
            
            success = False
            for test_path in ["authLogin.cgi", "filemanager/wfm2Login.cgi"]:
                for pwd_payload in [urllib.parse.quote(data['password']), urllib.parse.quote(b64_pwd)]:
                    try:
                        url = f"{base_url}/cgi-bin/{test_path}?user={encoded_user}&pwd={pwd_payload}"
                        res = requests.get(url, timeout=4, verify=False)
                        if res.status_code == 200 and ('status="1"' in res.text or ('authPassed' in res.text and '<![CDATA[1]]' in res.text)):
                            success = True
                            break
                    except Exception: pass
                if success: break
            
            if success:
                return {"status": "success", "message": "QNAP QTS Authenticated."}
            return {"status": "error", "detail": "Invalid Credentials or Host Unreachable"}

        elif plugin == "plex":
            url = ensure_url(data['url'], 32400)
            res = requests.get(f"{url}/status/sessions?X-Plex-Token={data['token']}", headers={"Accept": "application/json"}, timeout=3, verify=False)
            if res.status_code == 200: 
                return {"status": "success", "message": "Plex Authenticated."}
            return {"status": "error", "detail": f"HTTP {res.status_code}"}

    except Exception as e:
        return {"status": "error", "detail": f"Connection Failed: {str(e)}"}
    
    return {"status": "error", "detail": "Unknown plugin or validation failed."}

@app.post("/api/setup/discover", dependencies=[Depends(verify_admin)])
def run_infrastructure_discovery(payload: DiscoverPayload):
    discovered, stacks, errors = _perform_discovery(payload.plugins)
    
    return {
        "status": "success",
        "discovered": discovered,
        "proposed_stacks": stacks,
        "errors": errors
    }

@app.post("/api/setup/finalize", dependencies=[Depends(verify_admin)])
async def finalize_setup(payload: SetupPayload):
    conn = sqlite3.connect(DB_NAME, timeout=15)
    
    wan_uid = "mnl_wan_primary"
    conn.execute("DELETE FROM monitored_targets WHERE uid=?", (wan_uid,))
    conn.execute('''INSERT INTO monitored_targets 
                    (uid, name, entity_type, target_address, is_enabled, cooldown_interval, location) 
                    VALUES (?, ?, 'network', ?, 1, 600, 'WAN')''', 
                 (wan_uid, payload.wan_name or "WAN Gateway", payload.wan_target))

    conn.execute("INSERT OR REPLACE INTO sys_settings (key, value) VALUES ('hud_priorities', ?)", (",".join(payload.hud_priorities),))

    # Strict save logic. Ignore 'token_id', only encrypt actual secrets.
    for k, v in payload.plugins.items():
        if v and v != "********": 
            is_secret = any(x in k.lower() for x in ['secret', 'password', 'token']) and 'token_id' not in k.lower()
            final_val = encrypt_secret(str(v)) if is_secret else str(v)
            conn.execute("INSERT OR REPLACE INTO sys_settings (key, value) VALUES (?, ?)", (f"plugin_{k}", final_val))
            
    for k, v in payload.notifications.items():
        if v and v != "********": 
            is_secret = any(x in k.lower() for x in ['secret', 'password', 'token', 'webhook'])
            final_val = encrypt_secret(str(v)) if is_secret else str(v)
            conn.execute("INSERT OR REPLACE INTO sys_settings (key, value) VALUES (?, ?)", (f"notify_{k}", final_val))
            
    for key_row in conn.execute("SELECT key FROM sys_settings WHERE key LIKE 'plugin_%' OR key LIKE 'notify_%'").fetchall():
        key = key_row[0]
        base_k = key.replace('plugin_', '').replace('notify_', '')
        if key.startswith('plugin_') and base_k not in payload.plugins:
            conn.execute("DELETE FROM sys_settings WHERE key=?", (key,))
        elif key.startswith('notify_') and base_k not in payload.notifications:
            conn.execute("DELETE FROM sys_settings WHERE key=?", (key,))

    for target in payload.discovered_targets:
        conn.execute('''INSERT OR REPLACE INTO monitored_targets 
                        (uid, name, entity_type, is_enabled, location, parent_uid, stack_group) 
                        VALUES (?, ?, ?, ?, ?, ?, ?)''', 
                     (target['uid'], target['name'], target['type'], 
                      target.get('is_enabled', 1), target.get('location', 'Local'), 
                      target.get('parent_uid', ''), target.get('stack_group', '')))
    
    conn.execute("INSERT OR REPLACE INTO sys_settings (key, value) VALUES ('is_setup_complete', '1')")
    
    conn.commit()
    conn.close()
    return {"status": "success"}

# ==========================================
# PUBLIC & DASHBOARD ENDPOINTS
# ==========================================
@app.get("/api/live")
def live_data(authorization: str = Header(None)):
    try:
        if authorization: 
            verify_admin(authorization)
        
        conn = sqlite3.connect(DB_NAME, timeout=15)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        is_setup = cursor.execute('SELECT value FROM sys_settings WHERE key="is_setup_complete"').fetchone()
        if not is_setup or is_setup['value'] == '0':
            return {"status": "success", "is_setup_complete": False}

        cursor.execute('''
            SELECT t.uid, t.name AS entity_name, t.entity_type, t.is_enabled, t.down_since, t.target_address, t.port, 
                   t.cooldown_interval, t.cpu_threshold, t.mem_threshold, t.location, t.parent_uid, t.stack_group,
                   m.status, m.cpu_usage, m.mem_used, m.mem_total, m.disk_used, m.disk_total, m.ping_ms, m.packet_loss, m.jitter_ms, m.net_in, m.net_out
            FROM monitored_targets t
            LEFT JOIN latest_metrics m ON m.uid = t.uid
            ORDER BY CASE t.entity_type WHEN 'network' THEN 1 WHEN 'host' THEN 2 WHEN 'storage' THEN 3 WHEN 'lxc' THEN 4 WHEN 'vm' THEN 5 WHEN 'app' THEN 6 WHEN 'http' THEN 7 WHEN 'game' THEN 8 ELSE 9 END, LOWER(t.name) ASC
        ''')
        targets = [dict(r) for r in cursor.fetchall()]

        safe_keys = ['global_mute', 'hud_priorities', 'is_setup_complete', 'host_uptime']
        placeholders = ','.join(['?'] * len(safe_keys))
        cursor.execute(f'SELECT key, value FROM sys_settings WHERE key IN ({placeholders})', safe_keys)
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
@app.get("/api/admin/config", dependencies=[Depends(verify_admin)])
def get_integrations():
    conn = sqlite3.connect(DB_NAME, timeout=15)
    conn.row_factory = sqlite3.Row
    config = {r['key']: r['value'] for r in conn.execute('SELECT key, value FROM sys_settings').fetchall()}
    conn.close()
    
    safe_data = {}
    for k in config.keys():
        if k.startswith('plugin_') or k.startswith('notify_'):
            is_secret = any(x in k.lower() for x in ["secret", "password", "token", "webhook"]) and 'token_id' not in k.lower()
            val = config.get(k, "")
            if is_secret and val:
                safe_data[k] = "********"
            else:
                safe_data[k] = val
                
    return {"status": "success", "data": safe_data}

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
                    (uid, name, entity_type, target_address, port, is_enabled, down_since, cooldown_interval, location, parent_uid) 
                    VALUES (?, ?, ?, ?, ?, 1, 0.0, ?, ?, ?)''', 
                 (new_uid, payload.name, payload.entity_type, payload.target_address, payload.port, 
                  payload.cooldown_interval, payload.location, payload.parent_uid))
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.put("/api/admin/targets", dependencies=[Depends(verify_admin)])
async def edit_admin_target(payload: TargetUpdate):
    conn = sqlite3.connect(DB_NAME, timeout=15)
    conn.execute('''UPDATE monitored_targets 
                    SET name=?, target_address=?, port=?, cooldown_interval=?, cpu_threshold=?, mem_threshold=?, location=?, parent_uid=?, stack_group=?
                    WHERE uid=?''', 
                 (payload.name, payload.target_address, payload.port, payload.cooldown_interval, 
                  payload.cpu_threshold, payload.mem_threshold, payload.location, payload.parent_uid, payload.stack_group, payload.uid))
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

@app.post("/api/admin/trigger-report", dependencies=[Depends(verify_admin)])
async def trigger_report():
    conn = sqlite3.connect(DB_NAME, timeout=15)
    conn.execute("INSERT OR REPLACE INTO sys_settings (key, value) VALUES ('trigger_report', '1')")
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

@app.post("/api/admin/scan-devices", dependencies=[Depends(verify_admin)])
def admin_scan_devices():
    conn = sqlite3.connect(DB_NAME, timeout=15)
    conn.row_factory = sqlite3.Row
    config = {r['key']: r['value'] for r in conn.execute('SELECT key, value FROM sys_settings WHERE key LIKE "plugin_%"').fetchall()}
    
    def _d(key):
        val = config.get(key, "")
        is_secret = any(x in key.lower() for x in ['secret', 'password', 'token']) and 'token_id' not in key.lower()
        return decrypt_secret(val) if is_secret and val else val
        
    plugins = {
        "proxmox": {"ip": _d("plugin_proxmox_ip"), "token_id": _d("plugin_proxmox_token_id"), "token_secret": _d("plugin_proxmox_token_secret")},
        "portainer": {"url": _d("plugin_portainer_url"), "token": _d("plugin_portainer_token")},
        "jellyfin": {"url": _d("plugin_jellyfin_url"), "token": _d("plugin_jellyfin_token")},
        "plex": {"url": _d("plugin_plex_url"), "token": _d("plugin_plex_token")},
        "synology": {"ip": _d("plugin_synology_ip"), "user": _d("plugin_synology_user"), "password": _d("plugin_synology_password")},
        "qnap": {"ip": _d("plugin_qnap_ip"), "user": _d("plugin_qnap_user"), "password": _d("plugin_qnap_password")},
        "esxi": {"ip": _d("plugin_esxi_ip"), "user": _d("plugin_esxi_user"), "password": _d("plugin_esxi_password")},
        "truenas": {"ip": _d("plugin_truenas_ip"), "token": _d("plugin_truenas_token")},
        "pterodactyl": {"url": _d("plugin_pterodactyl_url"), "token": _d("plugin_pterodactyl_token")},
        "pihole": {"ip": _d("plugin_pihole_ip"), "app_password": _d("plugin_pihole_app_password")}
    }
    
    discovered, proposed_stacks, errors = _perform_discovery(plugins)
    
    existing_uids = set(r['uid'] for r in conn.execute("SELECT uid FROM monitored_targets").fetchall())
    new_targets = [d for d in discovered if d['uid'] not in existing_uids]
    
    added_count = 0
    for t in new_targets:
        parent_uid = ""
        stack_group = ""
        for stack in proposed_stacks:
            if stack['child']['uid'] == t['uid']:
                parent_uid = stack['parent']['uid']
                stack_group = stack['parent']['name']
        
        conn.execute('''INSERT INTO monitored_targets 
                        (uid, name, entity_type, is_enabled, location, parent_uid, stack_group) 
                        VALUES (?, ?, ?, 1, 'Local', ?, ?)''', 
                     (t['uid'], t['name'], t['type'], parent_uid, stack_group))
        added_count += 1
        
    conn.commit()
    conn.close()
    
    return {"status": "success", "added_count": added_count, "errors": errors}

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

    

    
    
      

    


    

    




    