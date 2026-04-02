# Jerry-Can

Collecteur périodique des prix d'essence des stations-service du Québec, alimenté par les données publiées toutes les 5 minutes par la [Régie de l'énergie du Québec](https://regieessencequebec.ca/).

## Fonctionnement

Le programme récupère automatiquement les données de prix de chaque station-service de la province et les enregistre dans une base de données SQLite locale, sur une base périodique configurable (défaut : toutes les 5 minutes).

```
regieessencequebec.ca  ──(HTTP)──▶  fetcher.py  ──▶  database.py  ──▶  fuel_prices.db
                                         ▲
                                    scheduler (schedule)
```

## Structure du projet

```
Jerry-Can/
├── main.py              # Point d'entrée — initialise la BD et démarre le planificateur
├── src/
│   ├── config.py        # Configuration (variables d'environnement)
│   ├── fetcher.py       # Récupération et analyse des données (JSON ou Excel)
│   └── database.py      # Persistance SQLite
├── tests/
│   ├── test_fetcher.py  # Tests unitaires du fetcher
│   └── test_database.py # Tests unitaires de la base de données
├── requirements.txt
└── .env.example
```

## Installation

```bash
pip install -r requirements.txt
```

## Configuration

Copiez `.env.example` vers `.env` et ajustez les variables :

| Variable               | Description                                      | Défaut                                        |
|------------------------|--------------------------------------------------|-----------------------------------------------|
| `FUEL_PRICE_URL`       | URL de l'API ou du fichier Excel                 | `https://regieessencequebec.ca/api/stations`  |
| `DATABASE_PATH`        | Chemin vers la base de données SQLite            | `fuel_prices.db`                              |
| `FETCH_INTERVAL_MINUTES` | Fréquence de récupération (minutes)            | `5`                                           |
| `REQUEST_TIMEOUT`      | Délai d'expiration des requêtes HTTP (secondes)  | `30`                                          |

## Utilisation

```bash
python main.py
```

Le programme :
1. Initialise les tables SQLite (`stations` et `prices`) si elles n'existent pas
2. Effectue une première collecte immédiatement au démarrage
3. Répète la collecte toutes les `FETCH_INTERVAL_MINUTES` minutes
4. S'arrête proprement avec `Ctrl+C`

## Schéma de base de données

### Table `stations`

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

### Table `prices`

| Colonne      | Type       | Description                        |
|-------------|------------|------------------------------------|
| `id`         | INTEGER PK | Clé primaire auto-incrémentée      |
| `station_id` | TEXT FK    | Référence vers `stations.id`       |
| `fuel_type`  | TEXT       | Type de carburant (regular, super, diesel…) |
| `price`      | REAL       | Prix en ¢/L                        |
| `fetched_at` | TEXT       | Horodatage UTC de la collecte      |

## Tests

```bash
python -m pytest tests/ -v
```
