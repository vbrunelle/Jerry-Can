# Architecture Decision Record — Stockage basé sur les changements

## Contexte

Jerry-Can collecte les prix d'essence de toutes les stations-service du Québec aux
5 minutes via la Régie de l'énergie du Québec. L'architecture initiale enregistrait
**un snapshot complet** de chaque collecte dans une base de données SQLite :

| Stations | Types carburant | Snapshots/jour | Lignes/jour     |
|----------|-----------------|----------------|-----------------|
| ~3 000   | ~3 par station  | 288 (aux 5 min)| **~2,6 millions** |

Sur un mois, cela représente environ **78 millions de lignes**, alors que les prix
ne changent typiquement qu'une fois par jour. Ce volume excessif entraîne :

* Croissance rapide de la taille de la base de données sur disque
* Requêtes analytiques de plus en plus lentes
* Redondance massive (>99 % des données sont des doublons)

L'objectif est de ne conserver que les **changements** (ajout, mise à jour, retrait)
tout en offrant la possibilité de reconstituer l'état complet à n'importe quel moment.

---

## Solutions évaluées

### 1. SCD Type 2 dans SQLite (implémentation manuelle)

Implémenter un patron *Slowly Changing Dimension Type 2* directement dans le schéma
SQLite : table `price_changes` ne contenant que les deltas, avec un champ
`change_type` (add/update/remove) et reconstruction par requête SQL fenêtrée.

| Critère                  | Évaluation |
|--------------------------|------------|
| Complexité               | Élevée — toute la logique de détection de changement, de gestion du journal et de reconstruction doit être codée et maintenue manuellement. |
| Fiabilité                | Fragile — aucune garantie ACID sur les écritures multi-lignes (pas de vrai merge atomique). |
| Voyage dans le temps     | Manuel — requêtes SQL fenêtrées complexes. |
| Performance              | Limitée — SQLite n'est pas optimisé pour les lectures analytiques sur de grands ensembles de données. |
| Maintenance              | Tout le code de détection de changement est de la logique applicative à maintenir. |

### 2. Apache Iceberg (PyIceberg)

Format de table open source de la fondation Apache avec support natif du voyage dans
le temps, upsert, et évolution de schéma. Bibliothèque Python : `pyiceberg`.

| Critère                  | Évaluation |
|--------------------------|------------|
| Complexité               | Moyenne-élevée — nécessite un catalogue (SQLite, REST ou Glue) même en local. |
| Fiabilité                | Excellente — transactions ACID, snapshots natifs. |
| Voyage dans le temps     | Natif — navigation par snapshot ou timestamp. |
| Performance              | Très bonne — stockage Parquet columnar. |
| Maturité Python          | En progression rapide mais API Python moins mature que delta-rs pour le merge local. |
| Maintenance              | Le catalogue ajoute une couche de complexité opérationnelle. |

### 3. Delta Lake via `deltalake` (delta-rs) ✅ **Solution retenue**

Format de table open source (Linux Foundation / delta.io) construit au-dessus de
fichiers Parquet avec un journal de transactions JSON. La bibliothèque Python
`deltalake` (basée sur l'implémentation Rust `delta-rs`) fonctionne **sans JVM
ni Spark**, directement sur le système de fichiers local.

| Critère                  | Évaluation |
|--------------------------|------------|
| Complexité               | **Faible** — aucun catalogue, aucun serveur externe. Une simple directory locale suffit. |
| Fiabilité                | **Excellente** — transactions ACID garanties par le journal de transactions. |
| Voyage dans le temps     | **Natif** — `DeltaTable(path, version=N)` ou `load_with_datetime()`. |
| Performance              | **Très bonne** — format Parquet columnar avec statistiques par fichier. |
| Merge natif              | **Oui** — `DeltaTable.merge()` avec clauses `when_matched_update`, `when_not_matched_insert`, et `when_not_matched_by_source_delete`. |
| Maturité Python          | **Élevée** — v1.5.0 stable, merge disponible depuis v0.14, API bien documentée. |
| Dépendances              | Légères — `deltalake` + `pyarrow` (optionnel). Pas de JVM. |
| Maintenance              | **Minimale** — la détection de changement, l'écriture conditionnelle et le versionnage sont gérés par la bibliothèque. |

### 4. DuckDB (moteur analytique)

Base de données analytique embarquée (« SQLite pour l'analytique »), haute
performance pour les requêtes OLAP.

| Critère                  | Évaluation |
|--------------------------|------------|
| Complexité               | Faible pour les requêtes, mais ne résout pas nativement la détection de changements. |
| Fiabilité                | Bonne — ACID au niveau de la base. |
| Voyage dans le temps     | **Absent** — pas de versionnage natif. Doit être combiné avec Delta Lake ou Iceberg. |
| Performance              | Excellente pour les requêtes analytiques. |
| Merge natif              | Non — c'est un moteur de requêtes, pas un format de table versionné. |
| Maintenance              | Toute la logique de changement resterait à coder manuellement. |

---

## Justification du choix : Delta Lake (`deltalake`)

### Arguments factuels

1. **Réduction de volume prouvée**
   L'opération `merge` de Delta Lake n'écrit de nouveaux fichiers Parquet que pour
   les lignes effectivement modifiées. Lors de snapshots successifs sans changement
   de prix, **aucune nouvelle donnée n'est écrite** (seul un commit vide est
   enregistré dans le journal). Cela réduit le stockage de >99 % par rapport à
   l'approche snapshot-complet.

2. **Merge atomique avec trois clauses**
   ```python
   dt.merge(source, predicate="target.station_id = source.station_id AND ...")
     .when_matched_update_all()          # prix modifiés
     .when_not_matched_insert_all()      # nouvelles stations
     .when_not_matched_by_source_delete() # stations disparues
     .execute()
   ```
   Les trois types de changements demandés (ajout, mise à jour, retrait) sont
   gérés en une seule opération atomique, sans code applicatif de comparaison.

3. **Voyage dans le temps intégré**
   ```python
   # État des stations à la version 42
   dt = DeltaTable("data/station_prices", version=42)
   df = dt.to_pandas()
   ```
   Chaque merge crée une nouvelle version. On peut reconstituer l'état exact de
   toutes les stations à n'importe quel moment sans requête SQL complexe.

4. **Aucune infrastructure externe**
   Contrairement à Iceberg (qui requiert un catalogue), Delta Lake fonctionne
   directement sur le système de fichiers local. Le journal de transactions est
   un ensemble de fichiers JSON dans le dossier `_delta_log/`.

5. **Compatibilité avec l'écosystème existant**
   Le projet utilise déjà `pandas`. Delta Lake s'intègre nativement avec pandas
   via `to_pandas()` et accepte les DataFrames pandas en entrée du merge.

6. **Bibliothèque légère et sans JVM**
   `deltalake` (delta-rs) est une implémentation Rust avec bindings Python.
   Pas besoin de Java, Scala, ou Spark. La taille de la dépendance est
   d'environ 40 Mo (vs des centaines de Mo pour Spark).

---

## Architecture retenue

```
regieessencequebec.ca ──(HTTP)──▶ datasource.py ──▶ database.py ──▶ data/station_prices/
                                       ▲                                  │
                                  scheduler                          Delta Lake
                                                                   (Parquet + _delta_log/)
```

### Table Delta : `station_prices`

| Colonne        | Type   | Description                                    |
|---------------|--------|------------------------------------------------|
| `station_id`   | STRING | Identifiant de la station (clé de merge)       |
| `station_name` | STRING | Nom du détaillant                              |
| `address`      | STRING | Adresse                                        |
| `city`         | STRING | Ville                                          |
| `region`       | STRING | Région administrative                          |
| `latitude`     | DOUBLE | Latitude                                       |
| `longitude`    | DOUBLE | Longitude                                      |
| `fuel_type`    | STRING | Type de carburant (clé de merge)               |
| `price`        | DOUBLE | Prix en ¢/L                                    |

**Clé de merge** : `(station_id, fuel_type)`

### Versionnage

Chaque appel à `save_snapshot()` produit un `merge` qui crée une nouvelle
version Delta si des changements sont détectés. Le journal `_delta_log/`
contient l'historique complet des opérations.

### Reconstruction d'état

```python
from src.database import get_state_at

# État à la version 10
stations = get_state_at(version=10)

# État le plus récent
stations = get_current_state()
```
