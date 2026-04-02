FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY main.py .

VOLUME ["/data"]

ENV DATABASE_PATH=/data/fuel_prices.db

# -u: unbuffered stdout/stderr so logs appear in real-time via docker logs
CMD ["python", "-u", "main.py"]
