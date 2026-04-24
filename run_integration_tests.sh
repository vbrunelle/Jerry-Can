#!/usr/bin/env bash
# run_integration_tests.sh
#
# Runs integration tests against a live Docker Compose stack.
# Reads credentials from .env.test, applies the password to the test
# user inside the container, then runs the tests.
#
# Usage:
#   ./run_integration_tests.sh
#   ./run_integration_tests.sh prices.tests.test_large_scale.AnalysisDetailPageLoadTest
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env.test"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: $ENV_FILE not found. Create it from .env.test (it already exists)." >&2
    exit 1
fi

# Load .env.test into the current shell (strip Windows CRLF line endings)
set -a
# shellcheck disable=SC1090
source <(sed 's/\r//' "$ENV_FILE")
set +a

# Derive the Docker Compose project name from the container name.
# Convention: jerry-can-second-web-1 → project jerry-can-second
#             jerry-can-web-1        → project jerry-can
# Strip the trailing "-web-1" (or any "-<service>-<index>") suffix.
COMPOSE_PROJECT=$(echo "$JERRYCAN_TEST_CONTAINER" | sed 's/-[^-]*-[0-9]*$//')

# Determine the compose file to use.
if [[ "$JERRYCAN_TEST_CONTAINER" == *"second"* ]]; then
    COMPOSE_FILE="$SCRIPT_DIR/docker-compose.second-instance.yml"
else
    COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
fi

echo "==> Building web image from local files (project: $COMPOSE_PROJECT)..."
docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" build web

echo "==> Restarting web container with the freshly built image..."
docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" up -d --no-deps web

echo "==> Waiting for web container to be ready..."
for i in $(seq 1 30); do
    if docker exec "$JERRYCAN_TEST_CONTAINER" python -c "import django" 2>/dev/null; then
        break
    fi
    sleep 2
done

echo "==> Resetting password for '$JERRYCAN_TEST_USERNAME' in container '$JERRYCAN_TEST_CONTAINER'..."
docker exec "$JERRYCAN_TEST_CONTAINER" python manage.py shell -c "
from django.contrib.auth.models import User
u = User.objects.get(username='${JERRYCAN_TEST_USERNAME}')
u.set_password('${JERRYCAN_TEST_PASSWORD}')
u.save()
print('Password updated for', u.username)
"

echo "==> Running integration tests against $JERRYCAN_TEST_URL ..."
cd "$SCRIPT_DIR/jerrycan"

TEST_MODULE="${1:-prices.tests.test_large_scale.AnalysisDetailPageLoadTest}"

python manage.py test "$TEST_MODULE" --verbosity=2
