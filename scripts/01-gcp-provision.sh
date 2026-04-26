#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# ALTERNATIVA VIA CONSOLE WEB (GCP Console):
# 1. Vai su Compute Engine -> Istanze VM -> "Crea istanza"
# 2. Nome: obsidian-mcp-vm, Area: us-east1, Zona: us-east1-b (Always Free Tier)
# 3. Configurazione macchina: Serie E2 -> e2-micro
# 4. Disco di avvio (cambia): Debian GNU/Linux 12, "Disco standard persistente", 30 GB
# 5. Opzioni avanzate -> Rete -> Tag di rete: scrivi "mcp-server" (premi invio)
# 6. Clicca "Crea" a fondo pagina.
#
# 7. Vai su Rete VPC -> Firewall -> "Crea regola firewall"
# 8. Nome: allow-mcp-8420, Direzione: In entrata
# 9. Target -> Tag di target: scrivi "mcp-server" (premi invio)
# 10. Intervalli IPv4 sorgente: 0.0.0.0/0
# 11. Protocolli e porte -> TCP: spunta ed inserisci 8420.
# 12. Clicca "Crea".
# ==============================================================================

echo "=== Creazione Infrastruttura GCP per Obsidian MCP ==="
PROJECT_ID=$(gcloud config get-value project 2>/dev/null || echo "")

if [ -z "$PROJECT_ID" ]; then
    echo "Errore: Progetto Google Cloud non configurato."
    echo "Esegui prima: gcloud init"
    exit 1
fi

ZONE="us-east1-b"
INSTANCE_NAME="obsidian-mcp-vm"
PORT=8420
NETWORK_TAG="mcp-server"

echo "Creazione VM $INSTANCE_NAME in progetto $PROJECT_ID ($ZONE)..."
gcloud compute instances create "$INSTANCE_NAME" \
    --project="$PROJECT_ID" \
    --zone="$ZONE" \
    --machine-type=e2-micro \
    --network-interface=network-tier=PREMIUM,subnet=default \
    --maintenance-policy=MIGRATE \
    --tags="$NETWORK_TAG" \
    --create-disk=auto-delete=yes,boot=yes,device-name="$INSTANCE_NAME",image=projects/debian-cloud/global/images/family/debian-12,mode=rw,size=30,type=pd-standard || echo "La VM potrebbe già esistere o c'è un errore."

echo ""
echo "Creazione regola firewall per porta TCP $PORT..."
gcloud compute firewall-rules create allow-mcp-$PORT \
    --project="$PROJECT_ID" \
    --direction=INGRESS \
    --priority=1000 \
    --network=default \
    --action=ALLOW \
    --rules=tcp:$PORT \
    --source-ranges=0.0.0.0/0 \
    --target-tags="$NETWORK_TAG" || echo "La regola firewall potrebbe già esistere."

echo ""
echo "=== Setup completato ==="
echo "Puoi connetterti alla VM tramite SSH eseguendo:"
echo "  gcloud compute ssh $INSTANCE_NAME --zone=$ZONE"
echo ""
echo "L'IP Pubblico della VM è:"
gcloud compute instances describe "$INSTANCE_NAME" \
  --zone="$ZONE" \
  --format='get(networkInterfaces[0].accessConfigs[0].natIP)'
