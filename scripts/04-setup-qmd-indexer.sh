#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# 04-setup-qmd-indexer.sh
# Installa il servizio systemd + timer per l'indicizzazione incrementale QMD.
#
# Eseguire sulla VM come utente normale (non root):
#   bash ~/obsidian-web-mcp/scripts/04-setup-qmd-indexer.sh
#
# Crea:
#   ~/.config/systemd/user/qmd-indexer.service  — esegue qmd-index (delta)
#   ~/.config/systemd/user/qmd-indexer.timer    — ogni 15 minuti
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

ENV_FILE=""
for p in "${REPO_DIR}/.env" "${HOME}/obsidian-web-mcp/.env" "${HOME}/.env"; do
    if [ -f "$p" ]; then
        ENV_FILE="$p"
        break
    fi
done

if [ -z "$ENV_FILE" ]; then
    echo "ATTENZIONE: Nessun file .env trovato."
fi

VAULT_PATH="${VAULT_PATH:-/mnt/obsidian-vault}"
UV_BIN="$(which uv 2>/dev/null || echo "${HOME}/.local/bin/uv")"

echo "=== Setup QMD Incremental Indexer ==="
echo "  repo    : ${REPO_DIR}"
echo "  vault   : ${VAULT_PATH}"
echo "  env     : ${ENV_FILE:-<none>}"
echo "  uv      : ${UV_BIN}"
echo ""

# ── Cartella systemd utente ────────────────────────────────────────────────────
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
mkdir -p "${SYSTEMD_USER_DIR}"

# ── qmd-indexer.service ────────────────────────────────────────────────────────
cat > "${SYSTEMD_USER_DIR}/qmd-indexer.service" <<EOF
[Unit]
Description=QMD-Lite Incremental Vault Indexer
# Aspetta che il mount rclone sia attivo prima di indicizzare
After=rclone-obsidian.service
# Non fallire se rclone non è un'unità utente (nel caso fosse system)
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=${REPO_DIR}
ExecStartPre=/bin/sh -c 'test -d ${VAULT_PATH} && ls ${VAULT_PATH} >/dev/null 2>&1 || { echo "Vault not mounted, skipping."; exit 1; }'
ExecStart=${UV_BIN} run --project ${REPO_DIR} qmd-index --vault ${VAULT_PATH}
EOF

# Inietta le env var se abbiamo il file .env
if [ -n "$ENV_FILE" ]; then
    echo "EnvironmentFile=${ENV_FILE}" >> "${SYSTEMD_USER_DIR}/qmd-indexer.service"
fi

cat >> "${SYSTEMD_USER_DIR}/qmd-indexer.service" <<EOF

# Non ritentare indefinitamente se il vault è giù
Restart=no

# Log a journald come gli altri servizi
StandardOutput=journal
StandardError=journal
SyslogIdentifier=qmd-indexer

[Install]
WantedBy=default.target
EOF

# ── qmd-indexer.timer ─────────────────────────────────────────────────────────
cat > "${SYSTEMD_USER_DIR}/qmd-indexer.timer" <<EOF
[Unit]
Description=QMD-Lite Indexer — ogni 15 minuti
Requires=qmd-indexer.service

[Timer]
# Prima esecuzione: 2 minuti dopo il boot (dà tempo a rclone di montare)
OnBootSec=2min
# Poi ogni 15 minuti
OnUnitActiveSec=15min
# Se il sistema era spento al momento previsto, esegui subito
Persistent=true

[Install]
WantedBy=timers.target
EOF

echo "File scritti:"
echo "  ${SYSTEMD_USER_DIR}/qmd-indexer.service"
echo "  ${SYSTEMD_USER_DIR}/qmd-indexer.timer"
echo ""

# ── Prima indicizzazione completa ─────────────────────────────────────────────
echo "=== Indicizzazione iniziale completa ==="
if [ -d "${VAULT_PATH}" ] && ls "${VAULT_PATH}" >/dev/null 2>&1; then
    set -a
    [ -n "$ENV_FILE" ] && source <(cat "$ENV_FILE" | tr -d '\r')
    set +a
    "${UV_BIN}" run --project "${REPO_DIR}" qmd-index --full --vault "${VAULT_PATH}"
else
    echo "ATTENZIONE: ${VAULT_PATH} non montato. Salta indicizzazione iniziale."
    echo "Esegui manualmente: uv run qmd-index --full --vault ${VAULT_PATH}"
fi

# ── Abilita e avvia il timer ───────────────────────────────────────────────────
echo ""
echo "=== Attivazione timer ==="
systemctl --user daemon-reload
systemctl --user enable --now qmd-indexer.timer

echo ""
echo "=== Stato timer ==="
systemctl --user list-timers qmd-indexer.timer --no-pager

echo ""
echo "Setup completato. Il vault sarà re-indicizzato ogni 15 minuti."
echo "Comandi utili:"
echo "  systemctl --user status qmd-indexer.service   # ultimo run"
echo "  systemctl --user list-timers                  # prossimo run"
echo "  journalctl --user -u qmd-indexer -n 30        # log recenti"
