"""Export the full price-change history to a CSV file.

Uses Hudi's native time-travel (``as.of.instant``) to reconstruct the
complete history of every price change at every station by diffing
consecutive commit snapshots.

Usage
-----
python dump_csv.py                        # writes /data/dump_prix.csv
python dump_csv.py /data/custom_name.csv  # custom output path
"""

import sys

from src.config import HUDI_TABLE_PATH
from src.price_history import build_historicized_changes_pandas

DEFAULT_OUTPUT = "/data/dump_prix.csv"


def main() -> None:
    output_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUTPUT

    print("Construction de l'historique via time-travel Hudi...")

    df = build_historicized_changes_pandas(HUDI_TABLE_PATH)

    if df.empty:
        print("Aucun changement de prix trouvé. Le CSV n'a pas été généré.")
        return

    df = df.sort_values(
        ["region", "city", "station_name", "fuel_type", "fetched_at"],
    ).reset_index(drop=True)

    df.to_csv(output_path, index=False)
    print(f"{len(df)} lignes exportées vers {output_path}")


if __name__ == "__main__":
    main()
