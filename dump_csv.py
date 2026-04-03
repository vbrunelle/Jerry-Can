"""Export the full price-change history to a CSV file.

Uses Hudi's native CDC (Change Data Capture) logs to retrieve the complete
history of every price change at every station — the same data source used
by show_price_variations.

Usage
-----
# Inside Docker (or locally with Spark available):
python dump_csv.py                        # writes /data/dump_prix.csv
python dump_csv.py /data/custom_name.csv  # custom output path
"""

import sys

from src.config import PERSISTENCE_BACKEND
from src.reader import get_reader

DEFAULT_OUTPUT = "/data/dump_prix.csv"


def main() -> None:
    output_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUTPUT

    print(f"Backend : {PERSISTENCE_BACKEND}")
    print("Lecture de l'historique CDC des changements de prix...")

    reader = get_reader()
    df = reader.read_changes()

    from pyspark.sql import functions as F

    # Keep only meaningful rows: inserts (first price seen), updates
    # where the price actually changed (delta != 0), and deletes (station
    # disappeared from the source). This removes the redundant rows Hudi
    # writes on every upsert cycle when nothing moved.
    df_filtered = df.filter(
        (F.col("op") == "i")
        | (F.col("op") == "d")
        | ((F.col("op") == "u") & F.col("delta").isNotNull() & (F.col("delta") != 0))
    )

    df_out = (
        df_filtered.select("station_id", "station_name", "city", "region",
                           "fuel_type", "prev_price", "price", "delta",
                           "prev_fetched_at", "fetched_at", "op")
        .orderBy("region", "city", "station_name", "fuel_type", "fetched_at")
    )

    count = df_out.count()
    if count == 0:
        print("Aucun changement de prix trouvé. Le CSV n'a pas été généré.")
        return

    df_out.coalesce(1).toPandas().to_csv(output_path, index=False)
    print(f"{count} lignes exportées vers {output_path}")


if __name__ == "__main__":
    main()
