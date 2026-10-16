#!/bin/bash

# HackNet NOC - Automated Bare-Metal Deployment Script
# Run with: curl -sSL https://raw.githubusercontent.com/CBiscuit456/hacknet-noc/master/install.sh | sudo bash

echo -e "\n\033[1;32m====================================\033[0m"
echo -e "\033[1;32m  HACKNET NOC INITIALIZATION SCRIPT \033[0m"
echo -e "\033[1;32m====================================\033[0m\n"

# 1. Check for root privileges
if [ "$EUID" -ne 0 ]; then 
  echo -e "\033[1;31mPlease run this script as root (e.g., curl -sSL ... | sudo bash)\033[0m"
  exit 1
fi

# Determine real user to prevent root-owned files breaking the app
APP_USER=$SUDO_USER
if [ -z "$APP_USER" ]; then APP_USER=$(whoami); fi

INSTALL_DIR="/opt/hacknet-noc"
REPO_URL="https://github.com/CBiscuit456/hacknet-noc.git"

echo -e "\033[1;36m[1/6] Installing OS Dependencies...\033[0m"
apt update -y
apt install -y git python3-venv python3-pip sqlite3 ufw curl

echo -e "\033[1;36m[2/6] Fetching HackNet NOC to $INSTALL_DIR...\033[0m"
if [ -d "$INSTALL_DIR" ]; then
    echo -e "   -> Directory exists. Pulling latest updates..."
    cd $INSTALL_DIR
    git pull
else
    git clone $REPO_URL $INSTALL_DIR
    cd $INSTALL_DIR
fi

# Ensure the real user owns the directory
chown -R $APP_USER:$APP_USER $INSTALL_DIR

echo -e "\033[1;36m[3/6] Building Python Environment...\033[0m"
sudo -u $APP_USER python3 -m venv .hacknet_env
sudo -u $APP_USER .hacknet_env/bin/pip install -r requirements.txt

echo -e "\033[1;36m[4/6] Setting up Data Directory...\033[0m"
sudo -u $APP_USER mkdir -p data

if [ ! -f .env ]; then
    sudo -u $APP_USER cat << 'EOF' > .env
# HackNet NOC Enterprise
# Security and Integrations are managed dynamically via the Web UI Setup Wizard.
DB_NAME=data/metrics.db
EOF
    echo -e "   -> Created template .env file."
else
    echo -e "   -> .env file already exists. Skipping."
fi

echo -e "\033[1;36m[5/6] Creating Systemd Background Services...\033[0m"
cat << EOF > /etc/systemd/system/hacknet-api.service
[Unit]
Description=HackNet API
After=network.target

[Service]
User=$APP_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.hacknet_env/bin/uvicorn api:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
EOF

cat << EOF > /etc/systemd/system/hacknet-collector.service
[Unit]
Description=HackNet Collector
After=network.target hacknet-api.service

[Service]
User=$APP_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.hacknet_env/bin/python collector.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable hacknet-api hacknet-collector
systemctl restart hacknet-api hacknet-collector

echo -e "\033[1;36m[6/6] Configuring UFW Firewall...\033[0m"
# Safely allow the port without forcefully enabling the firewall to prevent SSH lockouts on custom ports
if command -v ufw >/dev/null 2>&1; then
    ufw allow 8000/tcp comment 'HackNet NOC API'
    echo -e "   -> Port 8000 allowed in UFW."
    echo -e "   -> \033[1;33mNOTE: If UFW is currently inactive, you must enable it manually.\033[0m"
else
    echo -e "   -> UFW not installed, skipping firewall configuration."
fi

echo -e "\n\033[1;32m====================================\033[0m"
echo -e "\033[1;32m DEPLOYMENT COMPLETE! \033[0m"
echo -e "\033[1;32m====================================\033[0m"
echo -e "Access your setup wizard at: \033[1;36mhttp://$(hostname -I | awk '{print $1}'):8000\033[0m\n"



