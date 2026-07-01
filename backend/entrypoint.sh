#!/usr/bin/env bash
set -e

echo "→ Attente de la base de données..."
until pg_isready -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" >/dev/null 2>&1; do
  sleep 1
done
echo "→ Base de données prête."

python manage.py migrate --noinput

# Crée le superuser depuis les variables d'env si absent (dev only)
python manage.py shell <<'PYEOF'
import os
from django.contrib.auth import get_user_model
User = get_user_model()
u = os.environ.get("DJANGO_SUPERUSER_USERNAME")
p = os.environ.get("DJANGO_SUPERUSER_PASSWORD")
e = os.environ.get("DJANGO_SUPERUSER_EMAIL", "")
if u and p and not User.objects.filter(username=u).exists():
    User.objects.create_superuser(u, e, p)
    print(f"→ Superuser '{u}' créé.")
else:
    print("→ Superuser déjà présent ou non configuré.")
PYEOF

echo "→ Démarrage du serveur sur http://localhost:8000"
python manage.py runserver 0.0.0.0:8000
