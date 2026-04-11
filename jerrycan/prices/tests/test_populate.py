"""Unit tests for Snapshot.populate() using the real API data structure."""
import pandas as pd
from django.test import TestCase

from prices.models import Analysis, Fuel, Price, Snapshot, Station


def _make_dataframe(features: list[dict]) -> pd.DataFrame:
    """Build a DataFrame mimicking what _fetch_data() returns from the real API."""
    records = []
    for feat in features:
        props = feat.get("properties", {})
        coords = feat.get("geometry", {}).get("coordinates", [None, None])
        records.append({**props, "longitude": coords[0], "latitude": coords[1]})
    return pd.DataFrame(records)


def _make_feature(name="Station A", address="123 rue Test",
                  region="Île-de-Montréal", prices=None):
    return {
        "geometry": {"type": "Point", "coordinates": [-73.56, 45.50]},
        "properties": {
            "Name": name,
            "Address": address,
            "Region": region,
            "Prices": prices if prices is not None else [
                {"GasType": "Régulier", "Price": "199.9¢", "IsAvailable": True},
                {"GasType": "Super",    "Price": "224.9¢", "IsAvailable": True},
                {"GasType": "Diesel",   "Price": "269.9¢", "IsAvailable": True},
            ],
        },
    }


class TestPopulate(TestCase):

    def setUp(self):
        self.analysis = Analysis.objects.create(
            data_source_url="http://fake.example.com/",
            update_frequency=5,
            active=True,
        )
        self.snapshot = Snapshot.objects.create(analysis=self.analysis)

    # --- Station creation --------------------------------------------------

    def test_creates_station(self):
        self.snapshot.populate(_make_dataframe([_make_feature(name="Gas Plus")]))
        self.assertTrue(Station.objects.filter(name="Gas Plus").exists())

    def test_creates_one_station_per_feature(self):
        features = [_make_feature(name=f"Station {i}") for i in range(3)]
        self.snapshot.populate(_make_dataframe(features))
        self.assertEqual(Station.objects.count(), 3)

    def test_station_fields_are_mapped(self):
        self.snapshot.populate(_make_dataframe([_make_feature(
            name="Test Station", address="456 av. Test, Laval", region="Laurentides"
        )]))
        station = Station.objects.get(name="Test Station")
        self.assertEqual(station.adress, "456 av. Test")
        self.assertEqual(station.city, "Laval")
        self.assertEqual(station.region, "Laurentides")
        self.assertAlmostEqual(station.longitude, -73.56)
        self.assertAlmostEqual(station.latitude, 45.50)

    # --- Fuel creation -----------------------------------------------------

    def test_creates_fuels(self):
        self.snapshot.populate(_make_dataframe([_make_feature()]))
        self.assertTrue(Fuel.objects.filter(name="Régulier").exists())
        self.assertTrue(Fuel.objects.filter(name="Super").exists())
        self.assertTrue(Fuel.objects.filter(name="Diesel").exists())

    # --- Price creation ----------------------------------------------------

    def test_creates_prices_for_each_fuel(self):
        self.snapshot.populate(_make_dataframe([_make_feature()]))
        self.assertEqual(Price.objects.filter(snapshot=self.snapshot).count(), 3)

    def test_price_value_is_parsed_correctly(self):
        """Price comes as '199.9¢' — should be stored as a decimal number."""
        self.snapshot.populate(_make_dataframe([_make_feature(prices=[
            {"GasType": "Régulier", "Price": "199.9¢", "IsAvailable": True},
        ])]))
        price = Price.objects.get(snapshot=self.snapshot)
        self.assertAlmostEqual(float(price.price), 199.9)

    def test_unavailable_price_is_skipped(self):
        self.snapshot.populate(_make_dataframe([_make_feature(prices=[
            {"GasType": "Régulier", "Price": "199.9¢", "IsAvailable": True},
            {"GasType": "Super",    "Price": None,      "IsAvailable": False},
        ])]))
        self.assertEqual(Price.objects.filter(snapshot=self.snapshot).count(), 1)

    def test_no_prices_field_creates_no_prices(self):
        feature = _make_feature()
        feature["properties"]["Prices"] = None
        self.snapshot.populate(_make_dataframe([feature]))
        self.assertEqual(Price.objects.filter(snapshot=self.snapshot).count(), 0)

    # --- Multiple features -------------------------------------------------

    def test_multiple_stations_with_prices(self):
        features = [_make_feature(name=f"Station {i}") for i in range(4)]
        self.snapshot.populate(_make_dataframe(features))
        self.assertEqual(Price.objects.filter(snapshot=self.snapshot).count(), 4 * 3)

    def test_idempotent_station_upsert(self):
        """Calling populate twice on same data should not duplicate stations."""
        data = _make_dataframe([_make_feature(name="Same Station")])
        snapshot2 = Snapshot.objects.create(analysis=self.analysis)
        self.snapshot.populate(data)
        snapshot2.populate(data)
        self.assertEqual(Station.objects.filter(name="Same Station").count(), 1)
