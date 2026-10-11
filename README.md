# HackNet NOC v2.8.ENT
**An Enterprise-Grade, Headless Network Operations Center for Homelabs.**

![HackNet NOC Dashboard](docs/dashboard-screenshot.png)

HackNet NOC is a high-density, fault-tolerant monitoring appliance designed specifically for homelabs and small enterprise environments. Built on a lightweight FastAPI and SQLite backend, it features a mathematically auto-scaling frontend dashboard optimized for headless wall-mounted displays (1080p/4K).

The system continuously tracks Proxmox hypervisors, VMware ESXi, TrueNAS/Synology/QNAP Storage Arrays, Portainer Docker fleets, Pterodactyl game servers, external network routing, Pi-Hole instances, and Media Servers (Plex/Jellyfin).

---

## ✨ Key Capabilities

* **Root Cause Analysis (RCA) Engine:** Prevents "Alert Storms." If a physical host goes down, HackNet NOC automatically groups the dependent VMs, Docker Containers, and Web Apps into a Dependency Stack. You receive *one* critical alert for the root failure, rather than 50 separate notifications for every offline child service.
* **Smart Auto-Discovery:** Simply provide your API tokens in the Web Wizard. The engine securely maps your Host nodes, LXCs, VMs, Storage Pools, Docker Containers, and Media Streams without manual data entry.
* **Dynamic Modularity:** Don't use Pterodactyl or Pi-Hole? Just skip them during setup. The engine gracefully degrades and only allocates resources to poll exactly what you configure.
* **Smart Hysteresis (Anti-Flapping):** Built-in micro-retries for network and TCP checks prevent false Telegram/Discord alert spam caused by standard network jitter or momentary latency spikes.
* **Automated Reporting:** Compiles and dispatches a full infrastructure status matrix to your Telegram/Discord at 08:00 and 20:00 daily. Reports can also be triggered on-demand via the Admin Console.
* **Native Responsiveness:** The UI uses native CSS Grid to perfectly expand across 4K displays or gracefully collapse into a column layout for mobile viewing, pushing dormant infrastructure cleanly out of sight.

---

## 🏗️ Architecture & The "Watcher" Philosophy

HackNet NOC is engineered to be the ultimate source of truth for your infrastructure. 

**Best Practice:** It is highly recommended to deploy this instance on independent, dedicated hardware (such as a Raspberry Pi or a separate mini-PC). *Do not host HackNet on the exact hypervisor you are trying to monitor.* If your main server goes down, your watcher must remain online and capable of dispatching outbound alerts.

**Under the Hood:**
The application utilizes an asynchronous, thread-pooled Python backend that acts as the collector engine, paired with a FastAPI web server. All configurations, API keys (symmetrically encrypted via 256-bit Fernet), and historical metrics are stored in a local SQLite database utilizing Write-Ahead Logging (WAL) for high-speed, concurrent read/writes. This ensures zero reliance on heavy external databases like Redis or PostgreSQL.

---

## 🚀 Deployment Guide

### Option A: Docker (Highly Recommended)

Deploying via Docker ensures perfect dependency isolation. You do not need to download the source code; you simply pull the pre-built image from the GitHub Container Registry.

1. Create a `docker-compose.yml` file anywhere on your system:
   ```yaml
   services:
     noc-api:
       image: ghcr.io/cbiscuit456/hacknet-noc:latest
       container_name: hacknet-noc-api
       restart: unless-stopped
       ports:
         - "8000:8000"
       volumes:
         - ./data:/app/data

     noc-collector:
       image: ghcr.io/cbiscuit456/hacknet-noc:latest
       container_name: hacknet-noc-collector
       restart: unless-stopped
       volumes:
         - ./data:/app/data
       command: python collector.py
       depends_on:
         - noc-api
   ```
2. Start the application:
   ```bash
   docker-compose up -d
   ```
3. Navigate to `http://<your-ip>:8000` to begin the First-Boot Setup Wizard.

### Option B: Bare Metal / Raspberry Pi OS (One-Liner)

If you prefer to run this directly on a Debian/Ubuntu-based instance (or Raspberry Pi OS 64-bit) without Docker, you can use the automated bash installer.

Simply paste the following command into your terminal:
```bash
curl -sSL https://raw.githubusercontent.com/CBiscuit456/hacknet-noc/master/install.sh | sudo bash
```
*This script will automatically install dependencies, clone the app to `/opt/hacknet-noc`, configure the Python environment, and start the background systemd services.*

---

## ⚙️ Initial Configuration

Unlike older monitoring tools, HackNet NOC does not require you to configure massive `.env` files manually. 

Upon your first visit to the dashboard, you will be guided through the **Web Setup Wizard**. You will be prompted to securely input:
* Your Master Password (which symmetrically encrypts your configuration in the DB)
* Your Core WAN Routing IP (to determine ISP-level internet connectivity)
* Your Integration URLs and API Tokens
* Your Telegram Bot / Discord Webhook URL for alerting

The setup wizard features **Smart Discovery**, which will pre-scan your APIs and propose the optimal monitoring topology (RCA Dependency Stacks) before finalizing the installation.

---

## 🔄 Updating HackNet NOC

HackNet NOC is designed for seamless, non-destructive updates. Your historical data and configurations remain safely untouched in your `data` directory.

**To update via Docker:**
```bash
docker-compose pull
docker-compose up -d
```

**To update via Bare Metal:**
Simply re-run the installation one-liner. It will detect the existing installation at `/opt/hacknet-noc`, pull the latest code, and restart the services automatically.
```bash
curl -sSL https://raw.githubusercontent.com/CBiscuit456/hacknet-noc/master/install.sh | sudo bash
```

---

## 🛠️ Operations & Troubleshooting

* **Accessing the Admin Panel:** Navigate to `http://<your-ip>:8000/admin`. From here, you can trigger global network polls, dispatch status reports, update API keys, map custom TCP/HTTP sensors, and mute global sirens.
* **Accurate Proxmox RAM (QEMU Agent):** By default, Proxmox only reports the *allocated* RAM of a VM. To see the *true* RAM usage on your NOC dashboard, enable the "QEMU Guest Agent" in your Proxmox VM hardware options, and install the agent inside the guest OS (`sudo apt install qemu-guest-agent`).
* **Database Maintenance:** The system handles its own database pruning automatically. It deletes metrics older than 7 days, event logs older than 30 days, and runs an incremental vacuum every hour to keep the database lightning-fast. 

---

## 🗑️ Clean Uninstallation

If you need to completely remove HackNet NOC from your system, follow the instructions below to ensure zero orphaned files or networks are left behind.

**If deployed via Docker:**
```bash
# Spin down containers and remove orphaned volumes
docker-compose down -v --remove-orphans

# Delete the associated Docker images
docker rmi ghcr.io/cbiscuit456/hacknet-noc:latest

# Remove persistent data
sudo rm -rf data/ docker-compose.yml
```

**If deployed via Bare Metal:**
```bash
# Stop and disable the background services
sudo systemctl stop hacknet-api hacknet-collector
sudo systemctl disable hacknet-api hacknet-collector

# Delete the systemd service files and reload the daemon
sudo rm /etc/systemd/system/hacknet-api.service
sudo rm /etc/systemd/system/hacknet-collector.service
sudo systemctl daemon-reload

# Wipe the application directory securely
sudo rm -rf /opt/hacknet-noc/
```

---
*HackNet NOC is open-source software built for the homelab community.*







