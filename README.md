# Chaos Leaderboard 42

Classement alternatif et humoristique pour la Piscine de l'école 42.

> **Statut : étape 1** — Docker + DB + back-office Django opérationnels.
> Modèles : log d'events chronologique, config dynamique versionnée,
> snapshots quotidiens, tracking curl. La logique de calcul (snapshots de minuit,
> API 42) arrive aux étapes 2-4.

## Prérequis

Docker Desktop n'est pas encore installé sur cette machine :
```bash
brew install --cask docker      # puis lance Docker Desktop une fois
```

## Démarrage

```bash
cp .env.example .env             # renseigne au moins DJANGO_SECRET_KEY
docker compose up --build        # build + DB + Redis + Django
```

Au premier lancement, l'entrypoint :
1. attend Postgres,
2. applique les migrations,
3. crée le superuser depuis `.env` (`admin` / `admin` par défaut),
4. démarre le serveur sur http://localhost:8000.

### Charger des données de démo (Piscine, règles, étudiants, events)

```bash
docker compose exec backend python manage.py seed_demo
```

### Explorer

- Back-office : http://localhost:8000/admin/  (login `admin` / `admin`)
- Aperçu curl-able : `curl http://localhost:8000/leaderboard`
- Santé : `curl http://localhost:8000/healthz`

## Architecture (rappel)

| Brique | Rôle |
|---|---|
| `EventLog` | Log append-only ; le random est **figé à l'écriture** (`raw_points`). |
| `Rule` + `RuleVersion` | Config dynamique **versionnée dans le temps** (zéro hardcode). |
| `DailyCoefficient` | Multiplicateur journalier (figé via `locked`). |
| `DailySnapshot` | Score figé du jour ; `cumulative_total` = somme courante (pas de recalcul). |
| `CurlTracking` | Compte les `curl` (gains/pertes web). |

## Étapes suivantes

- **Étape 2** : moteur de règles (`evaluate(event, rule_version)`) lisant `params` JSONB.
- **Étape 3** : job Celery de minuit (snapshots) + recompute ciblé des jours *dirty* ;
  vraie route `/leaderboard`. Décommenter `worker`/`beat` dans `docker-compose.yml`.
- **Étape 4** : OAuth2 API 42 + polling idempotent (`dedup_key`).

## Sécurité

`.env` n'est jamais committé. **Régénère tes clés API 42** si elles ont fuité.
