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
| `NGINX_CONF`                  | Configuration nginx injectée au démarrage du conteneur     | *(voir `.env.example`)*|

### Configuration nginx sans fichiers locaux

La configuration nginx est entièrement définie dans le fichier `.env` via la variable `NGINX_CONF`.
Au démarrage, le conteneur nginx écrit cette valeur dans `/etc/nginx/conf.d/default.conf` avant de lancer nginx.
Aucun fichier local n'est monté dans le conteneur.

**Pourquoi cette approche ?**  
L'ancienne configuration montait `./nginx/default.conf` depuis le système de fichiers hôte
(`bind-mount`). Si ce fichier n'existait pas ou si Docker créait automatiquement un répertoire à
sa place (comportement Docker par défaut lorsque le chemin source est absent), nginx échouait avec
l'erreur :

```
mount src=…/nginx/default.conf … not a directory: Are you trying to mount a directory onto a
file (or vice-versa)?
```

En injectant la configuration via `NGINX_CONF`, cette dépendance aux fichiers locaux est éliminée :
tout est contenu dans `.env` et `docker-compose.yml`, aucun fichier hôte supplémentaire n'est requis.

**Format de `NGINX_CONF`**  
La valeur doit tenir sur une seule ligne dans le fichier `.env`.
Utilisez `\n` pour représenter les sauts de ligne — le conteneur convertit ces séquences en vrais
retours chariot via `printf "%b"`.

Les variables nginx (`$host`, `$remote_addr`, etc.) sont des variables **nginx**, pas des variables
shell. Elles **n'ont pas besoin d'être échappées** dans le fichier `.env` car Docker Compose ne
substitue pas les variables dans les valeurs du fichier `.env`.
En revanche, si vous deviez écrire `$host` directement dans `docker-compose.yml` (pas dans `.env`),
il faudrait écrire `$$host`.

**Changer le port d'écoute**  
Modifiez `NGINX_PORT` dans `.env` :

```dotenv
NGINX_PORT=9090
```

**Vérification après démarrage**

```bash
# Démarrer uniquement nginx (et ses dépendances)
docker compose up -d nginx

# Vérifier les logs nginx
docker compose logs -f nginx

# Vérifier que la config a bien été écrite dans le conteneur
docker compose exec nginx cat /etc/nginx/conf.d/default.conf
```

**Limites de l'approche par variable d'environnement**

- Une configuration longue dans une variable d'environnement est moins lisible qu'un fichier dédié.
- Les variables d'environnement peuvent apparaître dans les outils d'inspection (`docker inspect`).
- Si vous préférez éviter les variables d'environnement, vous pouvez :
  - **Image personnalisée** : créer un `Dockerfile` basé sur `nginx:alpine` qui copie `default.conf`
    dans l'image et publier cette image (ex. `ghcr.io/vbrunelle/jerry-can-nginx:latest`), puis
    remplacer `image: nginx:alpine` dans `docker-compose.yml`.
  - **Docker Configs** (mode Swarm uniquement) : utiliser `docker config create` pour stocker la
    configuration et la monter via `configs:` dans le compose.

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
