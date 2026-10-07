# HackNet NOC v2.8 (Enterprise Edition)
**An Enterprise-Grade, Headless Network Operations Center for Homelabs.**

![HackNet NOC Dashboard](docs/dashboard-screenshot.png)
*(Replace this placeholder with a screenshot of your main dashboard)*

HackNet NOC is a high-density, fault-tolerant monitoring appliance designed to track Proxmox hypervisors, Proxmox Storage Arrays, Pterodactyl game servers, network routing, and Pi-Hole instances. Built on a lightweight FastAPI and SQLite backend, it features a mathematically auto-scaling 1080p frontend dashboard designed specifically for headless wall-mounted displays.

---

## ✨ Key Features

* **Zero-Config Auto-Discovery:** Simply provide a Proxmox API token in the Web Wizard, and the system automatically discovers and monitors your Host node, LXCs, VMs, and Storage Arrays (NAS/LVM). 
* **Dynamic Modularity:** Don't use Pterodactyl or Pi-Hole? Just skip them in the setup wizard. The engine gracefully degrades and only polls what you configure.
* **Smart Hysteresis (Anti-Flapping):** Ping and TCP checks execute rapid micro-retries if a packet drops, preventing false Telegram/Discord spam caused by standard network jitter.
* **Core-Aware Game Monitoring:** Pterodactyl CPU usage is dynamically calculated against allocated core limits to prevent false 300% CPU alerts.
* **Native Responsiveness:** The UI uses native CSS Grid to perfectly expand across 4K displays or gracefully collapse into a column layout for mobile viewing.
* **Dynamic SVG Sparklines:** Historical CPU and Latency trends (last 10 minutes) are generated efficiently in the background of sensor cards without heavy chart libraries.
* **Sleek Admin Console:** Manage alert thresholds, custom deployment locations (e.g., "US-East" vs "Local Rack"), and pause/resume specific sensors dynamically via the UI.

---

## 🚀 Deployment Methods

### Option A: Docker / Portainer (Highly Recommended)

Deploying via Docker is the easiest and cleanest way to run HackNet NOC. It spins up two lightweight containers: an API backend and a Polling engine.

**1. Clone the repository:**
```bash
git clone https://github.com/yourusername/hacknet-noc.git
cd hacknet-noc
```

**2. Create the data directory (CRITICAL):**
> **Note:** You *must* create an empty `data` directory before running Docker. If you don't, Docker will create it as a root-owned directory, which will cause SQLite permission crashes.
```bash
mkdir data
```

**3. Spin up the stack:**
```bash
docker-compose up -d
```
Access your setup wizard at `http://<your-ip>:8000`.

---

### Option B: Bare Metal / Raspberry Pi (install.sh)

If you prefer to run this directly on a Raspberry Pi OS Lite (64-bit) instance without Docker, an automated deployment script is included.

```bash
chmod +x install.sh
sudo ./install.sh
```
This script will install Python dependencies, create a virtual environment, set up systemd services, generate the `data` folder, and configure the UFW firewall automatically. 

Once installed, start the services:
```bash
sudo systemctl start hacknet-api hacknet-collector
```

---

## ⚙️ Configuration (The Setup Wizard)

Unlike older monitoring tools, HackNet NOC does not require you to configure massive `.env` files. 

When you navigate to the dashboard for the first time, you will be greeted by the **First-Boot Setup Wizard**. From the Web UI, you will be prompted to securely input:
* Your Proxmox IPs and API Tokens
* Your Pterodactyl URL and Client Key
* Your Pi-Hole IP
* Your Telegram Bot / Discord Webhook URL for alerting

These are securely saved to the persistent SQLite database. If you ever need to update or remove an integration later, you can do so from the "Integrations" tab inside the Admin Console.

---

## 🛠️ Operations & Troubleshooting

* **Accessing the Admin Panel:** Navigate to `http://<your-ip>:8000/admin`. From here, you can add custom Ping/TCP sensors, pause auto-discovered VMs, and update alert thresholds.
* **Accurate Proxmox RAM (QEMU Agent):** By default, Proxmox only reports the *allocated* RAM of a VM. To see the *true* RAM usage on your NOC dashboard, enable the "QEMU Guest Agent" in your Proxmox VM hardware options, and install the agent inside the guest OS (`sudo apt install qemu-guest-agent`).
* **Database Maintenance:** The system handles its own database pruning. It deletes metrics older than 7 days, event logs older than 30 days, and runs an incremental vacuum every hour to keep the SQLite database lightning-fast. Do not manually run a full `VACUUM` while the engine is running.




