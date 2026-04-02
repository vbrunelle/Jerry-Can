"""Configuration for the fuel price fetcher."""

import os

from dotenv import load_dotenv

load_dotenv()

# URL for the Quebec fuel price data (Excel download)
FUEL_PRICE_URL = os.getenv(
    "FUEL_PRICE_URL",
    "https://regieessencequebec.ca/api/stations",
)

# Database path for SQLite
DATABASE_PATH = os.getenv("DATABASE_PATH", "fuel_prices.db")

# Fetch interval in minutes
FETCH_INTERVAL_MINUTES = int(os.getenv("FETCH_INTERVAL_MINUTES", "5"))

# HTTP request timeout in seconds
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))

# HTTP request headers
REQUEST_HEADERS = {
    "User-Agent": "Jerry-Can/1.0 (https://github.com/vbrunelle/Jerry-Can)",
    "Accept": "application/json, application/vnd.openxmlformats-officedocument.spreadsheetml.sheet, */*",
}
