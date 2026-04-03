# Jerry-Can

Collecteur périodique des prix d'essence des stations-service du Québec, alimenté par les données publiées toutes les 5 minutes par la [Régie de l'énergie du Québec](https://regieessencequebec.ca/).

## Fonctionnement

Le programme récupère automatiquement les données de prix de chaque station-service de la province et les enregistre via Apache Hudi (par défaut) ou SQLite, sur une base périodique configurable (défaut : toutes les 5 minutes).

```
regieessencequebec.ca  ──(HTTP)──▶  datasource.py  ──▶  database.py (SQLite)     ──▶  fuel_prices.db
                                         ▲                  ou
                                    scheduler          hudi_writer.py (Hudi)  ──▶  Parquet/Hudi table
```

## Structure du projet

```
Jerry-Can/
├── main.py              # Point d'entrée — initialise la BD et démarre le planificateur
├── src/
│   ├── config.py        # Configuration (variables d'environnement)
│   ├── datasource.py    # Récupération et analyse des données GeoJSON
│   ├── fetcher.py       # Récupération et analyse des données (JSON ou Excel, legacy)
│   ├── database.py      # Persistance SQLite (backend de repli)
│   └── hudi_writer.py   # Persistance Apache Hudi (backend par défaut)
├── tests/
│   ├── test_database.py     # Tests unitaires de la base de données SQLite
│   ├── test_hudi_writer.py  # Tests unitaires du writer Hudi
│   ├── test_fetcher.py      # Tests unitaires du fetcher
│   └── test_datasource.py   # Tests unitaires de la source de données
├── requirements.txt
└── .env.example
```

## Installation

```bash
pip install -r requirements.txt
```

> **Note :** `pyspark` (nécessaire pour Hudi) est inclus dans `requirements.txt`. Java 11+ doit être installé sur le système.

## Configuration

Copiez `.env.example` vers `.env` et ajustez les variables :

| Variable               | Description                                      | Défaut                                        |
|------------------------|--------------------------------------------------|-----------------------------------------------|
| `FUEL_PRICE_URL`       | URL de l'API ou du fichier Excel                 | `https://regieessencequebec.ca/api/stations`  |
| `DATABASE_PATH`        | Chemin vers la base de données SQLite            | `fuel_prices.db`                              |
| `FETCH_INTERVAL_MINUTES` | Fréquence de récupération (minutes)            | `5`                                           |
| `REQUEST_TIMEOUT`      | Délai d'expiration des requêtes HTTP (secondes)  | `30`                                          |
| `PERSISTENCE_BACKEND`  | Backend de persistance : `hudi` ou `sqlite`      | `hudi`                                        |
| `HUDI_TABLE_PATH`      | Chemin du tableau Hudi (local, HDFS ou S3)       | `/data/hudi/fuel_prices`                      |
| `HUDI_TABLE_NAME`      | Nom logique du tableau Hudi                      | `fuel_prices`                                 |

## Utilisation

### Collecte des données

```bash
python main.py
```

Le programme :
1. Initialise la couche de persistance (Hudi par défaut, ou SQLite si `PERSISTENCE_BACKEND=sqlite`)
2. Effectue une première collecte immédiatement au démarrage
3. Répète la collecte toutes les `FETCH_INTERVAL_MINUTES` minutes
4. S'arrête proprement avec `Ctrl+C`

### Inspecter les données collectées

```bash
# Backend SQLite (défaut)
python show.py

# SQLite — fichier spécifique
python show.py /chemin/vers/fuel_prices.db

# Backend Hudi (nécessite Java)
PERSISTENCE_BACKEND=hudi JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64 python show.py

# Hudi — chemin de table spécifique
PERSISTENCE_BACKEND=hudi JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64 python show.py /data/hudi/fuel_prices
```

Affiche le schéma, le résumé, les snapshots, les régions et les derniers prix.

## Backends de persistance

### Apache Hudi (défaut, basé sur les changements)

Backend optimisé qui ne persiste que les **changements réels** (création de station, changement de prix, disparition de station) au lieu de stocker chaque snapshot complet.

#### Pourquoi Hudi ?

- **Détection automatique des changements** : on soumet le snapshot complet et seuls les changements sont effectivement écrits (upsert natif sur `(station_id, fuel_type)` avec `fetched_at` comme champ de pré-combinaison).
- **Time travel natif** : lecture de l'état à n'importe quel instant dans l'historique via `get_snapshot_at()`.
- **Incremental query** : lecture optimisée des changements depuis un moment précis via `get_changes_since()`.
- **Stockage efficace** : colonnes Parquet compressées, partitionnement par `region`, index intégrés.
- **Compatible analytique** : les données sont lisibles directement par DuckDB, Spark, Trino, Presto, etc.

#### Schéma Hudi

| Champ          | Type   | Description                              |
|---------------|--------|------------------------------------------|
| `station_id`  | STRING | Identifiant de la station (record key)   |
| `fuel_type`   | STRING | Type de carburant (record key)           |
| `station_name`| STRING | Nom du détaillant                        |
| `address`     | STRING | Adresse                                  |
| `city`        | STRING | Ville                                    |
| `region`      | STRING | Région (champ de partitionnement)        |
| `latitude`    | DOUBLE | Latitude                                 |
| `longitude`   | DOUBLE | Longitude                                |
| `price`       | DOUBLE | Prix en ¢/L                              |
| `fetched_at`  | STRING | Horodatage UTC (champ de pré-combinaison)|

#### Requêtes analytiques

```python
from src.hudi_writer import get_snapshot_at, get_changes_since

# Reconstituer l'état des prix à un instant donné
df = get_snapshot_at("2026-04-01T12:00:00")
df.show()

# Obtenir les changements de prix depuis un instant donné
df = get_changes_since("2026-04-01T00:00:00")
df.show()
```

#### Impact sur la stack

| Composant        | Avant (SQLite)            | Après (Hudi)                                         |
|-----------------|---------------------------|------------------------------------------------------|
| Runtime          | Python 3.12               | Python 3.12 + Java 11+ + PySpark 3.5+               |
| Stockage         | SQLite (fuel_prices.db)   | Parquet/Hudi (répertoire configurable)               |
| Volumétrie/jour  | ~2.5M lignes (snapshots)  | Quelques milliers de lignes (changements uniquement) |
| Image Docker     | ~150 Mo                   | ~1.5 Go (avec Java + Spark)                         |
| Requêtes         | SQL sur SQLite             | Spark SQL / DataFrame API / DuckDB                   |
| Time travel      | Manuel (parcourir `fetched_at`) | Natif (`as.of.instant`)                         |
| Incremental read | Non supporté              | Natif (`hoodie.datasource.query.type=incremental`)   |

### SQLite (fallback)

Backend alternatif léger, sans dépendances supplémentaires. Chaque collecte insère **toutes** les lignes de prix dans la table `prices` (snapshot complet).

Pour activer SQLite :

```bash
export PERSISTENCE_BACKEND=sqlite
python main.py
```

##### Table `stations`

| Colonne      | Type    | Description                |
|-------------|---------|----------------------------|
| `id`         | TEXT PK | Identifiant de la station  |
| `name`       | TEXT    | Nom du détaillant          |
| `address`    | TEXT    | Adresse                    |
| `city`       | TEXT    | Ville                      |
| `region`     | TEXT    | Région administrative      |
| `latitude`   | REAL    | Latitude                   |
| `longitude`  | REAL    | Longitude                  |
| `created_at` | TEXT    | Date de création           |

##### Table `prices`

| Colonne      | Type       | Description                        |
|-------------|------------|------------------------------------|
| `id`         | INTEGER PK | Clé primaire auto-incrémentée      |
| `station_id` | TEXT FK    | Référence vers `stations.id`       |
| `fuel_type`  | TEXT       | Type de carburant (regular, super, diesel…) |
| `price`      | REAL       | Prix en ¢/L                        |
| `fetched_at` | TEXT       | Horodatage UTC de la collecte      |

## Docker

### Image par défaut (Hudi)

```bash
docker compose up -d
```

### Avec le backend SQLite

Ajoutez la variable d'environnement dans votre `.env` ou dans `docker-compose.yml` :

```yaml
environment:
  PERSISTENCE_BACKEND: sqlite
```

## Tests

```bash
python -m pytest tests/ -v
```
