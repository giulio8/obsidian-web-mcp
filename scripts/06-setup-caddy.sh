#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# 06-setup-caddy.sh  (eseguire SULLA VM)
#
# Installa Caddy come reverse proxy HTTPS davanti al server MCP.
# Richiede:
#   - Variabili DUCKDNS_SUBDOMAIN e DUCKDNS_TOKEN (chieste interattivamente
#     o già in .env)
#   - Porta 80 e 443 aperte nel firewall GCP (fatto da 05-reserve-static-ip.sh)
#
# Uso:
#   bash ~/obsidian-web-mcp/scripts/06-setup-caddy.sh
#
# Oppure con variabili già esportate:
#   DUCKDNS_SUBDOMAIN=obsidian-mcp DUCKDNS_TOKEN=xxxx bash 06-setup-caddy.sh
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${REPO_DIR}/.env"

# Carica .env se esiste
if [ -f "${ENV_FILE}" ]; then
    set -a
    source <(grep -v '^#' "${ENV_FILE}" | tr -d '\r')
    set +a
fi

# Chiedi le variabili mancanti
if [ -z "${DUCKDNS_SUBDOMAIN:-}" ]; then
    read -rp "Subdomain DuckDNS (es. 'obsidian-mcp' → obsidian-mcp.duckdns.org): " DUCKDNS_SUBDOMAIN
fi
if [ -z "${DUCKDNS_TOKEN:-}" ]; then
    read -rp "DuckDNS Token (dalla dashboard duckdns.org): " DUCKDNS_TOKEN
fi

MCP_PORT="${VAULT_MCP_PORT:-8420}"
HOSTNAME="${DUCKDNS_SUBDOMAIN}.duckdns.org"

echo ""
echo "=== Setup Caddy HTTPS Reverse Proxy ==="
echo "  hostname : ${HOSTNAME}"
echo "  upstream : 127.0.0.1:${MCP_PORT}"
echo ""

# ── 1. Ferma eventuali web server che occupano la porta 80 ────────────────────
echo "=== 1. Libero la porta 80 ==="
for SVC in apache2 nginx lighttpd; do
    if systemctl is-active --quiet "${SVC}" 2>/dev/null; then
        echo "Fermo ${SVC}..."
        sudo systemctl stop "${SVC}"
        sudo systemctl disable "${SVC}"
        echo "${SVC} fermato e disabilitato."
    fi
done

# ── 2. Installa Caddy ─────────────────────────────────────────────────────────
echo ""
echo "=== 2. Installa Caddy ==="
if command -v caddy &>/dev/null; then
    echo "Caddy già installato: $(caddy version)"
else
    sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        | sudo tee /etc/apt/sources.list.d/caddy-stable.list
    sudo apt-get update -q
    sudo apt-get install -y caddy
    echo "Caddy installato: $(caddy version)"
fi

# ── 2. Caddyfile ──────────────────────────────────────────────────────────────
echo ""
echo "=== 2. Configuro Caddyfile ==="
sudo tee /etc/caddy/Caddyfile > /dev/null <<EOF
# Obsidian MCP — HTTPS reverse proxy
# Caddy gestisce automaticamente il certificato Let's Encrypt

${HOSTNAME} {
    # Forwarding al server MCP locale
    reverse_proxy 127.0.0.1:${MCP_PORT}

    # Log compatti
    log {
        output file /var/log/caddy/obsidian-mcp.log {
            roll_size 10mb
            roll_keep 5
        }
        format console
        level WARN
    }

    # Header di sicurezza
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options nosniff
        X-Frame-Options DENY
    }
}
EOF

sudo mkdir -p /var/log/caddy
sudo chown caddy:caddy /var/log/caddy

echo "Caddyfile scritto in /etc/caddy/Caddyfile"

# ── 3. DuckDNS updater (cron ogni 5 minuti) ───────────────────────────────────
echo ""
echo "=== 3. DuckDNS auto-update ==="
DUCKDNS_SCRIPT="/usr/local/bin/duckdns-update.sh"
sudo tee "${DUCKDNS_SCRIPT}" > /dev/null <<EOF
#!/bin/bash
# Aggiorna DuckDNS con l'IP corrente della macchina
curl -s -o /var/log/duckdns.log \
  "https://www.duckdns.org/update?domains=${DUCKDNS_SUBDOMAIN}&token=${DUCKDNS_TOKEN}&ip="
EOF
sudo chmod +x "${DUCKDNS_SCRIPT}"

# Cron ogni 5 minuti
CRON_LINE="*/5 * * * * root ${DUCKDNS_SCRIPT}"
if grep -qF "duckdns-update" /etc/crontab 2>/dev/null; then
    echo "Cron DuckDNS già presente, skip."
else
    echo "${CRON_LINE}" | sudo tee -a /etc/crontab > /dev/null
    echo "Cron DuckDNS aggiunto (ogni 5 minuti)."
fi

# Prima esecuzione immediata
echo "Aggiorno DuckDNS subito..."
sudo bash "${DUCKDNS_SCRIPT}"
sleep 1
RESULT=$(cat /var/log/duckdns.log 2>/dev/null || echo "?")
echo "Risposta DuckDNS: ${RESULT}"
if [ "${RESULT}" != "OK" ]; then
    echo "ATTENZIONE: DuckDNS non ha risposto 'OK'. Controlla subdomain e token."
fi

# ── 4. Aggiorna .env con il nuovo hostname ────────────────────────────────────
echo ""
echo "=== 4. Aggiorno .env ==="
if grep -q "^DUCKDNS_SUBDOMAIN=" "${ENV_FILE}" 2>/dev/null; then
    sed -i "s|^DUCKDNS_SUBDOMAIN=.*|DUCKDNS_SUBDOMAIN=${DUCKDNS_SUBDOMAIN}|" "${ENV_FILE}"
else
    echo "DUCKDNS_SUBDOMAIN=${DUCKDNS_SUBDOMAIN}" >> "${ENV_FILE}"
fi
if grep -q "^DUCKDNS_TOKEN=" "${ENV_FILE}" 2>/dev/null; then
    sed -i "s|^DUCKDNS_TOKEN=.*|DUCKDNS_TOKEN=${DUCKDNS_TOKEN}|" "${ENV_FILE}"
else
    echo "DUCKDNS_TOKEN=${DUCKDNS_TOKEN}" >> "${ENV_FILE}"
fi
if grep -q "^VAULT_MCP_HOSTNAME=" "${ENV_FILE}" 2>/dev/null; then
    sed -i "s|^VAULT_MCP_HOSTNAME=.*|VAULT_MCP_HOSTNAME=${HOSTNAME}|" "${ENV_FILE}"
else
    echo "VAULT_MCP_HOSTNAME=${HOSTNAME}" >> "${ENV_FILE}"
fi
echo "Variabili aggiornate in ${ENV_FILE}"

# ── 5. MCP server: bind su 127.0.0.1 ─────────────────────────────────────────
echo ""
echo "=== 5. Restringo MCP server a localhost ==="
# Aggiorna host in server.py (0.0.0.0 → 127.0.0.1)
SERVER_PY="${REPO_DIR}/src/obsidian_vault_mcp/server.py"
if grep -q '"host": "0.0.0.0"' "${SERVER_PY}" 2>/dev/null || grep -q 'host="0.0.0.0"' "${SERVER_PY}" 2>/dev/null; then
    sed -i 's/host="0\.0\.0\.0"/host="127.0.0.1"/g' "${SERVER_PY}"
    echo "server.py: host aggiornato a 127.0.0.1"
else
    echo "server.py: nessuna occorrenza di '0.0.0.0' trovata (già corretto o da verificare manualmente)"
fi

# ── 6. Avvia e abilita Caddy ──────────────────────────────────────────────────
echo ""
echo "=== 6. Avvia Caddy ==="
sudo systemctl enable caddy
sudo systemctl restart caddy
sleep 3
sudo systemctl status caddy --no-pager | head -10

# Riavvia anche il server MCP per applicare il bind localhost
echo ""
echo "Riavvio server MCP..."
sudo systemctl restart obsidian-mcp.service 2>/dev/null || true

echo ""
echo "=== ✅ Setup completato ==="
echo ""
echo "  URL pubblico : https://${HOSTNAME}"
echo "  Cert HTTPS   : gestito automaticamente da Caddy (Let's Encrypt)"
echo "  MCP server   : 127.0.0.1:${MCP_PORT} (non più esposto pubblicamente)"
echo ""
echo "Test connettività:"
echo "  curl -s https://${HOSTNAME}/health || echo 'In attesa del cert (max 30s)'"
echo ""
echo "Aggiorna il tuo MCP config con:"
echo "  \"url\": \"https://${HOSTNAME}/mcp\""
