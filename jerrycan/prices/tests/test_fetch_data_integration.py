"""Integration tests for Analysis._fetch_data — hits the real data source, no mocking."""
import pandas as pd
from django.test import TestCase

from prices.models import Analysis


class TestFetchDataIntegration(TestCase):
    """These tests make a real HTTP request to regieessencequebec.ca.

    The data is fetched once in setUpClass and reused across all tests
    to avoid hammering the server.
    """

    REAL_URL = "https://regieessencequebec.ca/stations.geojson.gz"
    _df: pd.DataFrame = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        analysis = Analysis(
            data_source_url=cls.REAL_URL,
            update_frequency=5,
        )
        cls._df = analysis._fetch_data()

    def test_returns_dataframe(self):
        self.assertIsInstance(self._df, pd.DataFrame)

    def test_dataframe_is_not_empty(self):
        self.assertGreater(len(self._df), 0)

    def test_has_longitude_and_latitude_columns(self):
        self.assertIn("longitude", self._df.columns)
        self.assertIn("latitude", self._df.columns)

    def test_coordinates_are_not_null(self):
        self.assertTrue(self._df["longitude"].notna().all(), "Some rows have no longitude")
        self.assertTrue(self._df["latitude"].notna().all(), "Some rows have no latitude")

    def test_coordinates_within_quebec_bounds(self):
        self.assertTrue(
            (self._df["longitude"] >= -80.0).all() and (self._df["longitude"] <= -56.0).all(),
            "Some longitudes are outside Quebec bounds",
        )
        self.assertTrue(
            (self._df["latitude"] >= 44.0).all() and (self._df["latitude"] <= 63.0).all(),
            "Some latitudes are outside Quebec bounds",
        )

    def test_has_name_column(self):
        """The real API returns 'Name' (capital N)."""
        self.assertIn("Name", self._df.columns)

    def test_all_names_are_non_empty_strings(self):
        self.assertTrue(
            self._df["Name"].apply(lambda v: isinstance(v, str) and len(v) > 0).all(),
            "Some station names are empty or not strings",
        )
