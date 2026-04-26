#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# ISTRUZIONI (DA ESEGUIRE SULLA VM CON SUDO):
# Questo script garantisce che sia Rclone (montaggio R2) sia il Server MCP
# si avviino automaticamente ad ogni accensione o riavvio della VM in background.
# ==============================================================================

if [ "$EUID" -ne 0 ]; then
  echo "ERRORE: Esegui questo script come root, es: sudo ./03-setup-systemd.sh"
  exit 1
fi

APP_USER=${SUDO_USER:-$USER}
APP_GROUP=$(id -gn $APP_USER)
HOME_DIR=$(eval echo ~$APP_USER)

MOUNT_DIR="/mnt/obsidian-vault"
BUCKET_NAME="${R2_BUCKET_NAME:-obsidian-vault-bucket}"
MCP_DIR="$HOME_DIR/obsidian-web-mcp" # Assumendo che ci sia la cartella del progetto

echo "=== Creazione e abilitazione Servizi Systemd ==="

# Abilita fuse user_allow_other per permettere al server di leggere il mount
if [ -f /etc/fuse.conf ]; then
    sed -i 's/#user_allow_other/user_allow_other/' /etc/fuse.conf
    grep -q "user_allow_other" /etc/fuse.conf || echo "user_allow_other" >> /etc/fuse.conf
fi

# 1. Servizio Rclone
RCLONE_SERVICE="/etc/systemd/system/rclone-obsidian.service"
echo "Creazione $RCLONE_SERVICE..."
cat <<EOF > "$RCLONE_SERVICE"
[Unit]
Description=Rclone Mount for Obsidian R2 Vault
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
User=$APP_USER
Group=$APP_GROUP
ExecStart=/usr/bin/rclone mount obsidian-r2:$BUCKET_NAME $MOUNT_DIR \\
  --vfs-cache-mode full \\
  --dir-cache-time 30m \\
  --vfs-cache-max-age 24h \\
  --vfs-cache-max-size 5G \\
  --allow-other \\
  --log-level INFO
ExecStop=/bin/fusermount -uz $MOUNT_DIR
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# 2. Servizio Obsidian MCP
MCP_SERVICE="/etc/systemd/system/obsidian-mcp.service"
echo "Creazione $MCP_SERVICE..."
cat <<EOF > "$MCP_SERVICE"
[Unit]
Description=Obsidian MCP Server
After=network.target rclone-obsidian.service
Requires=rclone-obsidian.service

[Service]
Type=simple
User=$APP_USER
Group=$APP_GROUP
WorkingDirectory=$MCP_DIR
# Eseguiamo il server node/python sulla porta 8420 e puntandolo alla cartella di Rclone
ExecStart=/usr/bin/env npm start
Environment=PORT=8420
Environment=OBSIDIAN_DIR=$MOUNT_DIR
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

echo "Ricaricamento daemon systemd..."
systemctl daemon-reload

echo "Abilitazione servizi all'avvio..."
systemctl enable rclone-obsidian.service
systemctl enable obsidian-mcp.service

echo "=== Setup Systemd completato ==="
echo "Per avviarli usa:"
echo "sudo systemctl start rclone-obsidian.service"
echo "sudo systemctl start obsidian-mcp.service"
