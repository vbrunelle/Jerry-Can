"""Tests for the datasource module."""

import gzip
import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.datasource import (
    DataSource,
    GeoJsonDataSource,
    _parse_price,
)


# ---------------------------------------------------------------------------
# Unit tests (no network)
# ---------------------------------------------------------------------------


class TestParsePrice:
    def test_cents_suffix(self) -> None:
        assert _parse_price("190.9¢") == pytest.approx(190.9)

    def test_plain_number(self) -> None:
        assert _parse_price("175.0") == pytest.approx(175.0)

    def test_integer(self) -> None:
        assert _parse_price("180") == pytest.approx(180.0)

    def test_empty_string(self) -> None:
        assert _parse_price("") is None

    def test_no_number(self) -> None:
        assert _parse_price("N/A") is None

    def test_none(self) -> None:
        assert _parse_price(None) is None


def _make_feature(
    name: str = "Station Test",
    brand: str = "Shell",
    address: str = "1 Rue Test, Montréal",
    region: str = "Montréal",
    lat: float = 45.5,
    lng: float = -73.5,
    prices: list | None = None,
) -> dict:
    if prices is None:
        prices = [
            {"GasType": "Régulier", "Price": "190.9¢", "IsAvailable": True},
            {"GasType": "Super", "Price": "215.9¢", "IsAvailable": True},
            {"GasType": "Diesel", "Price": "268.9¢", "IsAvailable": True},
        ]
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lng, lat]},
        "properties": {
            "Name": name,
            "brand": brand,
            "Status": "En opération",
            "Address": address,
            "PostalCode": "H2X 1Y4",
            "Region": region,
            "Prices": prices,
        },
    }


def _make_geojson(*features: dict) -> dict:
    return {"type": "FeatureCollection", "metadata": {}, "features": list(features)}


def _mock_geojson_response(geojson: dict, gzipped: bool = True) -> MagicMock:
    body = json.dumps(geojson).encode()
    if gzipped:
        body = gzip.compress(body)
    mock_resp = MagicMock()
    mock_resp.content = body
    mock_resp.raise_for_status.return_value = None
    return mock_resp


class TestGeoJsonDataSourceParsing:
    def test_single_station_three_fuels(self) -> None:
        feature = _make_feature()
        geojson = _make_geojson(feature)
        with patch("src.datasource.requests.get", return_value=_mock_geojson_response(geojson)):
            records = GeoJsonDataSource(url="https://example.com/test.geojson.gz").fetch()
        assert len(records) == 3
        fuel_types = {r["fuel_type"] for r in records}
        assert fuel_types == {"regular", "super", "diesel"}

    def test_station_fields(self) -> None:
        feature = _make_feature(
            name="Ma Station",
            address="99 Boul René-Lévesque, Québec",
            region="Capitale-Nationale",
            lat=46.8,
            lng=-71.2,
        )
        geojson = _make_geojson(feature)
        with patch("src.datasource.requests.get", return_value=_mock_geojson_response(geojson)):
            records = GeoJsonDataSource(url="https://example.com/test.geojson.gz").fetch()
        rec = records[0]
        assert rec["station_id"] == "99 Boul René-Lévesque, Québec"
        assert rec["station_name"] == "Ma Station"
        assert rec["address"] == "99 Boul René-Lévesque, Québec"
        assert rec["city"] == "Québec"
        assert rec["region"] == "Capitale-Nationale"
        assert rec["latitude"] == pytest.approx(46.8)
        assert rec["longitude"] == pytest.approx(-71.2)

    def test_price_parsing(self) -> None:
        feature = _make_feature(prices=[
            {"GasType": "Régulier", "Price": "190.9¢", "IsAvailable": True},
        ])
        geojson = _make_geojson(feature)
        with patch("src.datasource.requests.get", return_value=_mock_geojson_response(geojson)):
            records = GeoJsonDataSource(url="https://example.com/test.geojson.gz").fetch()
        assert records[0]["price"] == pytest.approx(190.9)

    def test_missing_price_skipped(self) -> None:
        feature = _make_feature(prices=[
            {"GasType": "Régulier", "Price": "", "IsAvailable": True},
        ])
        geojson = _make_geojson(feature)
        with patch("src.datasource.requests.get", return_value=_mock_geojson_response(geojson)):
            records = GeoJsonDataSource(url="https://example.com/test.geojson.gz").fetch()
        assert records == []

    def test_missing_name_skipped(self) -> None:
        feature = _make_feature(name="")
        geojson = _make_geojson(feature)
        with patch("src.datasource.requests.get", return_value=_mock_geojson_response(geojson)):
            records = GeoJsonDataSource(url="https://example.com/test.geojson.gz").fetch()
        assert records == []

    def test_station_with_two_fuels(self) -> None:
        feature = _make_feature(prices=[
            {"GasType": "Régulier", "Price": "185.0¢", "IsAvailable": True},
            {"GasType": "Super", "Price": "210.0¢", "IsAvailable": True},
        ])
        geojson = _make_geojson(feature)
        with patch("src.datasource.requests.get", return_value=_mock_geojson_response(geojson)):
            records = GeoJsonDataSource(url="https://example.com/test.geojson.gz").fetch()
        assert len(records) == 2

    def test_multiple_stations(self) -> None:
        f1 = _make_feature(name="Station A")
        f2 = _make_feature(name="Station B")
        geojson = _make_geojson(f1, f2)
        with patch("src.datasource.requests.get", return_value=_mock_geojson_response(geojson)):
            records = GeoJsonDataSource(url="https://example.com/test.geojson.gz").fetch()
        assert len(records) == 6
        names = {r["station_name"] for r in records}
        assert names == {"Station A", "Station B"}

    def test_empty_features(self) -> None:
        geojson = _make_geojson()
        with patch("src.datasource.requests.get", return_value=_mock_geojson_response(geojson)):
            records = GeoJsonDataSource(url="https://example.com/test.geojson.gz").fetch()
        assert records == []

    def test_non_gzipped_response(self) -> None:
        feature = _make_feature()
        geojson = _make_geojson(feature)
        with patch("src.datasource.requests.get", return_value=_mock_geojson_response(geojson, gzipped=False)):
            records = GeoJsonDataSource(url="https://example.com/test.geojson").fetch()
        assert len(records) == 3

    def test_raises_on_http_error(self) -> None:
        with patch(
            "src.datasource.requests.get",
            side_effect=requests.RequestException("connection error"),
        ):
            with pytest.raises(requests.RequestException):
                GeoJsonDataSource(url="https://example.com/bad").fetch()

    def test_fuel_type_mapping(self) -> None:
        feature = _make_feature(prices=[
            {"GasType": "Ordinaire", "Price": "190.9¢", "IsAvailable": True},
        ])
        geojson = _make_geojson(feature)
        with patch("src.datasource.requests.get", return_value=_mock_geojson_response(geojson)):
            records = GeoJsonDataSource(url="https://example.com/test.geojson.gz").fetch()
        assert records[0]["fuel_type"] == "regular"

    def test_fetched_at_is_set(self) -> None:
        feature = _make_feature()
        geojson = _make_geojson(feature)
        with patch("src.datasource.requests.get", return_value=_mock_geojson_response(geojson)):
            records = GeoJsonDataSource(url="https://example.com/test.geojson.gz").fetch()
        assert records[0]["fetched_at"]


class TestDataSourceIsAbstract:
    def test_cannot_instantiate(self) -> None:
        with pytest.raises(TypeError):
            DataSource()


# ---------------------------------------------------------------------------
# Integration test — hits the real endpoint (network required)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestGeoJsonIntegration:
    """Validate that the real GeoJSON endpoint returns a healthy dataset.

    Run with: pytest -m integration
    """

    def test_fetches_over_2000_stations(self) -> None:
        source = GeoJsonDataSource()
        records = source.fetch()

        station_ids = {r["station_id"] for r in records}
        assert len(station_ids) > 2000, (
            f"Expected >2000 unique stations, got {len(station_ids)}"
        )
        # Also verify total records (stations × fuel types)
        assert len(records) > 5000, (
            f"Expected >5000 price records, got {len(records)}"
        )

    def test_every_record_has_required_fields(self) -> None:
        source = GeoJsonDataSource()
        records = source.fetch()

        required = {
            "station_id", "station_name", "address", "region",
            "latitude", "longitude", "fuel_type", "price", "fetched_at",
        }
        for rec in records:
            missing = required - set(rec.keys())
            assert not missing, f"Record missing keys {missing}: {rec}"

    def test_prices_are_positive(self) -> None:
        source = GeoJsonDataSource()
        records = source.fetch()

        for rec in records:
            assert rec["price"] > 0, f"Non-positive price: {rec}"

    def test_fuel_types_are_known(self) -> None:
        source = GeoJsonDataSource()
        records = source.fetch()

        known = {"regular", "super", "diesel", "e10", "e85"}
        fuel_types = {r["fuel_type"] for r in records}
        unknown = fuel_types - known
        assert not unknown, f"Unknown fuel types found: {unknown}"

    def test_regions_cover_quebec(self) -> None:
        source = GeoJsonDataSource()
        records = source.fetch()

        regions = {r["region"] for r in records}
        assert "Montréal" in regions
        assert "Capitale-Nationale" in regions
        assert len(regions) >= 15, f"Expected ≥15 regions, got {len(regions)}"
