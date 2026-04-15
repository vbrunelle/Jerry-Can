#!/bin/bash
set -e

echo "Applying database migrations..."
python manage.py migrate --noinput

echo "Creating initial admin user (if needed)..."
python manage.py create_initial_admin

echo "Starting Gunicorn..."
exec gunicorn jerrycan.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers 1 \
    --threads 4 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile -
