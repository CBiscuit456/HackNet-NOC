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

import time
import sqlite3
import subprocess
import socket
import requests
import os
import datetime
import urllib3
import re
import base64
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from proxmoxer import ProxmoxAPI
from cryptography.fernet import Fernet
from dotenv import load_dotenv

# Suppress insecure HTTPS warnings for local self-signed homelab APIs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

load_dotenv()
DB_NAME = os.getenv('DB_NAME', 'data/metrics.db')

# In-memory state tracking to prevent alert/log spam
ALERT_STATE = {}
PLUGIN_ERROR_STATE = {}
PTERO_LIMITS_CACHE = {}
LAST_DAILY_REPORT_TIME = None

GAME_ESCALATION_TIME = 86400
GAME_ESCALATION_COOLDOWN = 86400
POLL_INTERVAL = 10

# ==========================================
# HELPER FUNCTIONS & SECURITY
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

def parse_nas_input(user_input: str, default_port: int) -> str:
    return ensure_url(user_input, default_port)

def decrypt_secret(cipher_text: str, key: str) -> str:
    if not cipher_text or cipher_text == "********": return ""
    try:
        cipher = Fernet(key.encode())
        return cipher.decrypt(cipher_text.encode()).decode()
    except Exception:
        return cipher_text # Fallback if unencrypted during migration

def format_time_delta(seconds):
    days, seconds = divmod(int(seconds), 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days > 0: parts.append(f"{days}d")
    if hours > 0: parts.append(f"{hours}h")
    if minutes > 0: parts.append(f"{minutes}m")
    return " ".join(parts) if parts else "0m"

def log_event(entity_name, event_type, message, db_conn=None):
    try:
        if db_conn:
            db_conn.execute('INSERT INTO event_logs (entity_name, event_type, message) VALUES (?, ?, ?)', (entity_name, event_type, message))
        else:
            conn = sqlite3.connect(DB_NAME, timeout=15)
            conn.execute('INSERT INTO event_logs (entity_name, event_type, message) VALUES (?, ?, ?)', (entity_name, event_type, message))
            conn.commit()
            conn.close()
    except Exception:
        pass

def send_notification_sync(message, settings):
    tg_token = decrypt_secret(settings.get('notify_telegram_token'), settings.get('sys_encryption_key'))
    tg_chat = settings.get('notify_telegram_chat_id')
    discord_url = decrypt_secret(settings.get('notify_discord_webhook_url'), settings.get('sys_encryption_key'))
    
    success = False
    
    if tg_token and tg_chat:
        try: 
            r = requests.post(f"https://api.telegram.org/bot{tg_token}/sendMessage", 
                          json={"chat_id": tg_chat, "text": message, "parse_mode": "HTML"}, timeout=5)
            if r.status_code == 200: success = True
        except Exception: pass
    
    if discord_url:
        try:
            # Convert HTML layout to Discord-native Markdown
            discord_msg = message.replace('<b>', '**').replace('</b>', '**').replace('<i>', '*').replace('</i>', '*').replace('<br>', '\n')
            r = requests.post(discord_url, json={"content": discord_msg}, timeout=5)
            if r.status_code in [200, 204]: success = True
        except Exception: pass
        
    return success

def flush_notification_queue(settings):
    try:
        conn = sqlite3.connect(DB_NAME, timeout=15)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, message FROM notification_queue ORDER BY id ASC").fetchall()
        
        for r in rows:
            if send_notification_sync(r['message'], settings):
                conn.execute("DELETE FROM notification_queue WHERE id=?", (r['id'],))
            else:
                break 
                
        conn.commit()
        conn.close()
    except Exception: pass

def get_settings():
    try:
        conn = sqlite3.connect(DB_NAME, timeout=15)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT key, value FROM sys_settings").fetchall()
        conn.close()
        return {r['key']: r['value'] for r in rows}
    except Exception:
        return {}

def generate_full_report(title="📊 <b>HackNet Status Report</b>"):
    settings = get_settings()
    if not settings.get('notify_telegram_token') and not settings.get('notify_discord_webhook_url'): return

    try:
        conn = sqlite3.connect(DB_NAME, timeout=15)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT m.entity_name, m.entity_type, m.status, m.ping_ms, m.cpu_usage, m.mem_used, m.mem_total, t.is_enabled FROM latest_metrics m LEFT JOIN monitored_targets t ON m.uid = t.uid").fetchall()
        conn.close()
    except Exception: return

    net_down, vm_down, lxc_down, app_down, game_down, storage_down, http_down = [], [], [], [], [], [], []
    vms_tot, lxc_tot, app_tot, game_tot, storage_tot, http_tot = 0, 0, 0, 0, 0, 0
    host_str, net_wan = "UNCONFIGURED", "OFFLINE"

    for r in rows:
        n, t = r['entity_name'], r['entity_type']
        if r['is_enabled'] == 0 or r['status'] == 'paused':
            continue
        is_up = (r['status'] == 'running')
        if t == 'network':
            if not is_up: net_down.append(n)
            if 'WAN' in n.upper() and is_up: net_wan = f"{r['ping_ms']:.0f}ms"
        elif t == 'host':
            if is_up:
                mem_pct = (r['mem_used'] / max(r['mem_total'] or 1.0, 1.0)) * 100
                host_str = f"{n} - {r['cpu_usage']:.1f}% CPU | {mem_pct:.1f}% RAM"
            else:
                host_str = f"CRITICAL OFFLINE ({n})"
        elif t == 'storage':
            storage_tot += 1
            if not is_up: storage_down.append(n)
        elif t == 'vm':
            vms_tot += 1
            if not is_up: vm_down.append(n)
        elif t == 'lxc':
            lxc_tot += 1
            if not is_up: lxc_down.append(n)
        elif t == 'app':
            app_tot += 1
            if not is_up: app_down.append(n)
        elif t == 'http':
            http_tot += 1
            if not is_up: http_down.append(n)
        elif t == 'game':
            game_tot += 1
            if not is_up: game_down.append(n)

    def fmt(tot, down):
        if tot == 0: return "⚪ N/A"
        return f"🟢 {tot - len(down)}/{tot} ONLINE" if not down else f"🔴 {tot - len(down)}/{tot} ONLINE\n└ <i>Down: {', '.join(down)}</i>"
        
    msg = f"{title}\n\n"
    msg += f"🌐 <b>Network (WAN):</b> {'🟢 ONLINE' if not net_down else '🔴 OFFLINE'} (Avg: {net_wan})\n"
    msg += f"🖥️ <b>Compute Core:</b> {host_str}\n\n"
    msg += "<b>Infrastructure Matrix:</b>\n"
    msg += f"💾 Storage Arrays: {fmt(storage_tot, storage_down)}\n"
    msg += f"📦 Virtual Machines: {fmt(vms_tot, vm_down)}\n"
    msg += f"📦 LXC Containers: {fmt(lxc_tot, lxc_down)}\n"
    msg += f"🔌 Services & Apps: {fmt(app_tot, app_down)}\n"
    msg += f"🌍 Web Endpoints: {fmt(http_tot, http_down)}\n"
    msg += f"🎮 Game Servers: {fmt(game_tot, game_down)}"
    
    if not send_notification_sync(msg, settings):
        try:
            conn = sqlite3.connect(DB_NAME, timeout=15)
            conn.execute('INSERT INTO notification_queue (message) VALUES (?)', (msg,))
            conn.commit()
            conn.close()
        except Exception: pass

def process_alert(uid, entity_name, metric, condition_met, trigger_count, 
                  alert_body, recover_body, log_trigger, log_recover, 
                  down_since, is_game, global_mute, cooldown_interval, 
                  silent_resolve, settings, alert_updates, notification_queue, event_logs):
    key = f"{uid}_{metric}"
    if key not in ALERT_STATE: ALERT_STATE[key] = {'count': 0, 'is_alerting': False, 'first_alert_time': 0, 'last_alert_time': 0}
    state = ALERT_STATE[key]
    state_changed = False
    now = time.time()
    
    if condition_met:
        if state['count'] < trigger_count:
            state['count'] += 1
            state_changed = True
            
        if state['count'] >= trigger_count:
            if not state['is_alerting']:
                if not global_mute: 
                    notification_queue.append(alert_body)
                event_logs.append((entity_name, 'CRITICAL', log_trigger))
                state['is_alerting'] = True
                state['first_alert_time'] = down_since if down_since > 0 else now
                state['last_alert_time'] = now
                state_changed = True
            else:
                time_active = now - state['first_alert_time']
                cooldown = GAME_ESCALATION_COOLDOWN if (is_game and time_active >= GAME_ESCALATION_TIME) else cooldown_interval
                if (now - state['last_alert_time']) > cooldown:
                    duration_str = format_time_delta(time_active)
                    duration_text = f"Down for {duration_str}" if metric == 'offline' else f"Condition active for {duration_str}"
                    if not global_mute: 
                        escalation_body = alert_body.replace("🚨", "⚠️").replace("SYSTEM OFFLINE", "ESCALATION: STILL OFFLINE")
                        notification_queue.append(f"{escalation_body}\n\n<i>Note: {duration_text}</i>")
                    state['last_alert_time'] = now
                    state_changed = True
    else:
        if state['is_alerting']:
            duration_str = format_time_delta(now - state['first_alert_time'])
            duration_text = f"Total downtime: {duration_str}" if metric == 'offline' else f"Duration: {duration_str}"
            
            if not silent_resolve:
                if not global_mute: 
                    notification_queue.append(f"{recover_body}\n\n<i>{duration_text}</i>")
                event_logs.append((entity_name, 'RESOLVED', f"{log_recover} ({duration_text})"))
                
            state['is_alerting'] = False
            state['first_alert_time'] = 0
            state['count'] = 0
            state_changed = True
        elif state['count'] > 0:
            state['count'] = 0
            state_changed = True
            
    if state_changed: 
        alert_updates.append((key, int(state['is_alerting']), state['first_alert_time'], state['last_alert_time'], state['count']))

# ==========================================
# OBJECT-ORIENTED PLUGIN ARCHITECTURE
# ==========================================

class PluginContext:
    def __init__(self, settings, cfg, executor, enc_key):
        self.settings = settings
        self.cfg = cfg
        self.executor = executor
        self.enc_key = enc_key
        self.seen_uids = set()
        self.metrics_batch = []
        self.new_entities = []
        self.sys_updates = []
        self.futures = []

class BasePlugin:
    def __init__(self, ctx: PluginContext):
        self.ctx = ctx
        
    def run(self):
        pass

    def mark_offline(self, prefix: str):
        for uid, data in self.ctx.cfg.items():
            if uid.startswith(prefix) and data['enabled'] != 0:
                self.ctx.metrics_batch.append((uid, data['name'], data['type'], 'stopped', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

class PortainerPlugin(BasePlugin):
    def run(self):
        url = self.ctx.settings.get('plugin_portainer_url')
        token = decrypt_secret(self.ctx.settings.get('plugin_portainer_token'), self.ctx.enc_key)
        if not (url and token): return
        prefix = "dkr_"
        try:
            safe_url = ensure_url(url, 9000)
            headers = {"X-API-Key": token}
            eps = requests.get(f"{safe_url}/api/endpoints", headers=headers, timeout=6, verify=False).json()
            
            for ep in eps:
                ep_id = ep['Id']
                conts = requests.get(f"{safe_url}/api/endpoints/{ep_id}/docker/containers/json?all=1", headers=headers, timeout=6, verify=False).json()
                
                for c in conts:
                    c_id = c['Id'][:8]
                    c_name = c['Names'][0].lstrip('/')
                    uid = f"{prefix}{ep_id}_{c_id}"
                    
                    self.ctx.seen_uids.add(uid)
                    if uid not in self.ctx.cfg:
                        self.ctx.new_entities.append((uid, c_name, 'app', safe_url, 0, c['Id'], 'Local', '', ''))
                    
                    if self.ctx.cfg.get(uid, {}).get('enabled', 1) != 0:
                        status = 'running' if c['State'] == 'running' else 'stopped'
                        self.ctx.metrics_batch.append((uid, c_name, 'app', status, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
                    else:
                        self.ctx.metrics_batch.append((uid, c_name, 'app', 'paused', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
                        
            if PLUGIN_ERROR_STATE.get('portainer'):
                log_event("Portainer Integration", "RESOLVED", "Connection restored.")
                PLUGIN_ERROR_STATE['portainer'] = False
        except Exception as e:
            self.mark_offline(prefix)
            if not PLUGIN_ERROR_STATE.get('portainer'):
                log_event("Portainer Integration", "CRITICAL", f"Failed to reach Portainer API: {str(e)}")
                PLUGIN_ERROR_STATE['portainer'] = True

class JellyfinPlugin(BasePlugin):
    def run(self):
        url = self.ctx.settings.get('plugin_jellyfin_url')
        token = decrypt_secret(self.ctx.settings.get('plugin_jellyfin_token'), self.ctx.enc_key)
        if not (url and token): return

        uid = "app_jellyfin"
        name = "Jellyfin Media Server"
        self.ctx.seen_uids.add(uid)

        if uid not in self.ctx.cfg:
            self.ctx.new_entities.append((uid, name, 'app', ensure_url(url, 8096), 0, '', 'Local', '', ''))

        if self.ctx.cfg.get(uid, {}).get('enabled', 1) != 0:
            try:
                safe_url = ensure_url(url, 8096)
                res = requests.get(f"{safe_url}/System/Info", headers={"Authorization": f"MediaBrowser Token=\"{token}\""}, timeout=5, verify=False)
                res.raise_for_status()
                self.ctx.metrics_batch.append((uid, name, 'app', 'running', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
                if PLUGIN_ERROR_STATE.get('jellyfin'):
                    log_event("Jellyfin Integration", "RESOLVED", "Connection restored.")
                    PLUGIN_ERROR_STATE['jellyfin'] = False
            except Exception as e:
                self.ctx.metrics_batch.append((uid, name, 'app', 'stopped', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
                if not PLUGIN_ERROR_STATE.get('jellyfin'):
                    log_event("Jellyfin Integration", "CRITICAL", f"Failed to reach Jellyfin API: {str(e)}")
                    PLUGIN_ERROR_STATE['jellyfin'] = True
        else:
            self.ctx.metrics_batch.append((uid, name, 'app', 'paused', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

class PiholePlugin(BasePlugin):
    def run(self):
        ip = self.ctx.settings.get('plugin_pihole_ip')
        pwd = decrypt_secret(self.ctx.settings.get('plugin_pihole_app_password'), self.ctx.enc_key)
        if not ip: return
        
        clean_ip = urllib.parse.urlparse(ensure_url(ip, 80)).hostname or ip
        uid = f"pihole_{clean_ip}"
        name = "Pi-Hole DNS"
        
        self.ctx.seen_uids.add(uid)
        
        if uid not in self.ctx.cfg:
            self.ctx.new_entities.append((uid, name, 'app', ensure_url(ip, 80), 0, '', 'Local', '', ''))

        if self.ctx.cfg.get(uid, {}).get('enabled', 1) != 0:
            try:
                safe_url = ensure_url(ip, 80)
                auth = ("", pwd) if pwd else None
                res = requests.get(f"{safe_url}/api/stats/summary", auth=auth, timeout=5, verify=False)
                
                # Fall back to v5 endpoint if v6 fails
                if res.status_code == 404:
                    res = requests.get(f"{safe_url}/admin/api.php?summary", auth=auth, timeout=5, verify=False)
                res.raise_for_status()
                
                d = res.json()
                status_val = d.get('status', d.get('state', 'unknown'))
                
                domains_blocked = d.get('domains_being_blocked')
                if domains_blocked is None and 'gravity' in d:
                    domains_blocked = d['gravity'].get('domains_being_blocked', 0)
                if domains_blocked is None: domains_blocked = 0
                
                queries = d.get('dns_queries_today')
                if queries is None and 'queries' in d:
                    queries = d['queries'].get('total', 0)
                if queries is None: queries = 0
                
                ads_blocked = d.get('ads_blocked_today')
                if ads_blocked is None and 'queries' in d:
                    ads_blocked = d['queries'].get('blocked', 0)
                if ads_blocked is None: ads_blocked = 0
                
                ads_percentage = d.get('ads_percentage_today')
                if ads_percentage is None and 'queries' in d:
                    ads_percentage = d['queries'].get('percent_blocked', 0.0)
                if ads_percentage is None: ads_percentage = 0.0
                
                conn = sqlite3.connect(DB_NAME, timeout=15)
                conn.execute('DELETE FROM pihole_stats')
                conn.execute('INSERT INTO pihole_stats (id, status, domains_blocked, queries_today, ads_blocked, ads_percentage) VALUES (1, ?, ?, ?, ?, ?)', 
                             (status_val, domains_blocked, queries, ads_blocked, ads_percentage))
                conn.commit()
                conn.close()
                
                self.ctx.metrics_batch.append((uid, name, 'app', 'running', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
                
                if PLUGIN_ERROR_STATE.get('pihole'):
                    log_event("Pi-Hole Integration", "RESOLVED", "Connection to Pi-Hole restored.")
                    PLUGIN_ERROR_STATE['pihole'] = False
                    
            except Exception as e:
                self.ctx.metrics_batch.append((uid, name, 'app', 'stopped', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
                if not PLUGIN_ERROR_STATE.get('pihole'):
                    log_event("Pi-Hole Integration", "CRITICAL", f"Failed to reach Pi-Hole API: {str(e)}")
                    PLUGIN_ERROR_STATE['pihole'] = True
        else:
            self.ctx.metrics_batch.append((uid, name, 'app', 'paused', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

class PlexPlugin(BasePlugin):
    def run(self):
        url = self.ctx.settings.get('plugin_plex_url')
        token = decrypt_secret(self.ctx.settings.get('plugin_plex_token'), self.ctx.enc_key)
        if not (url and token): return

        uid = "app_plex"
        name = "Plex Media Server"
        self.ctx.seen_uids.add(uid)

        if uid not in self.ctx.cfg:
            self.ctx.new_entities.append((uid, name, 'app', ensure_url(url, 32400), 0, '', 'Local', '', ''))

        if self.ctx.cfg.get(uid, {}).get('enabled', 1) != 0:
            try:
                safe_url = ensure_url(url, 32400)
                res = requests.get(f"{safe_url}/status/sessions?X-Plex-Token={token}", headers={"Accept": "application/json"}, timeout=5, verify=False)
                res.raise_for_status()
                self.ctx.metrics_batch.append((uid, name, 'app', 'running', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
                if PLUGIN_ERROR_STATE.get('plex'):
                    log_event("Plex Integration", "RESOLVED", "Connection restored.")
                    PLUGIN_ERROR_STATE['plex'] = False
            except Exception as e:
                self.ctx.metrics_batch.append((uid, name, 'app', 'stopped', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
                if not PLUGIN_ERROR_STATE.get('plex'):
                    log_event("Plex Integration", "CRITICAL", f"Failed to reach Plex API: {str(e)}")
                    PLUGIN_ERROR_STATE['plex'] = True
        else:
            self.ctx.metrics_batch.append((uid, name, 'app', 'paused', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

class TrueNASPlugin(BasePlugin):
    def run(self):
        ip = self.ctx.settings.get('plugin_truenas_ip')
        token = decrypt_secret(self.ctx.settings.get('plugin_truenas_token'), self.ctx.enc_key)
        if not (ip and token): return

        safe_url = ensure_url(ip, 80)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        base_url = f"{safe_url}/api/v2.0"
        prefix = "truenas_"

        try:
            res_info = requests.get(f"{base_url}/system/info", headers=headers, timeout=6, verify=False)
            res_info.raise_for_status()
            sys_info = res_info.json()
            hostname = sys_info.get('hostname', 'TrueNAS')

            h_uid = f"{prefix}host"
            self.ctx.seen_uids.add(h_uid)
            if h_uid not in self.ctx.cfg:
                self.ctx.new_entities.append((h_uid, hostname, 'host', ip, 0, '', 'Local', '', ''))
            
            if self.ctx.cfg.get(h_uid, {}).get('enabled', 1) != 0:
                self.ctx.metrics_batch.append((h_uid, hostname, 'host', 'running', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
            else:
                self.ctx.metrics_batch.append((h_uid, hostname, 'host', 'paused', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

            res_pools = requests.get(f"{base_url}/pool", headers=headers, timeout=6, verify=False)
            if res_pools.status_code == 200:
                for pool in res_pools.json():
                    p_name = pool.get('name', 'Unknown_Pool')
                    p_uid = f"{prefix}pool_{p_name}"
                    self.ctx.seen_uids.add(p_uid)

                    if p_uid not in self.ctx.cfg:
                        self.ctx.new_entities.append((p_uid, f"Pool-{p_name}", 'storage', '', 0, '', 'Local', h_uid, ''))

                    if self.ctx.cfg.get(p_uid, {}).get('enabled', 1) != 0:
                        status = 'running' if pool.get('status') == 'ONLINE' else 'stopped'
                        self.ctx.metrics_batch.append((p_uid, f"Pool-{p_name}", 'storage', status, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
                    else:
                        self.ctx.metrics_batch.append((p_uid, f"Pool-{p_name}", 'storage', 'paused', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

            if PLUGIN_ERROR_STATE.get('truenas'):
                log_event("TrueNAS Integration", "RESOLVED", "Connection restored.")
                PLUGIN_ERROR_STATE['truenas'] = False

        except Exception as e:
            self.mark_offline(prefix)
            if not PLUGIN_ERROR_STATE.get('truenas'):
                log_event("TrueNAS Integration", "CRITICAL", f"Failed to reach TrueNAS API: {str(e)}")
                PLUGIN_ERROR_STATE['truenas'] = True

class ESXiPlugin(BasePlugin):
    def run(self):
        ip = self.ctx.settings.get('plugin_esxi_ip')
        user = self.ctx.settings.get('plugin_esxi_user')
        pwd = decrypt_secret(self.ctx.settings.get('plugin_esxi_password'), self.ctx.enc_key)
        if not (ip and user and pwd): return

        safe_url = ensure_url(ip, 443).replace("http://", "https://")
        base_url = f"{safe_url}/rest"
        prefix = "esxi_"

        try:
            auth_str = f"{user}:{pwd}"
            b64_auth = base64.b64encode(auth_str.encode()).decode()
            res_auth = requests.post(f"{base_url}/com/vmware/cis/session", headers={"Authorization": f"Basic {b64_auth}"}, timeout=6, verify=False)
            res_auth.raise_for_status()
            session_token = res_auth.json().get('value')
            headers = {"vmware-api-session-id": session_token}

            h_uid = f"{prefix}host"
            self.ctx.seen_uids.add(h_uid)
            if h_uid not in self.ctx.cfg:
                self.ctx.new_entities.append((h_uid, "ESXi Host", 'host', ip, 0, '', 'Local', '', ''))
            
            if self.ctx.cfg.get(h_uid, {}).get('enabled', 1) != 0:
                self.ctx.metrics_batch.append((h_uid, "ESXi Host", 'host', 'running', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
            else:
                self.ctx.metrics_batch.append((h_uid, "ESXi Host", 'host', 'paused', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

            res_vms = requests.get(f"{base_url}/vcenter/vm", headers=headers, timeout=6, verify=False)
            if res_vms.status_code == 200:
                for vm in res_vms.json().get('value', []):
                    vm_id, vm_name = vm.get('vm'), vm.get('name')
                    v_uid = f"{prefix}vm_{vm_id}"
                    self.ctx.seen_uids.add(v_uid)

                    if v_uid not in self.ctx.cfg:
                        self.ctx.new_entities.append((v_uid, vm_name, 'vm', '', 0, '', 'Local', h_uid, ''))

                    if self.ctx.cfg.get(v_uid, {}).get('enabled', 1) != 0:
                        status = 'running' if vm.get('power_state') == 'POWERED_ON' else 'stopped'
                        self.ctx.metrics_batch.append((v_uid, vm_name, 'vm', status, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
                    else:
                        self.ctx.metrics_batch.append((v_uid, vm_name, 'vm', 'paused', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

            if PLUGIN_ERROR_STATE.get('esxi'):
                log_event("VMware ESXi Integration", "RESOLVED", "Connection restored.")
                PLUGIN_ERROR_STATE['esxi'] = False

        except Exception as e:
            self.mark_offline(prefix)
            if not PLUGIN_ERROR_STATE.get('esxi'):
                log_event("VMware ESXi Integration", "CRITICAL", f"Failed to reach vSphere REST API: {str(e)}")
                PLUGIN_ERROR_STATE['esxi'] = True

class ProxmoxPlugin(BasePlugin):
    def run(self):
        px_ip = self.ctx.settings.get('plugin_proxmox_ip')
        px_id = self.ctx.settings.get('plugin_proxmox_token_id')
        px_secret = decrypt_secret(self.ctx.settings.get('plugin_proxmox_token_secret'), self.ctx.enc_key)
        if not (px_ip and px_id and px_secret): return
        
        prefix = "px_"
        try:
            if '!' not in px_id: raise ValueError("Invalid Token ID. Expected user@pam!token")
            user, token_name = px_id.split('!')
            
            proxmox = ProxmoxAPI(px_ip, user=user, token_name=token_name, token_value=px_secret, verify_ssl=False, timeout=6)
            my_node = proxmox.nodes.get()[0]['node']
            
            h_uid = f"{prefix}host_{my_node}"
            self.ctx.seen_uids.add(h_uid)
            if h_uid not in self.ctx.cfg:
                self.ctx.new_entities.append((h_uid, my_node, 'host', px_ip, 0, '', 'Local', '', ''))
            
            if self.ctx.cfg.get(h_uid, {}).get('enabled', 1) != 0:
                host_status = proxmox.nodes(my_node).status.get()
                host_cpu = (host_status.get('cpu') or 0.0) * 100
                host_mu = (host_status.get('memory', {}).get('used') or 0.0) / (1024**3)
                host_mt = (host_status.get('memory', {}).get('total') or 0.0) / (1024**3)
                host_du = host_status.get('rootfs', {}).get('used', 0) / (1024**3)
                host_dt = host_status.get('rootfs', {}).get('total', 1) / (1024**3)
                
                rx_mbps, tx_mbps = 0.0, 0.0
                try:
                    rrd_data = proxmox.nodes(my_node).rrddata.get(timeframe='hour')
                    if rrd_data:
                        for point in reversed(rrd_data):
                            if point.get('netin') is not None and point.get('netout') is not None:
                                rx_mbps = (point['netin'] * 8) / 1_000_000
                                tx_mbps = (point['netout'] * 8) / 1_000_000
                                break
                except Exception: pass
                
                self.ctx.sys_updates.append(('host_uptime', str(host_status.get('uptime', 0))))
                self.ctx.metrics_batch.append((h_uid, my_node, 'host', 'running', host_cpu, host_mu, host_mt, host_du, host_dt, rx_mbps, tx_mbps, 0.0, 0.0, 0.0))
            else:
                self.ctx.metrics_batch.append((h_uid, my_node, 'host', 'paused', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

            for storage in proxmox.nodes(my_node).storage.get():
                s_uid = f"{prefix}ds_{my_node}_{storage.get('storage')}"
                s_name = f"DS-{storage.get('storage')}"
                self.ctx.seen_uids.add(s_uid)
                if s_uid not in self.ctx.cfg: self.ctx.new_entities.append((s_uid, s_name, 'storage', '', 0, '', 'Local', h_uid, ''))
                if self.ctx.cfg.get(s_uid, {}).get('enabled', 1) != 0:
                    status = 'running' if storage.get('active') == 1 else 'stopped'
                    self.ctx.metrics_batch.append((s_uid, s_name, 'storage', status, 0.0, 0.0, 0.0, storage.get('used', 0)/1073741824, storage.get('total', 1)/1073741824, 0.0, 0.0, 0.0, 0.0, 0.0))
                else:
                    self.ctx.metrics_batch.append((s_uid, s_name, 'storage', 'paused', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

            for lxc in proxmox.nodes(my_node).lxc.get():
                l_uid = f"{prefix}lxc_{lxc.get('vmid')}"
                l_name = lxc.get('name')
                self.ctx.seen_uids.add(l_uid)
                if l_uid not in self.ctx.cfg: self.ctx.new_entities.append((l_uid, l_name, 'lxc', px_ip, 0, '', 'Local', h_uid, ''))
                if self.ctx.cfg.get(l_uid, {}).get('enabled', 1) != 0:
                    status = 'running' if lxc.get('status') == 'running' else 'stopped'
                    self.ctx.metrics_batch.append((l_uid, l_name, 'lxc', status, (lxc.get('cpu') or 0)*100, (lxc.get('mem') or 0)/1073741824, (lxc.get('maxmem') or 0)/1073741824, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
                else:
                    self.ctx.metrics_batch.append((l_uid, l_name, 'lxc', 'paused', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

            for vm in proxmox.nodes(my_node).qemu.get():
                v_uid = f"{prefix}vm_{vm.get('vmid')}"
                v_name = vm.get('name')
                self.ctx.seen_uids.add(v_uid)
                if v_uid not in self.ctx.cfg: self.ctx.new_entities.append((v_uid, v_name, 'vm', px_ip, 0, '', 'Local', h_uid, ''))
                if self.ctx.cfg.get(v_uid, {}).get('enabled', 1) != 0:
                    v_cpu, v_mu, v_mt = (vm.get('cpu') or 0)*100, (vm.get('mem') or 0)/1073741824, (vm.get('maxmem') or 0)/1073741824
                    if vm.get('status') == 'running':
                        try:
                            v_stat = proxmox.nodes(my_node).qemu(vm.get('vmid')).status.current.get()
                            v_cpu, v_mu, v_mt = (v_stat.get('cpu') or 0)*100, (v_stat.get('mem') or 0)/1073741824, (v_stat.get('maxmem') or 0)/1073741824
                        except: pass
                        self.ctx.metrics_batch.append((v_uid, v_name, 'vm', 'running', v_cpu, v_mu, v_mt, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
                    else:
                        self.ctx.metrics_batch.append((v_uid, v_name, 'vm', 'stopped', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
                else:
                    self.ctx.metrics_batch.append((v_uid, v_name, 'vm', 'paused', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

            if PLUGIN_ERROR_STATE.get('proxmox'):
                log_event("Proxmox Integration", "RESOLVED", "Connection restored.")
                PLUGIN_ERROR_STATE['proxmox'] = False

        except Exception as e:
            self.mark_offline(prefix)
            if not PLUGIN_ERROR_STATE.get('proxmox'):
                log_event("Proxmox Integration", "CRITICAL", f"Failed to reach Proxmox API: {str(e)}")
                PLUGIN_ERROR_STATE['proxmox'] = True

class PterodactylPlugin(BasePlugin):
    def fetch_node(self, uid, name, uuid_str, url, token):
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        for attempt in range(2):
            try:
                res = requests.get(f"{url}/api/client/servers/{uuid_str}/resources", headers=headers, timeout=5)
                if res.status_code == 200:
                    r = res.json()['attributes']
                    status = 'running' if r['current_state'] == 'running' else 'stopped'
                    
                    limit = PTERO_LIMITS_CACHE.get(uuid_str, 100)
                    if uuid_str not in PTERO_LIMITS_CACHE:
                        try:
                            det = requests.get(f"{url}/api/client/servers/{uuid_str}", headers=headers, timeout=5)
                            if det.status_code == 200:
                                limit = max(det.json()['attributes']['limits']['cpu'], 100)
                                PTERO_LIMITS_CACHE[uuid_str] = limit
                        except: pass

                    actual_cpu = (r['resources'].get('cpu_absolute', 0.0) / limit) * 100
                    mem_used = r['resources'].get('memory_bytes', 0.0) / (1024**3)
                    
                    mem_total = 0.0
                    try:
                        res_details = requests.get(f"{url}/api/client/servers/{uuid_str}", headers=headers, timeout=5)
                        if res_details.status_code == 200:
                            mem_total = (res_details.json()['attributes']['limits']['memory'] / 1024)
                    except: pass
                    
                    return (uid, name, 'game', status, actual_cpu, mem_used, mem_total, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
            except: time.sleep(0.5)
        return (uid, name, 'game', 'stopped', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    def run(self):
        pt_url = self.ctx.settings.get('plugin_pterodactyl_url')
        pt_token = decrypt_secret(self.ctx.settings.get('plugin_pterodactyl_token'), self.ctx.enc_key)
        if not (pt_url and pt_token): return

        prefix = "pt_"
        try:
            safe_url = ensure_url(pt_url)
            res = requests.get(f"{safe_url}/api/client", headers={"Authorization": f"Bearer {pt_token}", "Accept": "application/json"}, timeout=6)
            res.raise_for_status()
            
            for server in res.json().get('data', []):
                s_uuid, s_name = server['attributes']['uuid'], server['attributes']['name']
                uid = f"{prefix}{s_uuid}"
                self.ctx.seen_uids.add(uid)
                
                if uid not in self.ctx.cfg:
                    self.ctx.new_entities.append((uid, s_name, 'game', safe_url, 0, s_uuid, 'Local', '', ''))
                
                if self.ctx.cfg.get(uid, {}).get('enabled', 1) != 0:
                    self.ctx.futures.append(self.ctx.executor.submit(self.fetch_node, uid, s_name, s_uuid, safe_url, pt_token))
                else:
                    self.ctx.metrics_batch.append((uid, s_name, 'game', 'paused', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

            if PLUGIN_ERROR_STATE.get('pterodactyl'):
                log_event("Pterodactyl Integration", "RESOLVED", "Connection restored.")
                PLUGIN_ERROR_STATE['pterodactyl'] = False

        except Exception as e:
            self.mark_offline(prefix)
            if not PLUGIN_ERROR_STATE.get('pterodactyl'):
                log_event("Pterodactyl Integration", "CRITICAL", f"Failed to reach Pterodactyl Panel: {str(e)}")
                PLUGIN_ERROR_STATE['pterodactyl'] = True

class SynologyPlugin(BasePlugin):
    def run(self):
        ip_raw = self.ctx.settings.get('plugin_synology_ip')
        user = self.ctx.settings.get('plugin_synology_user')
        pwd = decrypt_secret(self.ctx.settings.get('plugin_synology_password'), self.ctx.enc_key)
        if not (ip_raw and user and pwd): return

        base_url = parse_nas_input(ip_raw, 5000)
        clean_ip = urllib.parse.urlparse(base_url).hostname or ip_raw
        uid = f"syn_{clean_ip}"
        name = "Synology DSM"
        
        self.ctx.seen_uids.add(uid)
        
        if uid not in self.ctx.cfg:
            self.ctx.new_entities.append((uid, name, 'storage', base_url, 0, '', 'Local', '', ''))

        if self.ctx.cfg.get(uid, {}).get('enabled', 1) != 0:
            try:
                encoded_user = urllib.parse.quote(user)
                encoded_pwd = urllib.parse.quote(pwd)
                auth_url = f"{base_url}/webapi/auth.cgi?api=SYNO.API.Auth&version=3&method=login&account={encoded_user}&passwd={encoded_pwd}"
                auth_res = requests.get(auth_url, timeout=5, verify=False).json()
                
                if auth_res.get('success'):
                    sid = auth_res['data']['sid']
                    cpu, mem_u, mem_t, disk_u, disk_t = 0.0, 0.0, 1.0, 0.0, 1.0
                    
                    try:
                        util_url = f"{base_url}/webapi/entry.cgi?api=SYNO.Core.System.Utilization&version=1&method=get&_sid={sid}"
                        util_res = requests.get(util_url, timeout=5, verify=False).json()
                        if util_res.get('success'):
                            cpu = float(util_res['data']['cpu']['system_load'] + util_res['data']['cpu']['user_load'])
                            mem_u = float(util_res['data']['memory']['real_usage']) / 100.0 # Stored as % of 1
                            mem_t = 1.0
                    except Exception: pass
                    
                    self.ctx.metrics_batch.append((uid, name, 'storage', 'running', cpu, mem_u, mem_t, disk_u, disk_t, 0.0, 0.0, 0.0, 0.0, 0.0))
                    
                    if PLUGIN_ERROR_STATE.get('synology'):
                        log_event("Synology Integration", "RESOLVED", "Connection restored.")
                        PLUGIN_ERROR_STATE['synology'] = False
                else:
                    self.ctx.metrics_batch.append((uid, name, 'storage', 'stopped', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
            except Exception as e:
                self.ctx.metrics_batch.append((uid, name, 'storage', 'stopped', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
                if not PLUGIN_ERROR_STATE.get('synology'):
                    log_event("Synology Integration", "CRITICAL", f"Failed to reach Synology DSM: {str(e)}")
                    PLUGIN_ERROR_STATE['synology'] = True
        else:
            self.ctx.metrics_batch.append((uid, name, 'storage', 'paused', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

class QNAPPlugin(BasePlugin):
    def run(self):
        ip_raw = self.ctx.settings.get('plugin_qnap_ip')
        user = self.ctx.settings.get('plugin_qnap_user')
        pwd = decrypt_secret(self.ctx.settings.get('plugin_qnap_password'), self.ctx.enc_key)
        if not (ip_raw and user and pwd): return

        base_url = parse_nas_input(ip_raw, 8080)
        clean_ip = urllib.parse.urlparse(base_url).hostname or ip_raw
        uid = f"qnap_{clean_ip}"
        name = "QNAP QTS"
        
        self.ctx.seen_uids.add(uid)
        
        if uid not in self.ctx.cfg:
            self.ctx.new_entities.append((uid, name, 'storage', base_url, 0, '', 'Local', '', ''))

        if self.ctx.cfg.get(uid, {}).get('enabled', 1) != 0:
            try:
                encoded_user = urllib.parse.quote(user)
                b64_pwd = base64.b64encode(pwd.encode()).decode()
                encoded_pwd = urllib.parse.quote(b64_pwd)
                
                session = requests.Session()
                success = False
                
                # Test multiple endpoints for QTS 4 / QTS 5 compatibility
                for test_path in ["authLogin.cgi", "filemanager/wfm2Login.cgi"]:
                    try:
                        auth_url = f"{base_url}/cgi-bin/{test_path}?user={encoded_user}&pwd={encoded_pwd}"
                        res = session.get(auth_url, timeout=5, verify=False)
                        if res.status_code == 200 and ('status="1"' in res.text or ('authPassed' in res.text and '<![CDATA[1]]' in res.text)):
                            success = True
                            break
                    except Exception: pass
                
                if success:
                    cpu, mem_u, mem_t, disk_u, disk_t = 0.0, 0.0, 1.0, 0.0, 1.0
                    try:
                        sys_res = session.get(f"{base_url}/cgi-bin/sysinfoReq.cgi", timeout=5, verify=False)
                        if sys_res.status_code == 200:
                            root = ET.fromstring(sys_res.text)
                            cpu_node = root.find('cpuUsage')
                            if cpu_node is not None:
                                cpu = float(cpu_node.text.replace('%', ''))
                    except Exception: pass
                    
                    self.ctx.metrics_batch.append((uid, name, 'storage', 'running', cpu, mem_u, mem_t, disk_u, disk_t, 0.0, 0.0, 0.0, 0.0, 0.0))
                    
                    if PLUGIN_ERROR_STATE.get('qnap'):
                        log_event("QNAP Integration", "RESOLVED", "Connection restored.")
                        PLUGIN_ERROR_STATE['qnap'] = False
                else:
                    self.ctx.metrics_batch.append((uid, name, 'storage', 'stopped', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
                    if not PLUGIN_ERROR_STATE.get('qnap'):
                        log_event("QNAP Integration", "CRITICAL", "Failed to reach QNAP QTS. Check credentials or port configs.")
                        PLUGIN_ERROR_STATE['qnap'] = True
            except Exception as e:
                self.ctx.metrics_batch.append((uid, name, 'storage', 'stopped', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
        else:
            self.ctx.metrics_batch.append((uid, name, 'storage', 'paused', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

class ManualMonitorsPlugin(BasePlugin):
    def check_ping(self, uid, name, ip):
        try:
            out = subprocess.run(['ping', '-c', '4', '-W', '1', ip], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            
            pl_match = re.search(r'([\d\.]+)%\s*packet loss', out.stdout)
            packet_loss = float(pl_match.group(1)) if pl_match else 100.0
            
            rtt_match = re.search(r'= [\d\.]+/([\d\.]+)/[\d\.]+/([\d\.]+)', out.stdout)
            ping_ms = float(rtt_match.group(1)) if rtt_match else 0.0
            jitter_ms = float(rtt_match.group(2)) if rtt_match else 0.0
            
            if out.returncode == 0 or packet_loss < 100.0:
                return (uid, name, 'network', 'running', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, ping_ms, packet_loss, jitter_ms)
        except: pass
        return (uid, name, 'network', 'stopped', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 100.0, 0.0)

    def check_tcp(self, uid, name, ip, port):
        for _ in range(2):
            try:
                with socket.create_connection((ip, port), timeout=3): 
                    return (uid, name, 'app', 'running', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
            except: time.sleep(0.5)
        return (uid, name, 'app', 'stopped', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    def check_http(self, uid, name, url):
        url = ensure_url(url)
        for _ in range(2):
            try:
                res = requests.get(url, timeout=5, verify=False)
                if res.status_code < 400:
                    return (uid, name, 'http', 'running', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, res.elapsed.total_seconds()*1000, 0.0, 0.0)
            except: time.sleep(0.5)
        return (uid, name, 'http', 'stopped', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 100.0, 0.0)

    def run(self):
        for uid, data in self.ctx.cfg.items():
            if uid.startswith('mnl_') and data['type'] in ['network', 'app', 'http']:
                self.ctx.seen_uids.add(uid)
                if data['enabled'] == 0:
                    self.ctx.metrics_batch.append((uid, data['name'], data['type'], 'paused', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
                    continue
                if data['type'] == 'network':
                    self.ctx.futures.append(self.ctx.executor.submit(self.check_ping, uid, data['name'], data['ip']))
                elif data['type'] == 'app':
                    self.ctx.futures.append(self.ctx.executor.submit(self.check_tcp, uid, data['name'], data['ip'], data['port']))
                elif data['type'] == 'http':
                    self.ctx.futures.append(self.ctx.executor.submit(self.check_http, uid, data['name'], data['ip']))

# ==========================================
# CORE LOOP ENGINE
# ==========================================

def collect_and_store():
    settings = get_settings()
    if settings.get('is_setup_complete') != '1': return
    global_mute = (settings.get('global_mute') == '1')
    enc_key = settings.get('sys_encryption_key')

    try:
        conn = sqlite3.connect(DB_NAME, timeout=15)
        cfg = {r[0]: {
            'name': r[1], 'type': r[2], 'ip': r[3], 'port': r[4], 'uuid': r[5], 'enabled': r[6], 
            'down_since': r[7], 'cooldown': r[8], 'cpu_thresh': r[9], 'mem_thresh': r[10], 'location': r[11], 
            'parent_uid': r[12], 'stack_group': r[13]} 
               for r in conn.execute("SELECT uid, name, entity_type, target_address, port, uuid, is_enabled, down_since, cooldown_interval, cpu_threshold, mem_threshold, location, parent_uid, stack_group FROM monitored_targets").fetchall()}
        conn.close()
    except Exception as e: 
        print(f"Database Read Error: {e}")
        return

    downtime_updates = []
    now = time.time()
    
    with ThreadPoolExecutor(max_workers=40) as executor:
        ctx = PluginContext(settings, cfg, executor, enc_key)

        plugins = [
            PiholePlugin(ctx),
            PlexPlugin(ctx),
            JellyfinPlugin(ctx),
            TrueNASPlugin(ctx),
            SynologyPlugin(ctx),
            QNAPPlugin(ctx),
            ESXiPlugin(ctx),
            ProxmoxPlugin(ctx),
            PortainerPlugin(ctx),
            PterodactylPlugin(ctx),
            ManualMonitorsPlugin(ctx)
        ]
        for p in plugins: p.run()

        for future in as_completed(ctx.futures, timeout=15):
            try:
                res = future.result()
                if res: ctx.metrics_batch.append(res)
            except Exception: pass
            
        for uid, data in cfg.items():
            if data['type'] in ['game', 'host', 'vm', 'lxc', 'storage', 'app'] and data['enabled'] != 0 and uid not in ctx.seen_uids:
                ctx.metrics_batch.append((uid, data['name'], data['type'], 'stopped', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

        current_status = {m[0]: m[3] for m in ctx.metrics_batch}
        
        parent_offline_map = {}
        for m in ctx.metrics_batch:
            if m[3] == 'stopped':
                parent_offline_map[m[0]] = []
                
        for uid, data in cfg.items():
            if data['parent_uid'] in parent_offline_map and current_status.get(uid) == 'stopped':
                parent_offline_map[data['parent_uid']].append(data['name'])

        alert_updates = []
        notification_queue = []
        event_logs = []

        for m in ctx.metrics_batch:
            uid, name, e_type, status = m[0], m[1], m[2], m[3]
            if status == 'paused': continue
            
            db_down = cfg.get(uid, {}).get('down_since', 0.0)
            cldn = cfg.get(uid, {}).get('cooldown', 3600)
            cpu_t = cfg.get(uid, {}).get('cpu_thresh', 90.0)
            mem_t = cfg.get(uid, {}).get('mem_thresh', 90.0)
            parent_uid = cfg.get(uid, {}).get('parent_uid')
            is_game = (e_type == 'game')
            
            if status == 'stopped':
                down_since = now if db_down == 0.0 else db_down
                if db_down == 0.0: downtime_updates.append((now, uid))
                
                is_suppressed = False
                alert_body = f"🚨 <b>SYSTEM OFFLINE</b> 🚨\n\n<b>Entity:</b> {name}\n<b>Type:</b> {e_type.upper()}"
                log_trigger = f"Service went offline."
                
                if parent_uid and current_status.get(parent_uid) == 'stopped':
                    is_suppressed = True
                else:
                    if uid in parent_offline_map and len(parent_offline_map[uid]) > 0:
                        affected = parent_offline_map[uid]
                        affected_str = ", ".join(affected[:5])
                        if len(affected) > 5:
                            affected_str += f" (+{len(affected)-5} more)"
                        alert_body += f"\n\n<b>Cascading Impact:</b>\n{len(affected)} dependent services are unreachable as a result.\n<i>└ {affected_str}</i>"
                        log_trigger += " (Cascading Impact Event)"
                    
                    if parent_uid and current_status.get(parent_uid) == 'running':
                        parent_name = cfg.get(parent_uid, {}).get('name', 'Parent Node')
                        alert_body += f"\n\n<b>Diagnostics:</b>\nThe underlying host node ({parent_name}) is currently ONLINE. This indicates an isolated crash of this specific application."
                        log_trigger += " (Isolated Fault)"

                if not is_suppressed:
                    recover_body = f"✅ <b>SYSTEM RECOVERED</b> ✅\n\n<b>Entity:</b> {name}\n<b>Status:</b> Back Online and responding."
                    process_alert(uid=uid, entity_name=name, metric='offline', condition_met=True, trigger_count=3, 
                                  alert_body=alert_body, recover_body=recover_body, log_trigger=log_trigger, log_recover="Service is back online.",
                                  down_since=down_since, is_game=is_game, global_mute=global_mute, cooldown_interval=cldn, 
                                  silent_resolve=False, settings=settings, alert_updates=alert_updates, notification_queue=notification_queue, event_logs=event_logs)
            else:
                if db_down != 0.0: downtime_updates.append((0.0, uid))
                process_alert(uid=uid, entity_name=name, metric='offline', condition_met=False, trigger_count=3, 
                              alert_body="", recover_body="", log_trigger="", log_recover="Service is back online.",
                              down_since=db_down, is_game=is_game, global_mute=global_mute, cooldown_interval=cldn, 
                              silent_resolve=False, settings=settings, alert_updates=alert_updates, notification_queue=notification_queue, event_logs=event_logs)
                
                if e_type == 'storage' and m[8] > 0:
                    disk_pct = (m[7] / m[8]) * 100
                    alert_body = f"⚠️ <b>CAPACITY WARNING</b> ⚠️\n\n<b>Entity:</b> {name}\n<b>Metric:</b> Storage Volume is at {disk_pct:.1f}%\n<b>Threshold:</b> {mem_t}%"
                    recover_body = f"✅ <b>CAPACITY NORMALIZED</b> ✅\n\n<b>Entity:</b> {name}\n<b>Status:</b> Storage volume usage has dropped below threshold."
                    process_alert(uid=uid, entity_name=name, metric='disk', condition_met=(disk_pct > mem_t), trigger_count=3, 
                                  alert_body=alert_body, recover_body=recover_body, log_trigger=f"Storage volume exceeded threshold ({disk_pct:.1f}%)", log_recover="Storage usage normalized.",
                                  down_since=0.0, is_game=False, global_mute=global_mute, cooldown_interval=cldn, 
                                  silent_resolve=False, settings=settings, alert_updates=alert_updates, notification_queue=notification_queue, event_logs=event_logs)
                elif e_type in ['vm', 'lxc', 'host', 'game']:
                    if m[4] > 0: 
                        alert_body = f"⚠️ <b>PERFORMANCE WARNING</b> ⚠️\n\n<b>Entity:</b> {name}\n<b>Metric:</b> CPU Usage is spiking at {m[4]:.1f}%\n<b>Threshold:</b> {cpu_t}%"
                        recover_body = f"✅ <b>PERFORMANCE NORMALIZED</b> ✅\n\n<b>Entity:</b> {name}\n<b>Status:</b> CPU Usage has stabilized."
                        process_alert(uid=uid, entity_name=name, metric='cpu', condition_met=(m[4] > cpu_t), trigger_count=3, 
                                      alert_body=alert_body, recover_body=recover_body, log_trigger=f"CPU exceeded threshold ({m[4]:.1f}%)", log_recover="CPU usage stabilized.",
                                      down_since=0.0, is_game=False, global_mute=global_mute, cooldown_interval=cldn, 
                                      silent_resolve=False, settings=settings, alert_updates=alert_updates, notification_queue=notification_queue, event_logs=event_logs)
                    if m[6] > 0: 
                        ram_pct = (m[5]/m[6])*100
                        alert_body = f"⚠️ <b>MEMORY WARNING</b> ⚠️\n\n<b>Entity:</b> {name}\n<b>Metric:</b> RAM Usage is high at {ram_pct:.1f}%\n<b>Threshold:</b> {mem_t}%"
                        recover_body = f"✅ <b>MEMORY NORMALIZED</b> ✅\n\n<b>Entity:</b> {name}\n<b>Status:</b> RAM Usage has dropped below threshold."
                        process_alert(uid=uid, entity_name=name, metric='ram', condition_met=(ram_pct > mem_t), trigger_count=3, 
                                      alert_body=alert_body, recover_body=recover_body, log_trigger=f"RAM exceeded threshold ({ram_pct:.1f}%)", log_recover="RAM usage stabilized.",
                                      down_since=0.0, is_game=False, global_mute=global_mute, cooldown_interval=cldn, 
                                      silent_resolve=False, settings=settings, alert_updates=alert_updates, notification_queue=notification_queue, event_logs=event_logs)

        # 5. Database Writing Phase (Batched to prevent locks)
        try:
            w_conn = sqlite3.connect(DB_NAME, timeout=15)
            for uid, name, e_type, target, port, uuid_str, loc, parent_uid, stack_group in ctx.new_entities:
                w_conn.execute('INSERT OR IGNORE INTO monitored_targets (uid, name, entity_type, target_address, port, uuid, is_enabled, down_since, cooldown_interval, cpu_threshold, mem_threshold, location, parent_uid, stack_group) VALUES (?, ?, ?, ?, ?, ?, 1, 0.0, 3600, 90.0, 90.0, ?, ?, ?)', (uid, name, e_type, target, port, uuid_str, loc, parent_uid, stack_group))
            for stamp, uid in downtime_updates:
                w_conn.execute('UPDATE monitored_targets SET down_since = ? WHERE uid = ?', (stamp, uid))
            
            w_conn.executemany('INSERT INTO metrics (uid, entity_name, entity_type, status, cpu_usage, mem_used, mem_total, disk_used, disk_total, net_in, net_out, ping_ms, packet_loss, jitter_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', ctx.metrics_batch)
            w_conn.executemany('INSERT OR REPLACE INTO latest_metrics (uid, entity_name, entity_type, status, cpu_usage, mem_used, mem_total, disk_used, disk_total, net_in, net_out, ping_ms, packet_loss, jitter_ms, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)', ctx.metrics_batch)
            
            if alert_updates:
                w_conn.executemany('''INSERT INTO alert_state (uid, is_alerting, first_alert_time, last_alert_time, count) VALUES (?, ?, ?, ?, ?) ON CONFLICT(uid) DO UPDATE SET is_alerting=excluded.is_alerting, first_alert_time=excluded.first_alert_time, last_alert_time=excluded.last_alert_time, count=excluded.count''', alert_updates)
                
            if event_logs:
                w_conn.executemany('INSERT INTO event_logs (entity_name, event_type, message) VALUES (?, ?, ?)', event_logs)
                
            if ctx.sys_updates: 
                w_conn.executemany("INSERT OR REPLACE INTO sys_settings (key, value) VALUES (?, ?)", ctx.sys_updates)
            
            w_conn.commit()
            w_conn.close()
        except Exception: pass

    # 6. Push Notifications out of DB lock
    for msg in notification_queue:
        if not send_notification_sync(msg, settings):
            try:
                conn = sqlite3.connect(DB_NAME, timeout=15)
                conn.execute('INSERT INTO notification_queue (message) VALUES (?)', (msg,))
                conn.commit()
                conn.close()
            except Exception: pass
            
    # Flush any older notifications if the network is back up
    flush_notification_queue(settings)

if __name__ == "__main__":
    loop_count = 0
    try:
        while True:
            start_time = time.time()
            collect_and_store()
            loop_count += 1
            
            now_dt = datetime.datetime.now()
            report_time_key = f"{now_dt.year}-{now_dt.month}-{now_dt.day}-{now_dt.hour}"
            
            # Scheduled 08:00 AM and 20:00 PM Reports
            if now_dt.hour in [8, 20] and now_dt.minute == 0 and LAST_DAILY_REPORT_TIME != report_time_key:
                report_title = "🌅 <b>HackNet Morning Status Report</b>" if now_dt.hour == 8 else "🌙 <b>HackNet Evening Status Report</b>"
                generate_full_report(report_title)
                LAST_DAILY_REPORT_TIME = report_time_key

            # Prune old logs and db vacuum every hour (360 loops of 10s)
            if loop_count >= 360:
                try:
                    conn = sqlite3.connect(DB_NAME, timeout=15)
                    conn.execute("DELETE FROM metrics WHERE timestamp <= datetime('now', '-7 days')")
                    conn.execute("DELETE FROM event_logs WHERE timestamp <= datetime('now', '-30 days')")
                    conn.execute("DELETE FROM notification_queue WHERE added_at <= datetime('now', '-3 days')")
                    conn.commit()
                    conn.execute("PRAGMA incremental_vacuum(16);")
                    conn.close()
                except Exception: pass
                loop_count = 0

            sleep_duration = max(0, POLL_INTERVAL - (time.time() - start_time))
            end_sleep = time.time() + sleep_duration
            
            # Sub-loop to listen for UI Triggers (Force Poll / Force Report)
            while time.time() < end_sleep:
                time.sleep(0.5)
                try:
                    conn = sqlite3.connect(DB_NAME, timeout=15)
                    
                    # 1. Force Poll Interrupt
                    fp = conn.execute("SELECT value FROM sys_settings WHERE key='force_poll'").fetchone()
                    if fp and fp[0] == '1':
                        conn.execute("UPDATE sys_settings SET value='0' WHERE key='force_poll'")
                        conn.commit()
                        conn.close()
                        break 
                        
                    # 2. On-Demand Report Dispatch
                    tr = conn.execute("SELECT value FROM sys_settings WHERE key='trigger_report'").fetchone()
                    if tr and tr[0] == '1':
                        conn.execute("UPDATE sys_settings SET value='0' WHERE key='trigger_report'")
                        conn.commit()
                        generate_full_report("📊 <b>HackNet On-Demand Status Report</b>")
                        
                    conn.close()
                except Exception: pass

    except KeyboardInterrupt: pass

    


    
    
    


    
    

    

    

    







    

    





    


    

