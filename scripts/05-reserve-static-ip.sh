#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# 05-reserve-static-ip.sh  (eseguire in LOCALE, non sulla VM)
#
# Riserva un IP esterno statico GCP e lo assegna alla VM.
# Aggiorna le regole firewall: apre 80/443 per Caddy, chiude 8420 pubblico.
#
# Uso:
#   bash scripts/05-reserve-static-ip.sh
#
# Prerequisiti: gcloud CLI autenticato, VM in us-east1-b
# ==============================================================================

VM_NAME="obsidian-mcp-vm"
ZONE="us-east1-b"
REGION="us-east1"
IP_NAME="obsidian-mcp-ip"
NETWORK_TAG="obsidian-mcp"

echo "=== 1. Riservo IP statico in ${REGION} ==="
if gcloud compute addresses describe "${IP_NAME}" --region="${REGION}" &>/dev/null; then
    echo "IP '${IP_NAME}' già esistente, skip."
else
    gcloud compute addresses create "${IP_NAME}" \
        --region="${REGION}" \
        --description="Static IP for obsidian-mcp-vm"
    echo "IP statico creato."
fi

STATIC_IP=$(gcloud compute addresses describe "${IP_NAME}" \
    --region="${REGION}" --format='value(address)')
echo "IP statico: ${STATIC_IP}"

echo ""
echo "=== 2. Assegno IP statico alla VM ==="
CURRENT_NAT=$(gcloud compute instances describe "${VM_NAME}" \
    --zone="${ZONE}" \
    --format='value(networkInterfaces[0].accessConfigs[0].name)' 2>/dev/null || echo "")

if [ -n "${CURRENT_NAT}" ]; then
    echo "Rimuovo access config esistente: '${CURRENT_NAT}'"
    gcloud compute instances delete-access-config "${VM_NAME}" \
        --zone="${ZONE}" \
        --access-config-name="${CURRENT_NAT}"
fi

gcloud compute instances add-access-config "${VM_NAME}" \
    --zone="${ZONE}" \
    --access-config-name="Static NAT" \
    --address="${STATIC_IP}"
echo "IP statico assegnato."

echo ""
echo "=== 3. Regole firewall ==="

# Apri 80 e 443 per Caddy (da qualsiasi IP)
for PORT_NAME in "allow-http" "allow-https"; do
    PORT=$([ "${PORT_NAME}" = "allow-http" ] && echo "tcp:80" || echo "tcp:443")
    RULE_NAME="${NETWORK_TAG}-${PORT_NAME}"
    if gcloud compute firewall-rules describe "${RULE_NAME}" &>/dev/null; then
        echo "Regola ${RULE_NAME} già esistente, skip."
    else
        gcloud compute firewall-rules create "${RULE_NAME}" \
            --direction=INGRESS \
            --priority=1000 \
            --network=default \
            --action=ALLOW \
            --rules="${PORT}" \
            --source-ranges=0.0.0.0/0 \
            --target-tags="${NETWORK_TAG}"
        echo "Creata regola: ${RULE_NAME}"
    fi
done

# Rimuovi eventuale regola che espone porta 8420 pubblicamente
# (il MCP server ascolterà solo su 127.0.0.1 dopo questo setup)
for OLD_RULE in "obsidian-mcp-allow-mcp" "allow-mcp-8420"; do
    if gcloud compute firewall-rules describe "${OLD_RULE}" &>/dev/null; then
        echo "Rimuovo regola pubblica ${OLD_RULE}..."
        gcloud compute firewall-rules delete "${OLD_RULE}" --quiet
    fi
done

# Assicura che la VM abbia il network tag corretto
gcloud compute instances add-tags "${VM_NAME}" \
    --zone="${ZONE}" \
    --tags="${NETWORK_TAG}" 2>/dev/null || true

echo ""
echo "=== ✅ Completato ==="
echo ""
echo "  IP statico: ${STATIC_IP}"
echo ""
echo "Prossimi passi:"
echo "  1. Vai su https://www.duckdns.org"
echo "  2. Crea un subdomain (es. 'obsidian-mcp') e imposta IP = ${STATIC_IP}"
echo "  3. Copia il tuo DuckDNS TOKEN dalla dashboard"
echo "  4. Sulla VM, esegui: bash ~/obsidian-web-mcp/scripts/06-setup-caddy.sh"
