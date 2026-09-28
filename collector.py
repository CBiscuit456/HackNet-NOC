import time
import sqlite3
import subprocess
import socket
import requests
import os
import datetime
import urllib3
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from proxmoxer import ProxmoxAPI
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==========================================
# SECRETS & CONFIG
# ==========================================
load_dotenv()
PROXMOX_IP = os.getenv('PROXMOX_IP')
TOKEN_ID = os.getenv('PROXMOX_TOKEN_ID')
TOKEN_SECRET = os.getenv('PROXMOX_TOKEN_SECRET')
PTERO_URL = os.getenv('PTERO_URL')
PTERO_TOKEN = os.getenv('PTERO_TOKEN')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
PIHOLE_IP = os.getenv('PIHOLE_IP')
PIHOLE_APP_PASSWORD = os.getenv('PIHOLE_APP_PASSWORD')

POLL_INTERVAL = 10
DB_NAME = 'metrics.db'
IGNORE_ENTITIES = ['expanse-svr']

ALERT_STATE = {}
PTERO_LIMITS_CACHE = {}
LAST_DAILY_REPORT_DAY = None

GAME_ESCALATION_TIME = 86400
GAME_ESCALATION_COOLDOWN = 86400

# ==========================================
# UTILITY & LOGGING FUNCTIONS
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

def log_event(entity, event_type, message):
    try:
        conn = sqlite3.connect(DB_NAME, timeout=15)
        conn.execute('INSERT INTO event_logs (entity_name, event_type, message) VALUES (?, ?, ?)', (entity, event_type, message))
        conn.commit()
        conn.close()
    except Exception:
        pass

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try: 
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=5)
    except Exception:
        pass

def generate_full_report(title="📊 <b>NOC Status Report</b>"):
    try:
        conn = sqlite3.connect(DB_NAME, timeout=15)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT m.entity_name, m.entity_type, m.status, m.ping_ms, m.cpu_usage, m.mem_used, t.is_enabled FROM metrics m LEFT JOIN monitored_targets t ON m.entity_name = t.name AND m.entity_type = t.entity_type WHERE m.id IN (SELECT MAX(id) FROM metrics GROUP BY entity_name, entity_type)").fetchall()
        conn.close()
    except Exception:
        return

    net_down, vm_down, lxc_down, app_down, game_down = [], [], [], [], []
    vms_tot, lxc_tot, app_tot, game_tot = 0, 0, 0, 0
    host_str, net_wan = "OFFLINE", "OFFLINE"
    unmonitored = []

    for r in rows:
        n, t = r['entity_name'], r['entity_type']
        if r['is_enabled'] == 0 or r['status'] == 'paused':
            if t != 'host': unmonitored.append(n)
            continue
        is_up = (r['status'] == 'running')
        if t == 'network':
            if not is_up: net_down.append(n)
            if n == 'WAN_DNS' and is_up: net_wan = f"{r['ping_ms']:.0f}ms"
        elif t == 'host':
            if is_up: host_str = f"{r['cpu_usage']:.1f}% CPU | {r['mem_used']:.1f}GB RAM"
        elif t == 'vm':
            vms_tot += 1
            if not is_up: vm_down.append(n)
        elif t == 'lxc':
            lxc_tot += 1
            if not is_up: lxc_down.append(n)
        elif t == 'app':
            app_tot += 1
            if not is_up: app_down.append(n)
        elif t == 'game':
            game_tot += 1
            if not is_up: game_down.append(n)

    def fmt(tot, down): return f"🟢 {tot - len(down)}/{tot} UP" if not down else f"🔴 {tot - len(down)}/{tot} UP [{', '.join(down)}]"
    msg = f"{title}\n\n🌐 <b>NETWORK:</b> {'🟢 ONLINE' if not net_down else '🔴 FAIL ['+', '.join(net_down)+']'} (WAN: {net_wan})\n🖥️ <b>HOST:</b> {host_str}\n📦 <b>VMs:</b> {fmt(vms_tot, vm_down)}\n📦 <b>LXCs:</b> {fmt(lxc_tot, lxc_down)}\n🔌 <b>WEB:</b> {fmt(app_tot, app_down)}\n🎮 <b>GAMES:</b> {fmt(game_tot, game_down)}"
    if unmonitored: msg += f"\n\n🔧 <b>UNMONITORED:</b> [{', '.join(unmonitored)}]"
    send_telegram(msg)

def setup_database():
    conn = sqlite3.connect(DB_NAME, timeout=15)
    cursor = conn.cursor()
    cursor.execute('PRAGMA journal_mode=WAL;')
    cursor.execute('PRAGMA auto_vacuum = INCREMENTAL;')
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS metrics (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, entity_name TEXT, entity_type TEXT, status TEXT, cpu_usage REAL, mem_used REAL, mem_total REAL, disk_used REAL, disk_total REAL, net_in REAL, net_out REAL, ping_ms REAL)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS alert_state (entity_metric TEXT PRIMARY KEY, is_alerting INTEGER, first_alert_time REAL, last_alert_time REAL, count INTEGER)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS monitored_targets (name TEXT, entity_type TEXT, target_address TEXT, port INTEGER, uuid TEXT, is_enabled INTEGER DEFAULT 1, down_since REAL DEFAULT 0.0, PRIMARY KEY(name, entity_type))''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS event_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, entity_name TEXT, event_type TEXT, message TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS sys_settings (key TEXT PRIMARY KEY, value TEXT)''')
    
    cursor.execute('INSERT OR IGNORE INTO sys_settings (key, value) VALUES ("global_mute", "0")')
    cursor.execute('INSERT OR IGNORE INTO sys_settings (key, value) VALUES ("force_poll", "0")')
    cursor.execute('''CREATE TABLE IF NOT EXISTS pihole_stats (id INTEGER PRIMARY KEY, status TEXT, domains_blocked INTEGER, queries_today INTEGER, ads_blocked INTEGER, ads_percentage REAL)''')

    cursor.execute("PRAGMA table_info(monitored_targets)")
    columns = [c[1] for c in cursor.fetchall()]
    if 'cooldown_interval' not in columns: cursor.execute("ALTER TABLE monitored_targets ADD COLUMN cooldown_interval INTEGER DEFAULT 3600")
    if 'cpu_threshold' not in columns: cursor.execute("ALTER TABLE monitored_targets ADD COLUMN cpu_threshold REAL DEFAULT 90.0")
    if 'mem_threshold' not in columns: cursor.execute("ALTER TABLE monitored_targets ADD COLUMN mem_threshold REAL DEFAULT 90.0")

    cursor.execute('CREATE INDEX IF NOT EXISTS idx_metrics_entity ON metrics(entity_name, entity_type)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_metrics_timestamp ON metrics(timestamp)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_events_timestamp ON event_logs(timestamp)')
    
    for row in cursor.execute('SELECT entity_metric, is_alerting, first_alert_time, last_alert_time, count FROM alert_state').fetchall():
        ALERT_STATE[row[0]] = {'is_alerting': bool(row[1]), 'first_alert_time': row[2], 'last_alert_time': row[3], 'count': row[4]}
    
    conn.commit()
    conn.close()

def sync_alert_db(key, state):
    try:
        conn = sqlite3.connect(DB_NAME, timeout=15)
        conn.execute('''INSERT INTO alert_state (entity_metric, is_alerting, first_alert_time, last_alert_time, count) VALUES (?, ?, ?, ?, ?) ON CONFLICT(entity_metric) DO UPDATE SET is_alerting=excluded.is_alerting, first_alert_time=excluded.first_alert_time, last_alert_time=excluded.last_alert_time, count=excluded.count''', (key, int(state['is_alerting']), state['first_alert_time'], state['last_alert_time'], state['count']))
        conn.commit()
        conn.close()
    except Exception:
        pass

def process_alert(entity, metric, condition_met, trigger_count, msg_trigger, msg_recover, down_since=0.0, is_game=False, global_mute=False, cooldown_interval=3600, silent_resolve=False):
    key = f"{entity}_{metric}"
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
                if not global_mute: send_telegram(f"🚨 <b>ALERT: {entity}</b>\n{msg_trigger}")
                log_event(entity, 'CRITICAL', msg_trigger)
                state['is_alerting'] = True
                state['first_alert_time'] = down_since if down_since > 0 else now
                state['last_alert_time'] = now
                state_changed = True
            else:
                time_down = now - state['first_alert_time']
                cooldown = GAME_ESCALATION_COOLDOWN if (is_game and time_down >= GAME_ESCALATION_TIME) else cooldown_interval
                if (now - state['last_alert_time']) > cooldown:
                    if not global_mute: send_telegram(f"🚨 <b>REMINDER: {entity}</b>\n{msg_trigger} (Down for {format_time_delta(time_down)})")
                    state['last_alert_time'] = now
                    state_changed = True
    else:
        if state['is_alerting']:
            duration = format_time_delta(now - state['first_alert_time'])
            if not silent_resolve:
                if not global_mute: send_telegram(f"🟢 <b>RESOLVED: {entity}</b>\n{msg_recover} (Was down for {duration})")
                log_event(entity, 'RESOLVED', f"{msg_recover} (Downtime: {duration})")
            state['is_alerting'] = False
            state['first_alert_time'] = 0
            state['count'] = 0
            state_changed = True
        elif state['count'] > 0:
            state['count'] = 0
            state_changed = True
            
    if state_changed: sync_alert_db(key, state)

# ==========================================
# WORKER FUNCTIONS WITH HYSTERESIS
# ==========================================
def collect_pihole_stats():
    if not PIHOLE_IP: return
    try:
        url = f"http://{PIHOLE_IP}/api/stats/summary"
        auth = ("", PIHOLE_APP_PASSWORD) if PIHOLE_APP_PASSWORD else None
        res = requests.get(url, auth=auth, timeout=5, verify=False)
        if res.status_code == 200:
            d = res.json()
            state = d.get('state', 'unknown')
            domains = d.get('gravity', {}).get('domains_being_blocked', 0)
            queries = d.get('queries', {}).get('total', 0)
            blocked = d.get('queries', {}).get('blocked', 0)
            percent = d.get('queries', {}).get('percent_blocked', 0.0)
            
            conn = sqlite3.connect(DB_NAME, timeout=15)
            conn.execute('DELETE FROM pihole_stats')
            conn.execute('INSERT INTO pihole_stats (id, status, domains_blocked, queries_today, ads_blocked, ads_percentage) VALUES (1, ?, ?, ?, ?, ?)', (state, domains, queries, blocked, percent))
            conn.commit()
            conn.close()
    except Exception: pass

def get_ptero_cpu_limit(uuid, headers):
    try:
        res = requests.get(f"{PTERO_URL}/api/client/servers/{uuid}", headers=headers, timeout=5)
        if res.status_code == 200:
            PTERO_LIMITS_CACHE[uuid] = max(res.json()['attributes']['limits']['cpu'], 100)
            return PTERO_LIMITS_CACHE[uuid]
    except Exception: pass
    # Fallback to cache if API times out, to prevent 300% spikes
    return PTERO_LIMITS_CACHE.get(uuid, 100)

def check_ping(name, ip):
    # Hysteresis: Retry up to 3 times before declaring dead
    for attempt in range(3):
        try:
            out = subprocess.run(['ping', '-c', '1', '-W', '1', ip], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if out.returncode == 0:
                match = re.search(r'time=([\d\.]+)', out.stdout)
                return ('network', name, 'running', 0.0, 0.0, 0.0, float(match.group(1)) if match else 0.0)
        except Exception: pass
        time.sleep(0.5)
    return ('network', name, 'stopped', 0.0, 0.0, 0.0, 0.0)

def check_tcp(name, ip, port):
    # Hysteresis: Retry up to 3 times
    for attempt in range(3):
        try:
            with socket.create_connection((ip, port), timeout=3): 
                return ('app', name, 'running', 0.0, 0.0, 0.0, 0.0)
        except Exception:
            time.sleep(0.5)
    return ('app', name, 'stopped', 0.0, 0.0, 0.0, 0.0)

def check_ptero(name, uuid):
    headers = {"Authorization": f"Bearer {PTERO_TOKEN}", "Accept": "application/json"}
    for attempt in range(3):
        try:
            res = requests.get(f"{PTERO_URL}/api/client/servers/{uuid}/resources", headers=headers, timeout=5)
            if res.status_code == 200:
                r = res.json()['attributes']
                status = 'running' if r['current_state'] == 'running' else 'stopped'
                limit = get_ptero_cpu_limit(uuid, headers)
                actual_cpu = (r['resources'].get('cpu_absolute', 0.0) / limit) * 100
                mem_used = r['resources'].get('memory_bytes', 0.0) / (1024**3)
                
                # Cache total memory safely
                mem_total = 0.0
                try:
                    res_details = requests.get(f"{PTERO_URL}/api/client/servers/{uuid}", headers=headers, timeout=5)
                    if res_details.status_code == 200:
                        mem_total = (res_details.json()['attributes']['limits']['memory'] / 1024)
                except Exception: pass
                
                return ('game', name, status, actual_cpu, mem_used, mem_total, 0.0)
        except Exception:
            time.sleep(0.5)
    return ('game', name, 'stopped', 0.0, 0.0, 0.0, 0.0)

# ==========================================
# MAIN LOOP
# ==========================================
def collect_and_store():
    try:
        conn = sqlite3.connect(DB_NAME, timeout=15)
        cfg = {(r[0], r[1]): {'ip': r[2], 'port': r[3], 'uuid': r[4], 'enabled': r[5], 'down_since': r[6], 'cooldown': r[7], 'cpu_thresh': r[8], 'mem_thresh': r[9]} 
               for r in conn.execute("SELECT name, entity_type, target_address, port, uuid, is_enabled, down_since, cooldown_interval, cpu_threshold, mem_threshold FROM monitored_targets").fetchall()}
        mute_setting = conn.execute("SELECT value FROM sys_settings WHERE key='global_mute'").fetchone()
        global_mute = True if mute_setting and mute_setting[0] == '1' else False
        conn.close()
    except Exception as e:
        print(f"Database read error: {e}")
        return

    metrics_batch, downtime_updates, alerts, new_entities = [], [], [], []
    now = time.time()
    futures = []

    # Dynamic pool limits to prevent thread exhaustion
    pool_size = min(40, len(cfg) + 5)
    
    with ThreadPoolExecutor(max_workers=pool_size) as executor:
        executor.submit(collect_pihole_stats)
        
        for (name, e_type), data in cfg.items():
            if data['enabled'] == 0:
                metrics_batch.append((name, e_type, 'paused', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
                alerts.append((name, 'offline', False, 3, "", "", 0.0, e_type == 'game', data['cooldown'], True))
                alerts.append((name, 'cpu', False, 3, "", "", 0.0, False, data['cooldown'], True))
                alerts.append((name, 'ram', False, 3, "", "", 0.0, False, data['cooldown'], True))
                continue
                
            if e_type == 'network': futures.append(executor.submit(check_ping, name, data['ip']))
            elif e_type == 'app': futures.append(executor.submit(check_tcp, name, data['ip'], data['port']))
            elif e_type == 'game': futures.append(executor.submit(check_ptero, name, data['uuid']))

        # Proxmox Logic with API Retries
        proxmox_success = False
        for attempt in range(2):
            try:
                proxmox = ProxmoxAPI(PROXMOX_IP, user=TOKEN_ID.split('!')[0], token_name=TOKEN_ID.split('!')[1], token_value=TOKEN_SECRET, verify_ssl=False, timeout=6)
                my_node = proxmox.nodes.get()[0]['node']
                
                host_status = proxmox.nodes(my_node).status.get()
                host_cpu = (host_status.get('cpu') or 0.0) * 100
                host_mu = (host_status.get('memory', {}).get('used') or 0.0) / (1024**3)
                host_mt = (host_status.get('memory', {}).get('total') or 0.0) / (1024**3)
                
                if cfg.get(('CORE-PVE', 'host'), {}).get('enabled') != 0:
                    metrics_batch.append(('CORE-PVE', 'host', 'running', host_cpu, host_mu, host_mt, 0.0, 0.0, 0.0, 0.0, 0.0))
                    cldwn = cfg.get(('CORE-PVE', 'host'), {}).get('cooldown', 3600)
                    cpu_thresh = cfg.get(('CORE-PVE', 'host'), {}).get('cpu_thresh', 90.0)
                    alerts.append(('CORE-PVE', 'api_offline', False, 2, "", "", 0.0, False, cldwn, False))
                    alerts.append(('CORE-PVE', 'cpu', host_cpu > cpu_thresh, 3, f"Host CPU critical at {host_cpu:.1f}%", "Host CPU normalized.", 0.0, False, cldwn, False))

                for lxc in proxmox.nodes(my_node).lxc.get():
                    name = lxc.get('name')
                    if name in IGNORE_ENTITIES: continue
                    if (name, 'lxc') not in cfg:
                        new_entities.append((name, 'lxc', 1))
                        cfg[(name, 'lxc')] = {'enabled': 1, 'down_since': 0.0, 'cooldown': 3600, 'cpu_thresh': 90.0, 'mem_thresh': 90.0}
                    if cfg[(name, 'lxc')]['enabled'] == 0: continue
                    
                    if lxc.get('status') == 'running':
                        metrics_batch.append((name, 'lxc', 'running', (lxc.get('cpu') or 0.0)*100, (lxc.get('mem') or 0)/1073741824, (lxc.get('maxmem') or 0)/1073741824, 0.0, 0.0, 0.0, 0.0, 0.0))
                    else:
                        metrics_batch.append((name, 'lxc', 'stopped', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

                for vm in proxmox.nodes(my_node).qemu.get():
                    name = vm.get('name')
                    vmid = vm.get('vmid')
                    if name in IGNORE_ENTITIES: continue
                    if (name, 'vm') not in cfg:
                        new_entities.append((name, 'vm', 1))
                        cfg[(name, 'vm')] = {'enabled': 1, 'down_since': 0.0, 'cooldown': 3600, 'cpu_thresh': 90.0, 'mem_thresh': 90.0}
                    if cfg[(name, 'vm')]['enabled'] == 0: continue
                    
                    if vm.get('status') == 'running':
                        try:
                            vm_stat = proxmox.nodes(my_node).qemu(vmid).status.current.get()
                            v_cpu = (vm_stat.get('cpu') or 0.0) * 100
                            v_mu = (vm_stat.get('mem') or 0.0) / 1073741824
                            v_mt = (vm_stat.get('maxmem') or 0.0) / 1073741824
                        except Exception:
                            v_cpu = (vm.get('cpu') or 0.0) * 100
                            v_mu = (vm.get('mem') or 0.0) / 1073741824
                            v_mt = (vm.get('maxmem') or 0.0) / 1073741824
                        metrics_batch.append((name, 'vm', 'running', v_cpu, v_mu, v_mt, 0.0, 0.0, 0.0, 0.0, 0.0))
                    else:
                        metrics_batch.append((name, 'vm', 'stopped', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
                proxmox_success = True
                break
            except Exception:
                time.sleep(1)
        
        if not proxmox_success:
            if cfg.get(('CORE-PVE', 'host'), {}).get('enabled') != 0:
                metrics_batch.append(('CORE-PVE', 'host', 'stopped', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
                cldwn = cfg.get(('CORE-PVE', 'host'), {}).get('cooldown', 3600)
                alerts.append(('CORE-PVE', 'api_offline', True, 2, "Proxmox API unreachable.", "Proxmox API online.", 0.0, False, cldwn, False))

        # Fault Tolerant Thread Resolution (15s Absolute Max)
        for future in as_completed(futures, timeout=15):
            try:
                res = future.result()
                if res: metrics_batch.append((res[1], res[0], res[2], res[3], res[4], res[5], 0.0, 0.0, 0.0, 0.0, res[6]))
            except Exception: pass

    # Alert Compilation
    for m in metrics_batch:
        name, e_type, status = m[0], m[1], m[2]
        if status == 'paused': continue
        db_down = cfg.get((name, e_type), {}).get('down_since', 0.0)
        cldn = cfg.get((name, e_type), {}).get('cooldown', 3600)
        cpu_t = cfg.get((name, e_type), {}).get('cpu_thresh', 90.0)
        mem_t = cfg.get((name, e_type), {}).get('mem_thresh', 90.0)
        is_game = (e_type == 'game')
        
        if status == 'stopped':
            down_since = now if db_down == 0.0 else db_down
            if db_down == 0.0: downtime_updates.append((now, name, e_type))
            alerts.append((name, 'offline', True, 3, f"{name} is offline.", f"{name} is back online.", down_since, is_game, cldn, False))
            
            # Silent Resolve clears CPU/RAM locks instantly without telegram spam
            alerts.append((name, 'cpu', False, 3, "", "", 0.0, False, cldn, True))
            alerts.append((name, 'ram', False, 3, "", "", 0.0, False, cldn, True))
        else:
            if db_down != 0.0: downtime_updates.append((0.0, name, e_type))
            alerts.append((name, 'offline', False, 3, "", "", db_down, is_game, cldn, False))
            
            cpu_usage, mem_used, mem_total = m[3], m[4], m[5]
            if cpu_usage > 0:
                alerts.append((name, 'cpu', cpu_usage > cpu_t, 3, f"CPU critical at {cpu_usage:.1f}%", "CPU normalized.", 0.0, False, cldn, False))
            if mem_total > 0:
                mem_pct = (mem_used / mem_total) * 100
                alerts.append((name, 'ram', mem_pct > mem_t, 3, f"RAM critical at {mem_pct:.1f}%", "RAM normalized.", 0.0, False, cldn, False))

    # Safe DB Writing
    try:
        w_conn = sqlite3.connect(DB_NAME, timeout=15)
        for name, e_type, enabled in new_entities:
            w_conn.execute('INSERT OR IGNORE INTO monitored_targets (name, entity_type, is_enabled, down_since, cooldown_interval, cpu_threshold, mem_threshold) VALUES (?, ?, ?, 0.0, 3600, 90.0, 90.0)', (name, e_type, enabled))
        for stamp, name, e_type in downtime_updates:
            w_conn.execute('UPDATE monitored_targets SET down_since = ? WHERE name = ? AND entity_type = ?', (stamp, name, e_type))
        w_conn.executemany('INSERT INTO metrics (entity_name, entity_type, status, cpu_usage, mem_used, mem_total, disk_used, disk_total, net_in, net_out, ping_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', metrics_batch)
        w_conn.commit()
        w_conn.close()
    except Exception as e:
        print(f"Database write error: {e}")

    for a in alerts: process_alert(a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7], global_mute, a[8], a[9])

if __name__ == "__main__":
    setup_database()
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

    





    


    

