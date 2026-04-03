#!/usr/bin/env bash
# dump.sh — Exporte l'historique complet des changements de prix en CSV.
#
# Le script se connecte au conteneur Docker Jerry-Can et exécute dump_csv.py.
# Le fichier CSV est écrit dans /data/ (volume monté), donc accessible sur l'hôte.
#
# Usage:
#   ./dump.sh                          # écrit ./data/dump_prix.csv
#   ./dump.sh /data/custom_name.csv    # chemin custom dans le conteneur

set -euo pipefail

CONTAINER="jerry-can-jerry-can-1"
OUTPUT="${1:-}"

if ! docker inspect "$CONTAINER" &>/dev/null; then
    echo "Erreur : le conteneur '$CONTAINER' n'existe pas ou n'est pas démarré."
    echo "Lance d'abord ./rebuild-run.sh"
    exit 1
fi

CMD="python dump_csv.py"
if [[ -n "$OUTPUT" ]]; then
    CMD="python dump_csv.py $OUTPUT"
fi

docker exec "$CONTAINER" bash -c "$CMD"

# Resolve local path for the user message
if [[ -f .env ]]; then
    DATA_DIR=$(grep -E '^DATA_DIR=' .env 2>/dev/null | cut -d= -f2- | tr -d '"'"'" | xargs)
fi
DATA_DIR="${DATA_DIR:-./data}"

LOCAL_FILE="$DATA_DIR/dump_prix.csv"
if [[ -n "$OUTPUT" ]]; then
    # /data/foo.csv → $DATA_DIR/foo.csv
    LOCAL_FILE="$DATA_DIR/${OUTPUT#/data/}"
fi

if [[ -f "$LOCAL_FILE" ]]; then
    echo ""
    echo "Fichier disponible sur l'hôte : $LOCAL_FILE"
fi
