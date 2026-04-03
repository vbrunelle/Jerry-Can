#!/usr/bin/env bash
# stop.sh — Stop and remove jerry-can containers.

set -euo pipefail

cd "$(dirname "$0")"

echo "==> Stopping jerry-can containers..."
docker compose down --remove-orphans
echo "==> Done."
