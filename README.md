# HackNet NOC v2.7 (Enterprise Edition)
**An Enterprise-Grade, Headless Network Operations Center for Homelabs.**

![HackNet NOC Dashboard](docs/dashboard-screenshot.png)
*(Replace this placeholder with a screenshot of your main dashboard)*

HackNet NOC is a high-density, fault-tolerant monitoring appliance designed to track Proxmox hypervisors, Proxmox Storage Arrays, Pterodactyl game servers, network routing, and Pi-Hole instances. Built on a lightweight FastAPI and SQLite backend, it features a mathematically auto-scaling 1080p frontend dashboard designed specifically for headless wall-mounted displays.

---

## ✨ Key Features

* **Zero-Config Auto-Discovery:** Simply provide a Proxmox API token, and the system automatically discovers and monitors your Host node, LXCs, VMs, and Storage Arrays (NAS/LVM). 
* **Dynamic Modularity:** Don't use Pterodactyl or Pi-Hole? Just leave their `.env` variables blank. The engine gracefully degrades and only polls what you configure.
* **Smart Hysteresis (Anti-Flapping):** Ping and TCP checks execute rapid micro-retries if a packet drops, preventing false Telegram spam caused by standard network jitter.
* **Core-Aware Game Monitoring:** Pterodactyl CPU usage is dynamically calculated against allocated core limits to prevent false 300% CPU alerts.
* **1080p Absolute Auto-Scaling:** The UI locks to a 1920x1080 canvas and mathematically scales to fit dead-center on *any* monitor without scrollbars.
* **Dynamic SVG Sparklines:** Historical CPU and Latency trends (last 10 minutes) are generated efficiently in the background of sensor cards without heavy chart libraries.
* **Sleek Admin Console:** Manage alert thresholds, custom deployment locations (e.g., "US-East" vs "Local Rack"), and pause/resume specific sensors dynamically via the UI without touching config files.

---

## 🚀 Deployment Methods

### Option A: Docker / Portainer (Highly Recommended)

Deploying via Docker is the easiest and cleanest way to run HackNet NOC. It spins up two lightweight containers: an API backend and a Polling engine.

**1. Clone the repository:**
```bash
git clone https://github.com/yourusername/hacknet-noc.git
cd hacknet-noc
```

**2. Create your `.env` file:**
Create a `.env` file in the root directory (see Configuration section below).

**3. Create the data directory (CRITICAL):**
> **Note:** You *must* create an empty `data` directory before running Docker. If you don't, Docker will create it as a root-owned directory, which will cause SQLite permission crashes.
```bash
mkdir data
```

**4. Spin up the stack:**
```bash
docker-compose up -d
```
Access your dashboard at `http://<your-ip>:8000`.

---

### Option B: Bare Metal / Raspberry Pi (install.sh)

If you prefer to run this directly on a Raspberry Pi OS Lite (64-bit) instance without Docker, an automated deployment script is included.

```bash
chmod +x install.sh
sudo ./install.sh
```
This script will install Python dependencies, create a virtual environment, set up systemd services (`hacknet-api` and `hacknet-collector`), and configure the UFW firewall automatically.

---

## ⚙️ Configuration (`.env` Setup)

The application requires a `.env` file in the root directory to store your infrastructure secrets. 

**Any module you do not wish to use (like Pi-Hole or Pterodactyl) can simply be left blank.**

```ini
# ==========================================
# HACKNET NOC SECRETS
# ==========================================

# Proxmox Hypervisor (Leave blank to disable)
PROXMOX_IP=10.10.x.x
PROXMOX_TOKEN_ID=root@pam!your_token_name
PROXMOX_TOKEN_SECRET=your_proxmox_api_secret_here

# Pterodactyl Panel (Leave blank to disable)
PTERO_URL=http://10.10.x.x
PTERO_TOKEN=your_pterodactyl_client_api_key_here

# Telegram Alerting (Leave blank to disable)
TELEGRAM_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_telegram_chat_id_here

# Pi-Hole v6 (Leave blank to disable)
PIHOLE_IP=10.10.x.x
PIHOLE_APP_PASSWORD=your_pihole_app_password_here

# Web UI Admin Security (Required)
ADMIN_SECRET_KEY=super_secret_password_change_me
```

---

## 🛠️ Operations & Troubleshooting

* **Accessing the Admin Panel:** Navigate to `http://<your-ip>:8000/admin` and log in using your `ADMIN_SECRET_KEY`. From here, you can add new network/app sensors, pause auto-discovered VMs, and set alert thresholds.
* **Accurate Proxmox RAM (QEMU Agent):** By default, Proxmox only reports the *allocated* RAM of a VM. To see the *true* RAM usage on your NOC dashboard, enable the "QEMU Guest Agent" in your Proxmox VM hardware options, and install the agent inside the guest OS (`sudo apt install qemu-guest-agent`).
* **Database Maintenance:** The system handles its own database pruning. It deletes metrics older than 7 days, event logs older than 30 days, and runs an incremental vacuum every hour to keep the SQLite database lightning-fast. Do not manually run a full `VACUUM` while the engine is running.




