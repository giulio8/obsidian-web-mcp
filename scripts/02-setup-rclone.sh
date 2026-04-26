#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# ISTRUZIONI (DA ESEGUIRE SULLA VM):
# 1. Connettiti alla VM via SSH (`gcloud compute ssh obsidian-mcp-vm`)
# 2. Clona questo repository o copia solo questi script
# 3. Imposta le tue chiavi Cloudflare R2 nell'ambiente:
#
# export R2_ACCESS_KEY_ID="tuo-access-key"
# export R2_SECRET_ACCESS_KEY="tuo-secret-key"
# export R2_ACCOUNT_ID="tuo-account-id"
# export R2_BUCKET_NAME="tuo-bucket"
#
# (In alternativa puoi modificare questo script inserendole qui sotto)
# ==============================================================================

if [ -z "${R2_ACCESS_KEY_ID:-}" ] || [ -z "${R2_SECRET_ACCESS_KEY:-}" ] || [ -z "${R2_ACCOUNT_ID:-}" ]; then
  echo "ERRORE: Le variabili R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ACCOUNT_ID devono essere impostate."
  exit 1
fi

BUCKET_NAME="${R2_BUCKET_NAME:-obsidian-vault-bucket}"

echo "=== Setup Rclone per Cloudflare R2 ==="

# 1. Installazione Rclone
if ! command -v rclone &> /dev/null; then
    echo "Installazione di Rclone in corso..."
    curl https://rclone.org/install.sh | sudo bash
else
    echo "Rclone è già installato."
fi

# 2. Configurazione Rclone
RCLONE_CONFIG_DIR="$HOME/.config/rclone"
mkdir -p "$RCLONE_CONFIG_DIR"

RCLONE_CONF="$RCLONE_CONFIG_DIR/rclone.conf"

echo "Creazione configurazione in $RCLONE_CONF..."
cat <<EOF > "$RCLONE_CONF"
[obsidian-r2]
type = s3
provider = Cloudflare
access_key_id = $R2_ACCESS_KEY_ID
secret_access_key = $R2_SECRET_ACCESS_KEY
endpoint = https://$R2_ACCOUNT_ID.r2.cloudflarestorage.com
acl = private
EOF

# 3. Creazione cartella locale
MOUNT_DIR="/mnt/obsidian-vault"
echo "Creazione directory di mount $MOUNT_DIR..."
sudo mkdir -p "$MOUNT_DIR"
sudo chown -R $USER:$USER "$MOUNT_DIR"

echo "Setup base rclone completato."
echo "Prova a testare l'accesso con:"
echo "rclone lsd obsidian-r2:"
