FROM python:3.12-slim

WORKDIR /app

# Install Java 21 (required by PySpark / Hudi). Remove apt package lists
# in the same layer to reduce image size by avoiding leftover apt caches.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openjdk-21-jre-headless \
    && rm -rf /var/lib/apt/lists/*
ENV JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY tests/ tests/
COPY pytest.ini .
COPY inspect_db.py .
COPY show.py .
COPY dump_csv.py .
COPY backfill_history.py .
COPY main.py .

VOLUME ["/data"]

ENV DATABASE_PATH=/data/fuel_prices.db
# Hudi is the default persistence backend; set PERSISTENCE_BACKEND=sqlite to fall back.
ENV PERSISTENCE_BACKEND=hudi
ENV HUDI_TABLE_PATH=/data/hudi/fuel_prices

# -u: unbuffered stdout/stderr so logs appear in real-time via docker logs
CMD ["python", "-u", "main.py"]
