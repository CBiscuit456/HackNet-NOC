import time
import sqlite3
import subprocess
import socket
import requests
import os
import datetime
import urllib3
import re
import json
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from proxmoxer import ProxmoxAPI
from dotenv import load_dotenv

# Suppress insecure HTTPS warnings for local self-signed homelab APIs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

load_dotenv()
DB_NAME = os.getenv('DB_NAME', 'data/metrics.db')

# In-memory state tracking to prevent alert/log spam
ALERT_STATE = {}
PLUGIN_ERROR_STATE = {}
PTERO_LIMITS_CACHE = {}
LAST_DAILY_REPORT_DAY = None

GAME_ESCALATION_TIME = 86400
GAME_ESCALATION_COOLDOWN = 86400
POLL_INTERVAL = 10

# ==========================================
# HELPER FUNCTIONS
# ==========================================

def format_time_delta(seconds):
    days, seconds = divmod(int(seconds), 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days > 0: parts.append(f"{days}d")
    if hours > 0: parts.append(f"{hours}h")
    if minutes > 0: parts.append(f"{minutes}m")
    return " ".join(parts) if parts else "0m"

def log_event(entity_name, event_type, message):
    try:
        conn = sqlite3.connect(DB_NAME, timeout=15)
        conn.execute('INSERT INTO event_logs (entity_name, event_type, message) VALUES (?, ?, ?)', (entity_name, event_type, message))
        conn.commit()
        conn.close()
    except Exception:
        pass

def send_notification(message, settings):
    tg_token = settings.get('notify_telegram_token')
    tg_chat = settings.get('notify_telegram_chat_id')
    discord_url = settings.get('notify_discord_webhook_url')
    
    # Telegram
    if tg_token and tg_chat:
        try: 
            requests.post(f"https://api.telegram.org/bot{tg_token}/sendMessage", 
                          json={"chat_id": tg_chat, "text": message, "parse_mode": "HTML"}, timeout=5)
        except Exception: pass
    
    # Discord Webhook
    if discord_url:
        try:
            discord_msg = message.replace('<b>', '**').replace('</b>', '**').replace('<br>', '\n')
            requests.post(discord_url, json={"content": discord_msg}, timeout=5)
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

def generate_full_report(title="📊 <b>NOC Status Report</b>"):
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
        return f"🟢 {tot - len(down)}/{tot} UP" if not down else f"🔴 {tot - len(down)}/{tot} UP [{', '.join(down)}]"
        
    msg = f"{title}\n\n🌐 <b>NETWORK:</b> {'🟢 ONLINE' if not net_down else '🔴 FAIL ['+', '.join(net_down)+']'} (WAN: {net_wan})\n🖥️ <b>HOST:</b> {host_str}\n💾 <b>STORAGE:</b> {fmt(storage_tot, storage_down)}\n📦 <b>VMs:</b> {fmt(vms_tot, vm_down)}\n📦 <b>LXCs:</b> {fmt(lxc_tot, lxc_down)}\n🔌 <b>TCP APPS:</b> {fmt(app_tot, app_down)}\n🌍 <b>WEB:</b> {fmt(http_tot, http_down)}\n🎮 <b>GAMES:</b> {fmt(game_tot, game_down)}"
    send_notification(msg, settings)

def sync_alert_db(key, state):
    try:
        conn = sqlite3.connect(DB_NAME, timeout=15)
        conn.execute('''INSERT INTO alert_state (uid, is_alerting, first_alert_time, last_alert_time, count) VALUES (?, ?, ?, ?, ?) ON CONFLICT(uid) DO UPDATE SET is_alerting=excluded.is_alerting, first_alert_time=excluded.first_alert_time, last_alert_time=excluded.last_alert_time, count=excluded.count''', (key, int(state['is_alerting']), state['first_alert_time'], state['last_alert_time'], state['count']))
        conn.commit()
        conn.close()
    except Exception: pass

def process_alert(uid, entity_name, metric, condition_met, trigger_count, msg_trigger, msg_recover, down_since, is_game, global_mute, cooldown_interval, silent_resolve, settings):
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
                if not global_mute: send_notification(f"🚨 <b>ALERT: {entity_name}</b>\n{msg_trigger}", settings)
                log_event(entity_name, 'CRITICAL', msg_trigger)
                state['is_alerting'] = True
                state['first_alert_time'] = down_since if down_since > 0 else now
                state['last_alert_time'] = now
                state_changed = True
            else:
                time_active = now - state['first_alert_time']
                cooldown = GAME_ESCALATION_COOLDOWN if (is_game and time_active >= GAME_ESCALATION_TIME) else cooldown_interval
                if (now - state['last_alert_time']) > cooldown:
                    duration_str = format_time_delta(time_active)
                    duration_text = f"Down for {duration_str}" if metric == 'offline' else f"Active for {duration_str}"
                    if not global_mute: send_notification(f"🚨 <b>REMINDER: {entity_name}</b>\n{msg_trigger} ({duration_text})", settings)
                    state['last_alert_time'] = now
                    state_changed = True
    else:
        if state['is_alerting']:
            duration_str = format_time_delta(now - state['first_alert_time'])
            duration_text = f"Was down for {duration_str}" if metric == 'offline' else f"Condition lasted {duration_str}"
            
            if not silent_resolve:
                if not global_mute: send_notification(f"🟢 <b>RESOLVED: {entity_name}</b>\n{msg_recover} ({duration_text})", settings)
                log_event(entity_name, 'RESOLVED', f"{msg_recover} (Duration: {duration_str})")
                
            state['is_alerting'] = False
            state['first_alert_time'] = 0
            state['count'] = 0
            state_changed = True
        elif state['count'] > 0:
            state['count'] = 0
            state_changed = True
            
    if state_changed: sync_alert_db(key, state)


# ==========================================
# OBJECT-ORIENTED PLUGIN ARCHITECTURE
# ==========================================

class PluginContext:
    """ Shared state passed down to every active plugin during a polling cycle. """
    def __init__(self, settings, cfg, executor):
        self.settings = settings
        self.cfg = cfg
        self.executor = executor
        self.seen_uids = set()
        self.metrics_batch = []
        self.new_entities = []
        self.sys_updates = []
        
        # --- FIX: These were missing, causing the fatal crash in Pterodactyl and Manual Monitors ---
        self.futures = []
        self.name_updates = []

class BasePlugin:
    def __init__(self, ctx: PluginContext):
        self.ctx = ctx
        
    def run(self):
        pass

    def mark_offline(self, prefix: str):
        """ Graceful Degradation: If a plugin crashes, force its known infrastructure to 'stopped' """
        for uid, data in self.ctx.cfg.items():
            if uid.startswith(prefix) and data['enabled'] != 0:
                self.ctx.metrics_batch.append((uid, data['name'], data['type'], 'stopped', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

class PiholePlugin(BasePlugin):
    def run(self):
        ip = self.ctx.settings.get('plugin_pihole_ip')
        pwd = self.ctx.settings.get('plugin_pihole_app_password')
        if not ip: return
        
        try:
            auth = ("", pwd) if pwd else None
            res = requests.get(f"http://{ip}/api/stats/summary", auth=auth, timeout=5, verify=False)
            res.raise_for_status()
            
            d = res.json()
            conn = sqlite3.connect(DB_NAME, timeout=15)
            conn.execute('DELETE FROM pihole_stats')
            conn.execute('INSERT INTO pihole_stats (id, status, domains_blocked, queries_today, ads_blocked, ads_percentage) VALUES (1, ?, ?, ?, ?, ?)', 
                         (d.get('state', 'unknown'), 
                          d.get('gravity', {}).get('domains_being_blocked', d.get('domains_being_blocked', 0)), 
                          d.get('queries', {}).get('total', d.get('dns_queries_today', 0)), 
                          d.get('queries', {}).get('blocked', d.get('ads_blocked_today', 0)), 
                          d.get('queries', {}).get('percent_blocked', d.get('ads_percentage_today', 0.0))))
            conn.commit()
            conn.close()
            
            if PLUGIN_ERROR_STATE.get('pihole'):
                log_event("Pi-Hole Integration", "RESOLVED", "Connection to Pi-Hole restored.")
                PLUGIN_ERROR_STATE['pihole'] = False
                
        except Exception as e:
            if not PLUGIN_ERROR_STATE.get('pihole'):
                log_event("Pi-Hole Integration", "CRITICAL", f"Failed to reach Pi-Hole API: {str(e)}")
                PLUGIN_ERROR_STATE['pihole'] = True

class PlexPlugin(BasePlugin):
    def run(self):
        url = self.ctx.settings.get('plugin_plex_url')
        token = self.ctx.settings.get('plugin_plex_token')
        if not (url and token): return

        uid = "plex_server_main"
        name = "Plex Media Server"
        self.ctx.seen_uids.add(uid)

        if uid not in self.ctx.cfg:
            self.ctx.new_entities.append((uid, name, 'app', url, 0, '', 'Local'))

        if self.ctx.cfg.get(uid, {}).get('enabled', 1) != 0:
            try:
                res = requests.get(f"{url}/status/sessions?X-Plex-Token={token}", headers={"Accept": "application/json"}, timeout=5, verify=False)
                res.raise_for_status()
                
                self.ctx.metrics_batch.append((uid, name, 'app', 'running', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

                if PLUGIN_ERROR_STATE.get('plex'):
                    log_event("Plex Integration", "RESOLVED", "Connection restored.")
                    PLUGIN_ERROR_STATE['plex'] = False
            except Exception as e:
                self.ctx.metrics_batch.append((uid, name, 'app', 'stopped', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
                if not PLUGIN_ERROR_STATE.get('plex'):
                    log_event("Plex Integration", "CRITICAL", f"Failed to reach Plex API: {str(e)}")
                    PLUGIN_ERROR_STATE['plex'] = True
        else:
            self.ctx.metrics_batch.append((uid, name, 'app', 'paused', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

class TrueNASPlugin(BasePlugin):
    def run(self):
        ip = self.ctx.settings.get('plugin_truenas_ip')
        token = self.ctx.settings.get('plugin_truenas_token')
        if not (ip and token): return

        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        base_url = f"http://{ip}/api/v2.0"
        prefix = "truenas_"

        try:
            res_info = requests.get(f"{base_url}/system/info", headers=headers, timeout=6, verify=False)
            res_info.raise_for_status()
            sys_info = res_info.json()
            hostname = sys_info.get('hostname', 'TrueNAS')

            h_uid = f"{prefix}host"
            self.ctx.seen_uids.add(h_uid)
            if h_uid not in self.ctx.cfg:
                self.ctx.new_entities.append((h_uid, hostname, 'host', ip, 0, '', 'Local'))
            
            if self.ctx.cfg.get(h_uid, {}).get('enabled', 1) != 0:
                self.ctx.metrics_batch.append((h_uid, hostname, 'host', 'running', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
            else:
                self.ctx.metrics_batch.append((h_uid, hostname, 'host', 'paused', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

            res_pools = requests.get(f"{base_url}/pool", headers=headers, timeout=6, verify=False)
            if res_pools.status_code == 200:
                for pool in res_pools.json():
                    p_name = pool.get('name', 'Unknown_Pool')
                    p_uid = f"{prefix}pool_{p_name}"
                    self.ctx.seen_uids.add(p_uid)

                    if p_uid not in self.ctx.cfg:
                        self.ctx.new_entities.append((p_uid, f"Pool-{p_name}", 'storage', '', 0, '', 'Local'))

                    if self.ctx.cfg.get(p_uid, {}).get('enabled', 1) != 0:
                        status = 'running' if pool.get('status') == 'ONLINE' else 'stopped'
                        self.ctx.metrics_batch.append((p_uid, f"Pool-{p_name}", 'storage', status, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
                    else:
                        self.ctx.metrics_batch.append((p_uid, f"Pool-{p_name}", 'storage', 'paused', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

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
        pwd = self.ctx.settings.get('plugin_esxi_password')
        if not (ip and user and pwd): return

        base_url = f"https://{ip}/rest"
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
                self.ctx.new_entities.append((h_uid, "ESXi Host", 'host', ip, 0, '', 'Local'))
            
            if self.ctx.cfg.get(h_uid, {}).get('enabled', 1) != 0:
                self.ctx.metrics_batch.append((h_uid, "ESXi Host", 'host', 'running', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
            else:
                self.ctx.metrics_batch.append((h_uid, "ESXi Host", 'host', 'paused', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

            res_vms = requests.get(f"{base_url}/vcenter/vm", headers=headers, timeout=6, verify=False)
            if res_vms.status_code == 200:
                for vm in res_vms.json().get('value', []):
                    vm_id, vm_name = vm.get('vm'), vm.get('name')
                    v_uid = f"{prefix}vm_{vm_id}"
                    self.ctx.seen_uids.add(v_uid)

                    if v_uid not in self.ctx.cfg:
                        self.ctx.new_entities.append((v_uid, vm_name, 'vm', '', 0, '', 'Local'))

                    if self.ctx.cfg.get(v_uid, {}).get('enabled', 1) != 0:
                        status = 'running' if vm.get('power_state') == 'POWERED_ON' else 'stopped'
                        self.ctx.metrics_batch.append((v_uid, vm_name, 'vm', status, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
                    else:
                        self.ctx.metrics_batch.append((v_uid, vm_name, 'vm', 'paused', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

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
        px_secret = self.ctx.settings.get('plugin_proxmox_token_secret')
        if not (px_ip and px_id and px_secret): return
        
        prefix = "px_"
        try:
            if '!' not in px_id: raise ValueError("Invalid Token ID. Expected user@pam!token")
            user, token_name = px_id.split('!')
            
            proxmox = ProxmoxAPI(px_ip, user=user, token_name=token_name, token_value=px_secret, verify_ssl=False, timeout=6)
            my_node = proxmox.nodes.get()[0]['node']
            
            # 1. Host Node
            h_uid = f"{prefix}host_{my_node}"
            self.ctx.seen_uids.add(h_uid)
            if h_uid not in self.ctx.cfg:
                self.ctx.new_entities.append((h_uid, my_node, 'host', px_ip, 0, '', 'Local'))
            
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
                self.ctx.metrics_batch.append((h_uid, my_node, 'host', 'running', host_cpu, host_mu, host_mt, host_du, host_dt, rx_mbps, tx_mbps, 0.0))
            else:
                self.ctx.metrics_batch.append((h_uid, my_node, 'host', 'paused', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

            # 2. Storage Arrays
            for storage in proxmox.nodes(my_node).storage.get():
                s_uid = f"{prefix}ds_{my_node}_{storage.get('storage')}"
                s_name = f"DS-{storage.get('storage')}"
                self.ctx.seen_uids.add(s_uid)
                if s_uid not in self.ctx.cfg: self.ctx.new_entities.append((s_uid, s_name, 'storage', '', 0, '', 'Local'))
                if self.ctx.cfg.get(s_uid, {}).get('enabled', 1) != 0:
                    status = 'running' if storage.get('active') == 1 else 'stopped'
                    self.ctx.metrics_batch.append((s_uid, s_name, 'storage', status, 0.0, 0.0, 0.0, storage.get('used', 0)/1073741824, storage.get('total', 1)/1073741824, 0.0, 0.0, 0.0))
                else:
                    self.ctx.metrics_batch.append((s_uid, s_name, 'storage', 'paused', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

            # 3. LXCs
            for lxc in proxmox.nodes(my_node).lxc.get():
                l_uid = f"{prefix}lxc_{lxc.get('vmid')}"
                l_name = lxc.get('name')
                self.ctx.seen_uids.add(l_uid)
                if l_uid not in self.ctx.cfg: self.ctx.new_entities.append((l_uid, l_name, 'lxc', px_ip, 0, '', 'Local'))
                if self.ctx.cfg.get(l_uid, {}).get('enabled', 1) != 0:
                    status = 'running' if lxc.get('status') == 'running' else 'stopped'
                    self.ctx.metrics_batch.append((l_uid, l_name, 'lxc', status, (lxc.get('cpu') or 0)*100, (lxc.get('mem') or 0)/1073741824, (lxc.get('maxmem') or 0)/1073741824, 0.0, 0.0, 0.0, 0.0, 0.0))
                else:
                    self.ctx.metrics_batch.append((l_uid, l_name, 'lxc', 'paused', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

            # 4. VMs
            for vm in proxmox.nodes(my_node).qemu.get():
                v_uid = f"{prefix}vm_{vm.get('vmid')}"
                v_name = vm.get('name')
                self.ctx.seen_uids.add(v_uid)
                if v_uid not in self.ctx.cfg: self.ctx.new_entities.append((v_uid, v_name, 'vm', px_ip, 0, '', 'Local'))
                if self.ctx.cfg.get(v_uid, {}).get('enabled', 1) != 0:
                    v_cpu, v_mu, v_mt = (vm.get('cpu') or 0)*100, (vm.get('mem') or 0)/1073741824, (vm.get('maxmem') or 0)/1073741824
                    if vm.get('status') == 'running':
                        try:
                            v_stat = proxmox.nodes(my_node).qemu(vm.get('vmid')).status.current.get()
                            v_cpu, v_mu, v_mt = (v_stat.get('cpu') or 0)*100, (v_stat.get('mem') or 0)/1073741824, (v_stat.get('maxmem') or 0)/1073741824
                        except: pass
                        self.ctx.metrics_batch.append((v_uid, v_name, 'vm', 'running', v_cpu, v_mu, v_mt, 0.0, 0.0, 0.0, 0.0, 0.0))
                    else:
                        self.ctx.metrics_batch.append((v_uid, v_name, 'vm', 'stopped', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
                else:
                    self.ctx.metrics_batch.append((v_uid, v_name, 'vm', 'paused', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

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
                    
                    return (uid, name, 'game', status, actual_cpu, mem_used, mem_total, 0.0, 0.0, 0.0, 0.0, 0.0)
            except: time.sleep(0.5)
        return (uid, name, 'game', 'stopped', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    def run(self):
        pt_url = self.ctx.settings.get('plugin_pterodactyl_url')
        pt_token = self.ctx.settings.get('plugin_pterodactyl_token')
        if not (pt_url and pt_token): return

        prefix = "pt_"
        try:
            res = requests.get(f"{pt_url}/api/client", headers={"Authorization": f"Bearer {pt_token}", "Accept": "application/json"}, timeout=6)
            res.raise_for_status()
            
            for server in res.json().get('data', []):
                s_uuid, s_name = server['attributes']['uuid'], server['attributes']['name']
                uid = f"{prefix}{s_uuid}"
                self.ctx.seen_uids.add(uid)
                
                if uid not in self.ctx.cfg:
                    self.ctx.new_entities.append((uid, s_name, 'game', pt_url, 0, s_uuid, 'Local'))
                
                if self.ctx.cfg.get(uid, {}).get('enabled', 1) != 0:
                    self.ctx.futures.append(self.ctx.executor.submit(self.fetch_node, uid, s_name, s_uuid, pt_url, pt_token))
                else:
                    self.ctx.metrics_batch.append((uid, s_name, 'game', 'paused', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

            if PLUGIN_ERROR_STATE.get('pterodactyl'):
                log_event("Pterodactyl Integration", "RESOLVED", "Connection restored.")
                PLUGIN_ERROR_STATE['pterodactyl'] = False

        except Exception as e:
            self.mark_offline(prefix)
            if not PLUGIN_ERROR_STATE.get('pterodactyl'):
                log_event("Pterodactyl Integration", "CRITICAL", f"Failed to reach Pterodactyl Panel: {str(e)}")
                PLUGIN_ERROR_STATE['pterodactyl'] = True

class ManualMonitorsPlugin(BasePlugin):
    def check_ping(self, uid, name, ip):
        for _ in range(2):
            try:
                out = subprocess.run(['ping', '-c', '1', '-W', '1', ip], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if out.returncode == 0:
                    match = re.search(r'time=([\d\.]+)', out.stdout)
                    return (uid, name, 'network', 'running', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, float(match.group(1)) if match else 0.0)
            except: pass
            time.sleep(0.5)
        return (uid, name, 'network', 'stopped', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    def check_tcp(self, uid, name, ip, port):
        for _ in range(2):
            try:
                with socket.create_connection((ip, port), timeout=3): 
                    return (uid, name, 'app', 'running', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
            except: time.sleep(0.5)
        return (uid, name, 'app', 'stopped', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    def check_http(self, uid, name, url):
        if not url.startswith('http'): url = 'http://' + url
        for _ in range(2):
            try:
                res = requests.get(url, timeout=5, verify=False)
                if res.status_code < 400:
                    return (uid, name, 'http', 'running', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, res.elapsed.total_seconds()*1000)
            except: time.sleep(0.5)
        return (uid, name, 'http', 'stopped', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    def run(self):
        for uid, data in self.ctx.cfg.items():
            if data['type'] in ['network', 'app', 'http']:
                if data['enabled'] == 0:
                    self.ctx.metrics_batch.append((uid, data['name'], data['type'], 'paused', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
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

    try:
        conn = sqlite3.connect(DB_NAME, timeout=15)
        cfg = {r[0]: {'name': r[1], 'type': r[2], 'ip': r[3], 'port': r[4], 'uuid': r[5], 'enabled': r[6], 'down_since': r[7], 'cooldown': r[8], 'cpu_thresh': r[9], 'mem_thresh': r[10], 'location': r[11]} 
               for r in conn.execute("SELECT uid, name, entity_type, target_address, port, uuid, is_enabled, down_since, cooldown_interval, cpu_threshold, mem_threshold, location FROM monitored_targets").fetchall()}
        conn.close()
    except Exception as e: 
        print(f"Database Read Error: {e}")
        return

    downtime_updates, alerts = [], []
    now = time.time()
    
    with ThreadPoolExecutor(max_workers=40) as executor:
        ctx = PluginContext(settings, cfg, executor)

        # 1. Execute Plugin Registry
        plugins = [
            PiholePlugin(ctx),
            PlexPlugin(ctx),
            TrueNASPlugin(ctx),
            ESXiPlugin(ctx),
            ProxmoxPlugin(ctx),
            PterodactylPlugin(ctx),
            ManualMonitorsPlugin(ctx)
        ]
        for p in plugins: p.run()

        # 2. Resolve Async Manual Monitors & Game Node checks
        for future in as_completed(ctx.futures, timeout=15):
            try:
                res = future.result()
                if res: ctx.metrics_batch.append(res)
            except Exception: pass
            
        # 3. Automatic Ghost Checks 
        for uid, data in cfg.items():
            if data['type'] in ['game', 'host', 'vm', 'lxc', 'storage'] and data['enabled'] != 0 and uid not in ctx.seen_uids:
                ctx.metrics_batch.append((uid, data['name'], data['type'], 'stopped', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

        # 4. Alert Compilation Engine
        for m in ctx.metrics_batch:
            uid, name, e_type, status = m[0], m[1], m[2], m[3]
            if status == 'paused': continue
            
            db_down = cfg.get(uid, {}).get('down_since', 0.0)
            cldn = cfg.get(uid, {}).get('cooldown', 3600)
            cpu_t = cfg.get(uid, {}).get('cpu_thresh', 90.0)
            mem_t = cfg.get(uid, {}).get('mem_thresh', 90.0)
            is_game = (e_type == 'game')
            
            if status == 'stopped':
                down_since = now if db_down == 0.0 else db_down
                if db_down == 0.0: downtime_updates.append((now, uid))
                
                if e_type == 'storage':
                    alerts.append((uid, name, 'offline', True, 3, f"Storage {name} is unreachable.", f"Storage {name} is back online.", down_since, False, cldn, False))
                else:
                    alerts.append((uid, name, 'offline', True, 3, f"{name} is offline.", f"{name} is back online.", down_since, is_game, cldn, False))
            else:
                if db_down != 0.0: downtime_updates.append((0.0, uid))
                alerts.append((uid, name, 'offline', False, 3, "", "", db_down, is_game, cldn, False))
                
                # Capacity Alerts
                if e_type == 'storage' and m[8] > 0:
                    disk_pct = (m[7] / m[8]) * 100
                    alerts.append((uid, name, 'disk', disk_pct > mem_t, 3, f"Storage {name} capacity critical at {disk_pct:.1f}%", f"Storage {name} capacity normalized.", 0.0, False, cldn, False))
                elif e_type in ['vm', 'lxc', 'host', 'game']:
                    if m[4] > 0: alerts.append((uid, name, 'cpu', m[4] > cpu_t, 3, f"{name} CPU critical at {m[4]:.1f}%", f"{name} CPU normalized.", 0.0, False, cldn, False))
                    if m[6] > 0: alerts.append((uid, name, 'ram', (m[5]/m[6])*100 > mem_t, 3, f"{name} RAM critical at {(m[5]/m[6])*100:.1f}%", f"{name} RAM normalized.", 0.0, False, cldn, False))

        # 5. Database Writing Phase
        try:
            w_conn = sqlite3.connect(DB_NAME, timeout=15)
            for uid, name, e_type, target, port, uuid_str, loc in ctx.new_entities:
                w_conn.execute('INSERT OR IGNORE INTO monitored_targets (uid, name, entity_type, target_address, port, uuid, is_enabled, down_since, cooldown_interval, cpu_threshold, mem_threshold, location) VALUES (?, ?, ?, ?, ?, ?, 1, 0.0, 3600, 90.0, 90.0, ?)', (uid, name, e_type, target, port, uuid_str, loc))
            for stamp, uid in downtime_updates:
                w_conn.execute('UPDATE monitored_targets SET down_since = ? WHERE uid = ?', (stamp, uid))
            
            # The metrics tuple requires exactly 12 fields to match the DB schema correctly.
            w_conn.executemany('INSERT INTO metrics (uid, entity_name, entity_type, status, cpu_usage, mem_used, mem_total, disk_used, disk_total, net_in, net_out, ping_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', ctx.metrics_batch)
            w_conn.executemany('INSERT OR REPLACE INTO latest_metrics (uid, entity_name, entity_type, status, cpu_usage, mem_used, mem_total, disk_used, disk_total, net_in, net_out, ping_ms, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)', ctx.metrics_batch)
            
            if ctx.sys_updates: 
                w_conn.executemany("INSERT OR REPLACE INTO sys_settings (key, value) VALUES (?, ?)", ctx.sys_updates)
            
            w_conn.commit()
            w_conn.close()
        except Exception as e: 
            print(f"Database Write Error: {e}")

    # 6. Push Notifications out of main DB lock
    for a in alerts: process_alert(a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7], a[8], global_mute, a[9], a[10], settings)

if __name__ == "__main__":
    loop_count = 0
    try:
        while True:
            start_time = time.time()
            collect_and_store()
            loop_count += 1
            
            now_dt = datetime.datetime.now()
            if now_dt.hour == 8 and now_dt.minute == 0 and LAST_DAILY_REPORT_DAY != now_dt.day:
                generate_full_report("🌅 <b>HackNet Morning Status Report</b>")
                LAST_DAILY_REPORT_DAY = now_dt.day

            # Prune old logs and db vacuum every hour (360 loops of 10s)
            if loop_count >= 360:
                try:
                    conn = sqlite3.connect(DB_NAME, timeout=15)
                    conn.execute("DELETE FROM metrics WHERE timestamp <= datetime('now', '-7 days')")
                    conn.execute("DELETE FROM event_logs WHERE timestamp <= datetime('now', '-30 days')")
                    conn.commit()
                    conn.execute("PRAGMA incremental_vacuum(16);")
                    conn.close()
                except Exception: pass
                loop_count = 0

            sleep_duration = max(0, POLL_INTERVAL - (time.time() - start_time))
            end_sleep = time.time() + sleep_duration
            
            # Sub-loop to check for "force_poll" command from UI
            while time.time() < end_sleep:
                time.sleep(0.5)
                try:
                    conn = sqlite3.connect(DB_NAME, timeout=15)
                    fp = conn.execute("SELECT value FROM sys_settings WHERE key='force_poll'").fetchone()
                    if fp and fp[0] == '1':
                        conn.execute("UPDATE sys_settings SET value='0' WHERE key='force_poll'")
                        conn.commit()
                        conn.close()
                        break 
                    conn.close()
                except Exception: pass

    except KeyboardInterrupt: pass

    
    

    

    

    







    

    





    


    

