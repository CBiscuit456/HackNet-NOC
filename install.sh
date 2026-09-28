#!/bin/bash

# HackNet NOC - Automated Deployment Script
# Run with: sudo ./install.sh

echo -e "\n\033[1;32m====================================\033[0m"
echo -e "\033[1;32m  HACKNET NOC INITIALIZATION SCRIPT \033[0m"
echo -e "\033[1;32m====================================\033[0m\n"

# 1. Check for root privileges
if [ "$EUID" -ne 0 ]; then 
  echo -e "\033[1;31mPlease run this script as root (sudo ./install.sh)\033[0m"
  exit
fi

USER_DIR=$(pwd)
APP_USER=$SUDO_USER
if [ -z "$APP_USER" ]; then APP_USER=$(whoami); fi

echo -e "\033[1;36m[1/5] Installing OS Dependencies...\033[0m"
apt update -y
apt install python3-venv python3-pip sqlite3 ufw -y

echo -e "\033[1;36m[2/5] Building Python Environment...\033[0m"
sudo -u $APP_USER python3 -m venv .hacknet_env
sudo -u $APP_USER .hacknet_env/bin/pip install -r requirements.txt

echo -e "\033[1;36m[3/5] Configuring .env file...\033[0m"
if [ ! -f .env ]; then
    sudo -u $APP_USER cat << 'EOF' > .env
# HackNet NOC Secrets
PROXMOX_IP=10.10.x.x
PROXMOX_TOKEN_ID=root@pam!your_token_name
PROXMOX_TOKEN_SECRET=your_proxmox_api_secret_here
PTERO_URL=http://10.10.x.x
PTERO_TOKEN=your_pterodactyl_client_api_key_here
TELEGRAM_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_telegram_chat_id_here
PIHOLE_IP=
PIHOLE_APP_PASSWORD=
ADMIN_SECRET_KEY=super_secret_password_change_me
EOF
    echo -e "   -> Created template .env file."
else
    echo -e "   -> .env file already exists. Skipping."
fi

echo -e "\033[1;36m[4/5] Creating Systemd Background Services...\033[0m"
cat << EOF > /etc/systemd/system/hacknet-api.service
[Unit]
Description=HackNet API
After=network.target

[Service]
User=$APP_USER
WorkingDirectory=$USER_DIR
ExecStart=$USER_DIR/.hacknet_env/bin/uvicorn api:app --host 0.0.0.0 --port 8000
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
WorkingDirectory=$USER_DIR
ExecStart=$USER_DIR/.hacknet_env/bin/python collector.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable hacknet-api hacknet-collector

echo -e "\033[1;36m[5/5] Configuring UFW Firewall...\033[0m"
ufw allow ssh
ufw allow 8000/tcp
ufw --force enable

echo -e "\n\033[1;32m====================================\033[0m"
echo -e "\033[1;32m DEPLOYMENT COMPLETE! \033[0m"
echo -e "\033[1;32m====================================\033[0m"
echo -e "1. Edit your \033[1;33m.env\033[0m file to add your API keys."
echo -e "2. Run \033[1;33msudo systemctl start hacknet-api hacknet-collector\033[0m"
echo -e "3. Access your dashboard at: \033[1;36mhttp://$(hostname -I | awk '{print $1}'):8000\033[0m\n"