#!/usr/bin/env bash
# rebuild-run.sh — Stop jerry-can containers, rebuild the image, start detached,
# then follow logs. Ctrl+C exits log following but leaves the container running.

set -euo pipefail

cd "$(dirname "$0")"

# Charger DATA_DIR depuis .env si présent
if [[ -f .env ]]; then
    DATA_DIR=$(grep -E '^DATA_DIR=' .env | cut -d= -f2- | tr -d '"'"'" | xargs)
fi
DATA_DIR="${DATA_DIR:-./data}"
mkdir -p "$DATA_DIR"

echo "==> Stopping existing jerry-can containers..."
docker compose down --remove-orphans || true

echo "==> Building image..."
docker compose build

echo "==> Starting container (detached)..."
docker compose up -d

echo "==> Container started. Following logs (Ctrl+C to stop watching, container keeps running)..."
trap '' INT
docker compose logs -f jerry-can
