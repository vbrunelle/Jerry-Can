#!/usr/bin/env bash
# inspect-docker.sh — Inspecte le contenu de la base de données dans le conteneur Docker.
#
# Usage:
#   ./inspect-docker.sh                   # lit le backend configuré dans .env
#   ./inspect-docker.sh sqlite            # force SQLite
#   ./inspect-docker.sh hudi              # force Hudi
#   ./inspect-docker.sh sqlite /chemin/vers/fuel_prices.db   # chemin custom SQLite
#   ./inspect-docker.sh hudi /chemin/vers/hudi               # chemin custom Hudi

set -euo pipefail

CONTAINER="jerry-can-jerry-can-1"
BACKEND="${1:-}"
TARGET="${2:-}"

if ! docker inspect "$CONTAINER" &>/dev/null; then
    echo "Erreur : le conteneur '$CONTAINER' n'existe pas ou n'est pas démarré."
    echo "Lance d'abord ./rebuild-run.sh"
    exit 1
fi

CMD="python show.py"
if [[ -n "$TARGET" ]]; then
    CMD="python show.py $TARGET"
fi

if [[ -n "$BACKEND" ]]; then
    docker exec -e PERSISTENCE_BACKEND="$BACKEND" "$CONTAINER" bash -c "$CMD"
else
    docker exec "$CONTAINER" bash -c "$CMD"
fi
