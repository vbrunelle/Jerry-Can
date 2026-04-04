"""Configuration for the fuel price fetcher."""

import os

from dotenv import load_dotenv

load_dotenv()

# URL for the Quebec fuel price data (GeoJSON)
GEOJSON_URL = os.getenv(
    "GEOJSON_URL",
    "https://regieessencequebec.ca/stations.geojson.gz",
)

# Legacy URL (kept for reference / fallback)
FUEL_PRICE_URL = os.getenv(
    "FUEL_PRICE_URL",
    "https://regieessencequebec.ca/stations.geojson.gz",
)

# Database path for SQLite
DATABASE_PATH = os.getenv("DATABASE_PATH", "fuel_prices.db")

# Fetch interval in minutes
FETCH_INTERVAL_MINUTES = int(os.getenv("FETCH_INTERVAL_MINUTES", "5"))

# Inspection cache refresh interval in minutes (cron job in management server)
INSPECTION_REFRESH_INTERVAL_MINUTES = int(
    os.getenv("INSPECTION_REFRESH_INTERVAL_MINUTES", "5")
)

# HTTP request timeout in seconds
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))

# HTTP request headers
REQUEST_HEADERS = {
    "User-Agent": "Jerry-Can/1.0 (https://github.com/vbrunelle/Jerry-Can)",
    "Accept": "application/json, application/vnd.openxmlformats-officedocument.spreadsheetml.sheet, */*",
}

# ---------------------------------------------------------------------------
# Persistence backend selection ("sqlite" or "hudi")
# ---------------------------------------------------------------------------
PERSISTENCE_BACKEND = os.getenv("PERSISTENCE_BACKEND", "hudi")

# ---------------------------------------------------------------------------
# Apache Hudi settings (used only when PERSISTENCE_BACKEND == "hudi")
# ---------------------------------------------------------------------------

# Filesystem path where the Hudi table is stored (local or HDFS/S3).
HUDI_TABLE_PATH = os.getenv("HUDI_TABLE_PATH", "/data/hudi/fuel_prices")

# Logical Hudi table name.
HUDI_TABLE_NAME = os.getenv("HUDI_TABLE_NAME", "fuel_prices")

# Spark shuffle parallelism used by Hudi upserts/inserts.
HUDI_PARALLELISM = os.getenv("HUDI_PARALLELISM", "2")
