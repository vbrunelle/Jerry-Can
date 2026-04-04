#!/bin/bash
set -e

# Run migrations
python manage.py migrate --no-input

# Collect static files
python manage.py collectstatic --no-input

# Create initial admin user from env var (idempotent)
python manage.py create_initial_admin

# Start gunicorn
# Cache refresh (every 15 min) and download cleanup (every hour) run
# as in-process background threads via DashboardConfig.ready().
exec gunicorn jerrycan_web.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers "${GUNICORN_WORKERS:-3}" \
    --timeout "${GUNICORN_TIMEOUT:-120}" \
    --access-logfile - \
    --error-logfile -
