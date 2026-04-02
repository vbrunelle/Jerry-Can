"""Tests for the fetcher module."""

import io
import json
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
import requests

from src.fetcher import (
    _parse_excel,
    _parse_json,
    _to_float,
    fetch_prices,
)


class TestToFloat:
    def test_int(self) -> None:
        assert _to_float(175) == 175.0

    def test_float_string(self) -> None:
        assert _to_float("189.9") == pytest.approx(189.9)

    def test_none(self) -> None:
        assert _to_float(None) is None

    def test_invalid_string(self) -> None:
        assert _to_float("n/a") is None


class TestParseJson:
    def _station(self, **kwargs) -> dict:
        base = {
            "id": "ST001",
            "name": "Station Test",
            "address": "1 Rue Test",
            "city": "Montréal",
            "region": "Montréal",
            "latitude": 45.5,
            "longitude": -73.5,
            "price": 175.9,
        }
        base.update(kwargs)
        return base

    def test_basic_station(self) -> None:
        records = _parse_json([self._station()])
        assert len(records) == 1
        assert records[0]["station_id"] == "ST001"
        assert records[0]["price"] == pytest.approx(175.9)

    def test_station_with_prices_list(self) -> None:
        station = self._station()
        station.pop("price")
        station["prices"] = [
            {"fuelType": "Ordinaire", "price": 175.9},
            {"fuelType": "Diesel", "price": 162.5},
        ]
        records = _parse_json([station])
        assert len(records) == 2
        fuel_types = {r["fuel_type"] for r in records}
        assert fuel_types == {"regular", "diesel"}

    def test_missing_price_skipped(self) -> None:
        station = self._station()
        station.pop("price")
        records = _parse_json([station])
        assert records == []

    def test_missing_id_skipped(self) -> None:
        station = {"name": "No ID Station", "price": 175.0}
        records = _parse_json([station])
        assert records == []

    def test_fuel_type_mapping(self) -> None:
        station = self._station(fuelType="Super")
        records = _parse_json([station])
        assert records[0]["fuel_type"] == "super"

    def test_french_keys(self) -> None:
        station = {
            "no_station": "QC999",
            "nom": "Station Française",
            "adresse": "99 Avenue du Parc",
            "ville": "Québec",
            "region": "Québec",
            "prix": 179.5,
        }
        records = _parse_json([station])
        assert len(records) == 1
        assert records[0]["station_id"] == "QC999"
        assert records[0]["price"] == pytest.approx(179.5)

    def test_empty_list(self) -> None:
        assert _parse_json([]) == []


class TestParseExcel:
    def _make_excel(self, data: dict) -> bytes:
        df = pd.DataFrame(data)
        buffer = io.BytesIO()
        df.to_excel(buffer, index=False, engine="openpyxl")
        return buffer.getvalue()

    def test_basic_excel(self) -> None:
        excel_bytes = self._make_excel(
            {
                "Numéro de station": ["ST001", "ST002"],
                "Nom du détaillant": ["Station A", "Station B"],
                "Adresse": ["1 Rue A", "2 Rue B"],
                "Ville": ["Montréal", "Québec"],
                "Région": ["Montréal", "Québec"],
                "Latitude": [45.5, 46.8],
                "Longitude": [-73.5, -71.2],
                "Type de carburant": ["Ordinaire", "Diesel"],
                "Prix (¢/L)": [175.9, 162.5],
            }
        )
        records = _parse_excel(excel_bytes)
        assert len(records) == 2
        assert records[0]["station_id"] == "ST001"
        assert records[0]["fuel_type"] == "regular"
        assert records[1]["fuel_type"] == "diesel"

    def test_missing_required_column_raises(self) -> None:
        excel_bytes = self._make_excel({"Ville": ["Montréal"]})
        with pytest.raises(ValueError, match="missing expected columns"):
            _parse_excel(excel_bytes)

    def test_alternative_column_names(self) -> None:
        excel_bytes = self._make_excel(
            {
                "No station": ["QC001"],
                "Nom": ["Station Alt"],
                "Prix": [180.0],
            }
        )
        records = _parse_excel(excel_bytes)
        assert len(records) == 1
        assert records[0]["station_id"] == "QC001"
        assert records[0]["price"] == pytest.approx(180.0)


class TestFetchPrices:
    def _mock_json_response(self, data: list) -> MagicMock:
        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.json.return_value = data
        mock_resp.raise_for_status.return_value = None
        return mock_resp

    def _mock_excel_response(self, excel_bytes: bytes) -> MagicMock:
        mock_resp = MagicMock()
        mock_resp.headers = {
            "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        }
        mock_resp.content = excel_bytes
        mock_resp.raise_for_status.return_value = None
        return mock_resp

    def test_fetches_json(self) -> None:
        stations = [
            {
                "id": "ST001",
                "name": "Station Test",
                "city": "Montréal",
                "price": 175.9,
            }
        ]
        with patch("src.fetcher.requests.get", return_value=self._mock_json_response(stations)):
            records = fetch_prices("https://example.com/api/stations")
        assert len(records) == 1
        assert records[0]["station_id"] == "ST001"

    def test_fetches_json_with_envelope(self) -> None:
        data = {
            "stations": [
                {"id": "ST001", "name": "Station Test", "price": 175.9}
            ]
        }
        with patch("src.fetcher.requests.get", return_value=self._mock_json_response(data)):
            records = fetch_prices("https://example.com/api/stations")
        assert len(records) == 1

    def test_raises_on_http_error(self) -> None:
        with patch(
            "src.fetcher.requests.get",
            side_effect=requests.RequestException("connection error"),
        ):
            with pytest.raises(requests.RequestException):
                fetch_prices("https://example.com/api/stations")

    def test_fetches_excel_by_content_type(self) -> None:
        df = pd.DataFrame(
            {
                "Numéro de station": ["ST001"],
                "Prix (¢/L)": [175.0],
            }
        )
        buf = io.BytesIO()
        df.to_excel(buf, index=False, engine="openpyxl")
        excel_bytes = buf.getvalue()

        with patch(
            "src.fetcher.requests.get",
            return_value=self._mock_excel_response(excel_bytes),
        ):
            records = fetch_prices("https://example.com/data.xlsx")
        assert len(records) == 1
        assert records[0]["station_id"] == "ST001"
