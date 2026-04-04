#!/bin/bash
set -e

# Run migrations
python manage.py migrate --no-input

# Collect static files
python manage.py collectstatic --no-input

# Create initial admin user from env var (idempotent)
python manage.py create_initial_admin

# Schedule cache refresh every minute via cron
# (cron doesn't inherit Docker env vars, so we export them explicitly)
{
    printenv | grep -E '^(DJANGO_DB_PATH|DJANGO_SECRET_KEY|DATABASE_PATH|HUDI_TABLE_PATH|PERSISTENCE_BACKEND)='
    echo '*/5 * * * * flock -n /tmp/refresh_cache.lock -c "cd /app && /usr/local/bin/python3 manage.py refresh_cache >> /var/log/refresh_cache.log 2>&1"'
} | crontab -
cron

# Start gunicorn
exec gunicorn jerrycan_web.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers "${GUNICORN_WORKERS:-3}" \
    --timeout "${GUNICORN_TIMEOUT:-120}" \
    --access-logfile - \
    --error-logfile -
