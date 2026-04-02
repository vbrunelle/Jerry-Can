"""Fetcher module for retrieving Quebec fuel price data from regieessencequebec.ca."""

import io
import logging
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import requests

from src.config import FUEL_PRICE_URL, REQUEST_HEADERS, REQUEST_TIMEOUT

logger = logging.getLogger(__name__)

# Mapping of French column names to internal field names for the Excel export.
# Adjust these if the actual column headers differ.
EXCEL_COLUMN_MAP = {
    "Numéro de station": "station_id",
    "Nom du détaillant": "station_name",
    "Adresse": "address",
    "Ville": "city",
    "Région": "region",
    "Latitude": "latitude",
    "Longitude": "longitude",
    "Type de carburant": "fuel_type",
    "Prix (¢/L)": "price",
    "Date de mise à jour": "updated_at",
    # Alternative column names that may appear
    "No station": "station_id",
    "Nom": "station_name",
    "Type carburant": "fuel_type",
    "Prix": "price",
    "Date mise à jour": "updated_at",
}

# Mapping of French fuel type names to internal values
FUEL_TYPE_MAP = {
    "Ordinaire": "regular",
    "Super": "super",
    "Diesel": "diesel",
    "E10": "e10",
    "E85": "e85",
}


def _parse_excel(content: bytes) -> list[dict[str, Any]]:
    """Parse an Excel response body into a list of station price records."""
    df = pd.read_excel(io.BytesIO(content), engine="openpyxl")
    df.rename(columns=EXCEL_COLUMN_MAP, inplace=True)

    required = {"station_id", "price"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"Excel file is missing expected columns: {missing}. "
            f"Found: {list(df.columns)}"
        )

    records = []
    fetched_at = datetime.now(UTC).isoformat(timespec="seconds")
    for _, row in df.iterrows():
        fuel_type_raw = str(row.get("fuel_type", "Ordinaire")).strip()
        fuel_type = FUEL_TYPE_MAP.get(fuel_type_raw, fuel_type_raw.lower())

        record: dict[str, Any] = {
            "station_id": str(row["station_id"]).strip(),
            "station_name": str(row.get("station_name", "")).strip(),
            "address": str(row.get("address", "")).strip(),
            "city": str(row.get("city", "")).strip(),
            "region": str(row.get("region", "")).strip(),
            "latitude": _to_float(row.get("latitude")),
            "longitude": _to_float(row.get("longitude")),
            "fuel_type": fuel_type,
            "price": float(row["price"]),
            "fetched_at": fetched_at,
        }
        records.append(record)

    return records


def _parse_json(data: list[dict]) -> list[dict[str, Any]]:
    """Parse a JSON response (list of station objects) into price records."""
    records = []
    fetched_at = datetime.now(UTC).isoformat(timespec="seconds")
    for station in data:
        station_id = str(
            station.get("id")
            or station.get("stationId")
            or station.get("no_station")
            or station.get("numero")
            or ""
        ).strip()
        if not station_id:
            continue

        station_name = str(
            station.get("name")
            or station.get("nom")
            or station.get("nomStation")
            or ""
        ).strip()

        address = str(
            station.get("address")
            or station.get("adresse")
            or ""
        ).strip()

        city = str(
            station.get("city")
            or station.get("ville")
            or ""
        ).strip()

        region = str(
            station.get("region")
            or station.get("regionAdministrative")
            or ""
        ).strip()

        latitude = _to_float(station.get("latitude") or station.get("lat"))
        longitude = _to_float(station.get("longitude") or station.get("lng") or station.get("lon"))

        # Stations may embed multiple fuel types in a list or at the top level.
        # "prix" in French can be either a list of price objects or a scalar value;
        # only treat it as a prices list when it is actually a list.
        prices_raw = station.get("prices") or station.get("carburants") or []
        prix_field = station.get("prix")
        if not prices_raw and isinstance(prix_field, list):
            prices_raw = prix_field

        if prices_raw:
            for price_entry in prices_raw:
                fuel_type_raw = str(
                    price_entry.get("fuelType")
                    or price_entry.get("typeCarburant")
                    or price_entry.get("type")
                    or "Ordinaire"
                ).strip()
                fuel_type = FUEL_TYPE_MAP.get(fuel_type_raw, fuel_type_raw.lower())
                price_value = _to_float(
                    price_entry.get("price")
                    or price_entry.get("prix")
                    or price_entry.get("value")
                )
                if price_value is None:
                    continue
                records.append({
                    "station_id": station_id,
                    "station_name": station_name,
                    "address": address,
                    "city": city,
                    "region": region,
                    "latitude": latitude,
                    "longitude": longitude,
                    "fuel_type": fuel_type,
                    "price": price_value,
                    "fetched_at": fetched_at,
                })
        else:
            # Single price at station level
            price_value = _to_float(
                station.get("price")
                or station.get("prix")
                or station.get("prixEssence")
            )
            if price_value is None:
                continue
            fuel_type_raw = str(
                station.get("fuelType")
                or station.get("typeCarburant")
                or "Ordinaire"
            ).strip()
            fuel_type = FUEL_TYPE_MAP.get(fuel_type_raw, fuel_type_raw.lower())
            records.append({
                "station_id": station_id,
                "station_name": station_name,
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


def _to_float(value: Any) -> float | None:
    """Convert a value to float, returning None if not possible."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_prices(url: str = FUEL_PRICE_URL) -> list[dict[str, Any]]:
    """
    Fetch current fuel prices from regieessencequebec.ca.

    The function tries JSON first; if the response content type indicates
    an Excel file it falls back to Excel parsing.

    Returns a list of price record dicts.
    """
    logger.info("Fetching fuel prices from %s", url)
    try:
        response = requests.get(
            url,
            headers=REQUEST_HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Failed to fetch fuel prices: %s", exc)
        raise

    content_type = response.headers.get("Content-Type", "")

    if "spreadsheet" in content_type or "excel" in content_type or url.endswith((".xlsx", ".xls")):
        logger.info("Parsing response as Excel.")
        records = _parse_excel(response.content)
    else:
        # Default: try JSON
        logger.info("Parsing response as JSON.")
        data = response.json()
        if isinstance(data, dict):
            # Unwrap common envelope shapes
            data = (
                data.get("stations")
                or data.get("data")
                or data.get("results")
                or data.get("items")
                or list(data.values())[0]
                if data
                else []
            )
        records = _parse_json(data)

    logger.info("Fetched %d price records.", len(records))
    return records
