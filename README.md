# HackNet NOC v2.8.ENT (Final Edition)
**An Enterprise-Grade, Headless Network Operations Center for Homelabs.**

![HackNet NOC Dashboard](docs/dashboard-screenshot.png)
*(Replace this placeholder with a screenshot of your main dashboard)*

HackNet NOC is a high-density, fault-tolerant monitoring appliance designed to track Proxmox hypervisors, TrueNAS Storage Arrays, Portainer Docker fleets, Pterodactyl game servers, network routing, Pi-Hole instances, and Media Servers (Plex/Jellyfin). Built on a lightweight FastAPI and SQLite backend, it features a mathematically auto-scaling 1080p/4K frontend dashboard designed specifically for headless wall-mounted displays.

---

## ✨ Key Enterprise Features

* **Root Cause Analysis (RCA) Engine:** Prevents "Alert Storms." If a physical host goes down, HackNet NOC automatically groups the dependent VMs, Docker Containers, and Web Apps into a Dependency Stack. You receive *one* critical alert for the root failure, rather than 50 separate notifications.
* **Smart Auto-Discovery:** Simply provide your API tokens in the Web Wizard, and the system automatically discovers and maps your Host nodes, LXCs, VMs, Storage Pools, Docker Containers, and Media Streams. 
* **Dynamic Modularity:** Don't use Pterodactyl or Pi-Hole? Just skip them in the setup wizard. The engine gracefully degrades and only polls what you configure.
* **Smart Hysteresis (Anti-Flapping):** Ping and TCP checks execute rapid micro-retries if a packet drops, preventing false Telegram/Discord spam caused by standard network jitter.
* **Scheduled & On-Demand Reporting:** Automatically compiles and dispatches a full infrastructure status report to your Telegram/Discord at 08:00 and 20:00. Reports can also be triggered on-demand via the Admin Console.
* **Native Responsiveness:** The UI uses native CSS Grid to perfectly expand across 4K displays or gracefully collapse into a column layout for mobile viewing, pushing dormant/paused infrastructure cleanly out of sight.

---

## 🚀 The "Watcher" Philosophy

HackNet NOC is designed to be the ultimate source of truth for your infrastructure. 

**Best Practice:** It is highly recommended to deploy this instance on independent hardware (like a Raspberry Pi or a separate mini-PC). *Do not host HackNet on the exact hypervisor you are trying to monitor.* If your main server goes down, HackNet must stay online to alert you.

---

## 📦 Deployment & Undeployment

### Option A: Docker / Portainer (Highly Recommended)

Deploying via Docker is the easiest and cleanest way to run HackNet NOC. It spins up two lightweight containers: an API backend and a Polling engine.

**To Deploy:**
1. Clone the repository and navigate into it:
   ```bash
   git clone https://github.com/yourusername/hacknet-noc.git
   cd hacknet-noc
   ```
2. Create the data directory (CRITICAL to prevent SQLite permission errors):
   ```bash
   mkdir data
   ```
3. Spin up the stack in the background:
   ```bash
   docker-compose up -d --build
   ```
4. Access your setup wizard at `http://<your-ip>:8000`.

**To Undeploy / Remove:**
1. Stop and remove the containers:
   ```bash
   docker-compose down
   ```
2. *(Optional)* Wipe all persistent data, configurations, and history:
   ```bash
   sudo rm -rf data/
   ```

---

### Option B: Bare Metal / Raspberry Pi (install.sh)

If you prefer to run this directly on a Raspberry Pi OS Lite (64-bit) instance without Docker, an automated deployment script is included.

**To Deploy:**
1. Clone the repository, navigate into it, and make the installer executable:
   ```bash
   git clone https://github.com/yourusername/hacknet-noc.git
   cd hacknet-noc
   chmod +x install.sh
   ```
2. Run the installer as root:
   ```bash
   sudo ./install.sh
   ```
3. Start the systemd services:
   ```bash
   sudo systemctl start hacknet-api hacknet-collector
   ```
4. Access your setup wizard at `http://<your-ip>:8000`.

**To Undeploy / Remove:**
1. Stop the background services:
   ```bash
   sudo systemctl stop hacknet-api hacknet-collector
   ```
2. Disable the services from booting on startup:
   ```bash
   sudo systemctl disable hacknet-api hacknet-collector
   ```
3. Delete the systemd service files:
   ```bash
   sudo rm /etc/systemd/system/hacknet-api.service
   sudo rm /etc/systemd/system/hacknet-collector.service
   sudo systemctl daemon-reload
   ```
4. *(Optional)* Delete the entire application folder to completely wipe the installation:
   ```bash
   cd ..
   sudo rm -rf hacknet-noc/
   ```

---

## ⚙️ Configuration (The Setup Wizard)

Unlike older monitoring tools, HackNet NOC does not require you to configure massive `.env` files. 

When you navigate to the dashboard for the first time, you will be greeted by the **First-Boot Setup Wizard**. From the Web UI, you will be prompted to securely input:
* Your Master Password (which securely encrypts your configuration)
* Your Core WAN Routing IP (to determine internet connectivity)
* Your Integration URLs and API Tokens (Proxmox, Portainer, TrueNAS, etc.)
* Your Telegram Bot / Discord Webhook URL for alerting

The setup wizard features **Smart Discovery**, which will pre-scan your APIs and propose the optimal monitoring topology (RCA Dependency Stacks) before you finalize the installation.

---

## 🛠️ Operations & Troubleshooting

* **Accessing the Admin Panel:** Navigate to `http://<your-ip>:8000/admin`. From here, you can trigger global network polls, dispatch status reports, update API keys, add custom Ping/TCP sensors, and mute active alerts.
* **Accurate Proxmox RAM (QEMU Agent):** By default, Proxmox only reports the *allocated* RAM of a VM. To see the *true* RAM usage on your NOC dashboard, enable the "QEMU Guest Agent" in your Proxmox VM hardware options, and install the agent inside the guest OS (`sudo apt install qemu-guest-agent`).
* **Database Maintenance:** The system handles its own database pruning. It deletes metrics older than 7 days, event logs older than 30 days, and runs an incremental vacuum every hour to keep the SQLite database lightning-fast. Do not manually run a full `VACUUM` while the engine is running.




