# HackNet NOC v2.5
**An Enterprise-Grade, Headless Network Operations Center for Raspberry Pi.**

![HackNet NOC Dashboard](docs/dashboard-screenshot.png)
*(Replace this placeholder with a screenshot of your main dashboard)*

HackNet NOC is a high-density, fault-tolerant monitoring appliance designed to track Proxmox hypervisors, Pterodactyl game servers, network routing, and Pi-Hole instances. Built on a lightweight FastAPI and SQLite backend, it features a mathematically auto-scaling 1080p frontend dashboard designed specifically for headless wall-mounted displays.

---

## 🏗️ Architecture & Project Structure

The application is decoupled into resilient layers to ensure maximum uptime, utilizing asynchronous Python polling and a Write-Ahead Logging (WAL) SQLite database.

```text
hacknet/
├── .env                    # System secrets and API keys (Git-ignored)
├── metrics.db              # SQLite Database (Auto-generated)
├── api.py                  # FastAPI REST backend & Pydantic bouncers
├── collector.py            # Multi-threaded polling daemon
├── install.sh              # Automated deployment script
├── README.md
└── static/
    ├── admin.html          # Secure Admin Console UI
    ├── index.html          # Main NOC Display UI
    └── boot_logo.jpeg      # Dashboard background image
```

---

## ✨ Key Features

* **Proxmox Auto-Discovery:** Automatically detects and begins monitoring all running VMs and LXCs on your hypervisor.
* **Core-Aware Game Monitoring:** Pterodactyl CPU usage is dynamically calculated against allocated core limits to prevent false alerts.
* **Pi-Hole v6 Integration:** Natively connects to Pi-Hole's v6 REST API via App Passwords to pull live ad-blocking telemetry.
* **Network Hysteresis (Anti-Flapping):** Ping and TCP checks execute rapid micro-retries if a packet drops, preventing false Telegram alerts caused by standard network jitter.
* **Dynamic Telegram Routing:** Set custom alert cooldowns per-sensor (e.g., alert every 10 mins, or every 24 hours). Includes a "Global Mute" toggle for maintenance windows.
* **1080p Absolute Auto-Scaling:** The UI locks to a 1920x1080 canvas and mathematically scales to fit dead-center on *any* monitor without scrollbars.

---

## 📋 Prerequisites

* **Hardware:** Raspberry Pi 4 Model B (or newer) is highly recommended.
* **OS:** Raspberry Pi OS Lite (64-bit). A 64-bit OS is required for native VS Code Remote SSH support and proper memory allocation.
* **Software:** Python 3.11+

---

## ⚙️ Configuration (`.env` Setup)

The application requires a `.env` file in the root directory to store your infrastructure secrets. **Never commit this file to version control.**

Create a `.env` file and populate it:

```ini
# ==========================================
# HACKNET NOC SECRETS
# ==========================================

# Proxmox Hypervisor
PROXMOX_IP=10.10.x.x
PROXMOX_TOKEN_ID=root@pam!your_token_name
PROXMOX_TOKEN_SECRET=your_proxmox_api_secret_here

# Pterodactyl Panel
PTERO_URL=http://10.10.x.x
PTERO_TOKEN=your_pterodactyl_client_api_key_here

# Telegram Alerting
TELEGRAM_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_telegram_chat_id_here

# Pi-Hole v6 (Optional)
PIHOLE_IP=10.10.x.x
PIHOLE_APP_PASSWORD=your_pihole_app_password_here

# Web UI Admin Security
ADMIN_SECRET_KEY=super_secret_password_change_me
```

---

## 🚀 Deployment

### Option A: Automated Install (Recommended)
Run the included bash script on a fresh Raspberry Pi to automatically install dependencies, configure the UFW firewall, build the Python environment, and register the Systemd services.
```bash
chmod +x install.sh
./install.sh
```

### Option B: Manual Install
**1. Install Dependencies:**
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install python3-venv python3-pip sqlite3 ufw -y
```

**2. Build Python Environment:**
```bash
python3 -m venv .hacknet_env
source .hacknet_env/bin/activate
pip install fastapi uvicorn proxmoxer requests aiohttp python-dotenv pydantic
```

**3. Start Services:**
* `uvicorn api:app --host 0.0.0.0 --port 8000`
* `python collector.py`

---

## 🛠️ Operations & Troubleshooting

The application runs as two Systemd daemons. Use standard `systemctl` and `journalctl` commands to manage and audit the application.

**View Live Collector Logs (Polling & Network Errors):**
```bash
sudo journalctl -u hacknet-collector -f
```

**View Live API Logs (Web Traffic & Auth Errors):**
```bash
sudo journalctl -u hacknet-api -f
```

**Restart Services (Required after code or `.env` changes):**
```bash
sudo systemctl restart hacknet-api hacknet-collector
```

---

## 💡 Pro-Tips for NOC Managers

1. **Accurate Proxmox RAM (QEMU Agent):** By default, Proxmox only reports the *allocated* RAM of a VM. To see the *true* RAM usage on your NOC dashboard, you must enable the "QEMU Guest Agent" in the Proxmox VM hardware options, and install the agent inside the guest OS (`sudo apt install qemu-guest-agent`).
2. **Forcing a Poll:** If you reboot a server and don't want to wait for the standard 10-second sleep loop, click the "Manual Ping Override" button in the Admin Console to instantly break the sleep loop and update the NOC.
3. **Database Maintenance:** The system handles its own database pruning. It deletes metrics older than 7 days, event logs older than 30 days, and runs an incremental vacuum every hour to keep the SQLite database lightning-fast. Do not manually run a full `VACUUM` while the engine is running.


