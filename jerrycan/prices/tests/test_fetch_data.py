"""Unit tests for Analysis._fetch_data — all HTTP calls are mocked."""
import gzip
import json
from unittest.mock import MagicMock, patch

import pandas as pd
import requests
from django.test import TestCase

from prices.models import Analysis


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_geojson_gz(features: list) -> bytes:
    geojson = {"type": "FeatureCollection", "features": features}
    return gzip.compress(json.dumps(geojson).encode())


def _make_feature(name="Station A", city="Montréal", region="Île-de-Montréal",
                  adress="123 rue Test", lon=-73.56, lat=45.50,
                  regular=1.899, premium=2.099, diesel=1.799):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "name": name,
            "city": city,
            "region": region,
            "adress": adress,
            "regular": regular,
            "premium": premium,
            "diesel": diesel,
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFetchData(TestCase):
    """Unit tests for Analysis._fetch_data (no real network)."""

    def _analysis(self):
        return Analysis(
            data_source_url="http://fake.example.com/stations.geojson.gz",
            update_frequency=5,
        )

    # --- Happy path --------------------------------------------------------

    @patch("prices.models.requests.get")
    def test_returns_dataframe(self, mock_get):
        mock_get.return_value = MagicMock(content=_make_geojson_gz([_make_feature()]))
        self.assertIsInstance(self._analysis()._fetch_data(), pd.DataFrame)

    @patch("prices.models.requests.get")
    def test_dataframe_has_expected_columns(self, mock_get):
        mock_get.return_value = MagicMock(content=_make_geojson_gz([_make_feature()]))
        df = self._analysis()._fetch_data()
        for col in ("name", "city", "region", "adress", "longitude", "latitude",
                    "regular", "premium", "diesel"):
            self.assertIn(col, df.columns, msg=f"Missing column: {col}")

    @patch("prices.models.requests.get")
    def test_one_row_per_feature(self, mock_get):
        features = [_make_feature(name=f"Station {i}") for i in range(5)]
        mock_get.return_value = MagicMock(content=_make_geojson_gz(features))
        self.assertEqual(len(self._analysis()._fetch_data()), 5)

    @patch("prices.models.requests.get")
    def test_coordinates_are_mapped(self, mock_get):
        mock_get.return_value = MagicMock(content=_make_geojson_gz([_make_feature(lon=-71.0, lat=46.5)]))
        df = self._analysis()._fetch_data()
        self.assertAlmostEqual(df.iloc[0]["longitude"], -71.0)
        self.assertAlmostEqual(df.iloc[0]["latitude"], 46.5)

    @patch("prices.models.requests.get")
    def test_empty_feature_collection_returns_empty_dataframe(self, mock_get):
        mock_get.return_value = MagicMock(content=_make_geojson_gz([]))
        self.assertTrue(self._analysis()._fetch_data().empty)

    # --- Error handling ----------------------------------------------------

    @patch("prices.models.requests.get")
    def test_raises_on_http_error(self, mock_get):
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = requests.HTTPError("404")
        mock_get.return_value = mock_response
        with self.assertRaises(requests.HTTPError):
            self._analysis()._fetch_data()

    @patch("prices.models.requests.get")
    def test_raises_on_network_error(self, mock_get):
        mock_get.side_effect = requests.ConnectionError("unreachable")
        with self.assertRaises(requests.ConnectionError):
            self._analysis()._fetch_data()

    @patch("prices.models.requests.get")
    def test_raises_on_invalid_gzip(self, mock_get):
        mock_get.return_value = MagicMock(content=b"this is not gzip data")
        with self.assertRaises(Exception):
            self._analysis()._fetch_data()

    @patch("prices.models.requests.get")
    def test_raises_on_invalid_json(self, mock_get):
        mock_get.return_value = MagicMock(content=gzip.compress(b"not json {{"))
        with self.assertRaises(Exception):
            self._analysis()._fetch_data()
