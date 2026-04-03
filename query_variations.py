#!/usr/bin/env python3
"""Liste toutes les stations ayant eu au moins une variation de prix.

Pour chaque station/type-carburant, affiche :
  - le nombre total de changements de prix enregistrés
  - le prix minimum et maximum observés
  - l'amplitude (max - min)

Usage
-----
    # SQLite (historique complet, toutes les captures)
    PERSISTENCE_BACKEND=sqlite python query_variations.py
    PERSISTENCE_BACKEND=sqlite python query_variations.py ./data/fuel_prices.db

    # Hudi (lecture CDC native — before/after sans LAG)
    PERSISTENCE_BACKEND=hudi python query_variations.py
    PERSISTENCE_BACKEND=hudi python query_variations.py /data/hudi/fuel_prices

Comment ça marche
-----------------
SQLite : toutes les stations sont écrites à chaque snapshot, même celles
         dont le prix n'a pas changé.  SqliteReader.read_changes() applique
         un LAG Spark pour ne garder que les vraies variations.

Hudi   : activé avec hoodie.table.cdc.enabled=true et DATA_BEFORE_AFTER.
         HudiReader.read_changes() lit en mode query.type=cdc depuis t=000.
         Hudi fournit nativement les structs before/after — pas besoin de LAG.

Dans les deux cas, read_price_changes() retourne un DataFrame unifié où
chaque ligne = un vrai événement de changement de prix.
"""

import sys

from pyspark.sql import functions as F

from src.config import DATABASE_PATH, HUDI_TABLE_PATH, PERSISTENCE_BACKEND
from src.reader import read_price_changes


def main() -> None:
    backend = PERSISTENCE_BACKEND.lower()
    path = sys.argv[1] if len(sys.argv) > 1 else (
        DATABASE_PATH if backend == "sqlite" else HUDI_TABLE_PATH
    )

    print(f"Backend : {backend}  |  Chemin : {path}\n")

    # Chaque ligne retournée = un vrai changement de prix (logique différente par backend)
    df_changes = read_price_changes(backend=backend, path=path)

    change_count = df_changes.count()
    if change_count == 0:
        print("Aucune variation de prix détectée dans les données enregistrées.")
        return

    print(f"{change_count} événement(s) de changement de prix au total.\n")

    # Statistiques par (station, fuel_type)
    result = (
        df_changes
        .groupBy("station_id", "station_name", "city", "region", "fuel_type")
        .agg(
            F.count("*").alias("nb_variations"),
            F.round(F.min("price"), 1).alias("prix_min"),
            F.round(F.max("price"), 1).alias("prix_max"),
            F.round(F.max("price") - F.min("price"), 1).alias("amplitude"),
        )
        .orderBy(F.desc("nb_variations"), F.desc("amplitude"))
    )

    station_count = result.count()
    print(f"{station_count} station(s) ont eu au moins une variation de prix :\n")
    result.show(n=500, truncate=40)


if __name__ == "__main__":
    main()


    print(f"{station_count} station(s) ont eu au moins une variation de prix :\n")
    result.show(n=500, truncate=40)


if __name__ == "__main__":
    main()
