FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends cron && rm -rf /var/lib/apt/lists/*

COPY management_server/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY management_server/manage.py ./
COPY management_server/jerrycan_web/ jerrycan_web/
COPY management_server/accounts/ accounts/
COPY management_server/dashboard/ dashboard/

# Shared price_history module (single source of truth from src/)
COPY src/price_history.py ./

COPY management_server/entrypoint.sh ./
RUN chmod +x entrypoint.sh

VOLUME ["/data"]

ENV DJANGO_SETTINGS_MODULE=jerrycan_web.settings
ENV PYTHONUNBUFFERED=1
ENV DJANGO_DB_PATH=/data/django_db.sqlite3
ENV DATABASE_PATH=/data/fuel_prices.db

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
