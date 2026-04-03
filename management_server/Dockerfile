FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY manage.py ./
COPY jerrycan_web/ jerrycan_web/
COPY accounts/ accounts/
COPY dashboard/ dashboard/

COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

VOLUME ["/data"]

ENV DJANGO_SETTINGS_MODULE=jerrycan_web.settings
ENV PYTHONUNBUFFERED=1
ENV DJANGO_DB_PATH=/data/django_db.sqlite3
ENV DATABASE_PATH=/data/fuel_prices.db

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
