# Jerry-Can

Collecteur périodique des prix d'essence des stations-service du Québec, alimenté par les données publiées par la [Régie de l'énergie du Québec](https://regieessencequebec.ca/). Les données sont collectées automatiquement et présentées via une interface web Django.

## Fonctionnement

L'application Django récupère périodiquement les données de prix de chaque station-service de la province et les enregistre dans une base de données SQLite. Une interface web permet de visualiser et d'analyser les données collectées.

```
regieessencequebec.ca  ──(HTTP)──▶  Analysis._fetch_data()  ──▶  SQLite (db.sqlite3)  ──▶  Interface web
                                          ▲
                                   planificateur Django
                                   (thread d'arrière-plan)
```

## Structure du projet

```
Jerry-Can/
├── jerrycan/                    # Application Django principale
│   ├── manage.py
│   ├── requirements.txt
│   ├── entrypoint.sh            # Script de démarrage (migrations + gunicorn)
│   ├── Dockerfile
│   ├── jerrycan/                # Configuration Django (settings, urls, wsgi)
│   └── prices/                  # Application de collecte et visualisation des prix
│       ├── models.py            # Modèles : Analysis, Snapshot, Station, Fuel, Price
│       ├── views.py             # Vues et API JSON
│       ├── urls.py              # Routage URL
│       ├── templates/           # Gabarits HTML
│       └── management/
│           └── commands/
│               └── create_initial_admin.py  # Création du compte admin initial
├── nginx/
│   └── default.conf             # Configuration nginx (reverse proxy)
├── docker-compose.yml
└── .env.example
```

## Installation (développement local)

```bash
cd jerrycan
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

## Configuration

Copiez `.env.example` vers `.env` et ajustez les variables :

| Variable                      | Description                                                | Défaut               |
|-------------------------------|------------------------------------------------------------|----------------------|
| `DJANGO_SECRET_KEY`           | Clé secrète Django (obligatoire en production)             | *(non défini)*       |
| `DJANGO_DEBUG`                | Mode débogage (`True` ou `False`)                          | `False`              |
| `DJANGO_ALLOWED_HOSTS`        | Noms d'hôtes acceptés (séparés par des virgules)           | `localhost,127.0.0.1`|
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Origines CSRF autorisées (séparées par des virgules)       | `http://localhost:8080` |
| `DJANGO_DB_DIR`               | Répertoire du fichier SQLite (`db.sqlite3`)                | Répertoire du projet |
| `NGINX_PORT`                  | Port exposé par nginx sur la machine hôte                  | `8080`               |

## Utilisation

### Démarrer l'application

```bash
docker compose up -d
```

L'interface web est accessible à l'adresse `http://localhost:8080` (ou le port configuré via `NGINX_PORT`).

### Premier démarrage

Au premier démarrage, un compte administrateur est créé automatiquement avec un nom d'utilisateur et un mot de passe aléatoires. Les identifiants sont affichés dans les logs du conteneur :

```bash
docker compose logs web
```

Le mot de passe doit être changé lors de la première connexion.

### Créer et configurer une analyse

1. Connectez-vous à l'interface d'administration.
2. Créez une nouvelle **Analyse** (menu *Analyses*) en configurant :
   - **URL de la source de données** : URL du GeoJSON de la Régie de l'énergie du Québec.
   - **Fréquence de mise à jour** : intervalle entre les collectes (en minutes).
   - **Collecte automatique** : activez pour démarrer la collecte en arrière-plan.
3. Vous pouvez aussi déclencher une collecte manuelle depuis la page de détail de l'analyse.

## Docker

```bash
# Démarrer l'application
docker compose up -d

# Voir les logs (dont les identifiants admin au premier démarrage)
docker compose logs web

# Arrêter l'application
docker compose down
```

Les services démarrés sont :
- **web** : application Django (Gunicorn) sur le port 8000 (interne)
- **nginx** : reverse proxy exposé sur `NGINX_PORT` (défaut : 8080)

## Tests

```bash
cd jerrycan
python manage.py test
```
