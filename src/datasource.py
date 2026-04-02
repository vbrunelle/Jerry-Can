"""Abstract data source and concrete implementations for fuel price data."""

import gzip
import json
import logging
import re
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any

import requests

from src.config import GEOJSON_URL, REQUEST_HEADERS, REQUEST_TIMEOUT

logger = logging.getLogger(__name__)

FUEL_TYPE_MAP = {
    "Régulier": "regular",
    "Super": "super",
    "Diesel": "diesel",
    "E10": "e10",
    "E85": "e85",
    "Ordinaire": "regular",
}

_PRICE_RE = re.compile(r"([\d.]+)")


def _parse_price(raw: str) -> float | None:
    """Extract a numeric price from a string like '190.9¢'."""
    m = _PRICE_RE.search(str(raw))
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


class DataSource(ABC):
    """Abstract base class for fuel price data sources."""

    @abstractmethod
    def fetch(self) -> list[dict[str, Any]]:
        """Fetch fuel price records.

        Returns a list of dicts with keys:
            station_id, station_name, address, city, region,
            latitude, longitude, fuel_type, price, fetched_at
        """


class GeoJsonDataSource(DataSource):
    """Fetches fuel prices from the Régie Essence Québec GeoJSON endpoint."""

    def __init__(self, url: str = GEOJSON_URL) -> None:
        self._url = url

    def fetch(self) -> list[dict[str, Any]]:
        logger.info("Fetching fuel prices from %s", self._url)
        response = requests.get(
            self._url,
            headers=REQUEST_HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        content = response.content
        try:
            content = gzip.decompress(content)
        except (gzip.BadGzipFile, OSError):
            pass

        data = json.loads(content)
        features = data.get("features", [])
        records = self._parse_features(features)
        logger.info("Fetched %d price records from %d stations.", len(records), len(features))
        return records

    @staticmethod
    def _parse_features(features: list[dict]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        fetched_at = datetime.now(UTC).isoformat(timespec="seconds")

        for idx, feature in enumerate(features):
            props = feature.get("properties", {})
            geometry = feature.get("geometry", {})
            coords = geometry.get("coordinates", [None, None])

            name = (props.get("Name") or "").strip()
            if not name:
                continue

            address = (props.get("Address") or "").strip()
            # Extract city from address (text after last comma) or leave blank
            city = ""
            if ", " in address:
                city = address.rsplit(", ", 1)[-1].strip()

            region = (props.get("Region") or "").strip()
            longitude = coords[0] if len(coords) >= 2 else None
            latitude = coords[1] if len(coords) >= 2 else None

            # Build a stable station ID from address (unique per location)
            station_id = address if address else f"station-{idx}"

            prices = props.get("Prices") or []
            for price_entry in prices:
                gas_type_raw = (price_entry.get("GasType") or "").strip()
                fuel_type = FUEL_TYPE_MAP.get(gas_type_raw, gas_type_raw.lower())
                price_value = _parse_price(price_entry.get("Price", ""))
                if price_value is None:
                    continue

                records.append({
                    "station_id": station_id,
                    "station_name": name,
                    "address": address,
                    "city": city,
                    "region": region,
                    "latitude": latitude,
                    "longitude": longitude,
                    "fuel_type": fuel_type,
                    "price": price_value,
                    "fetched_at": fetched_at,
                })

        return records
