from pathlib import Path
import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
)

SECRET_KEY = env("DJANGO_SECRET_KEY", default="dev-insecure-key")
DEBUG = env("DJANGO_DEBUG")
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["*"])

INSTALLED_APPS = [
    # Unfold : admin moderne (doit précéder django.contrib.admin)
    "unfold",
    "unfold.contrib.filters",
    "unfold.contrib.forms",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # 3rd party
    "rest_framework",
    "django_celery_beat",
    # local
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", default="chaos42"),
        "USER": env("POSTGRES_USER", default="chaos"),
        "PASSWORD": env("POSTGRES_PASSWORD", default="chaos"),
        "HOST": env("POSTGRES_HOST", default="db"),
        "PORT": env("POSTGRES_PORT", default="5432"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

LANGUAGE_CODE = "fr-fr"
TIME_ZONE = "Europe/Paris"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ─── Unfold (admin) ───
UNFOLD = {
    "SITE_TITLE": "Chaos 42",
    "SITE_HEADER": "Chaos Leaderboard 42",
    "SITE_SUBHEADER": "Back-office de la Piscine",
    "SITE_URL": "/dashboard/",
    "COLORS": {
        "primary": {
            "500": "143 224 58", "600": "126 200 45", "700": "100 160 35",
        },
    },
}

# ─── Celery + planning du snapshot de minuit (design §3) ───
from celery.schedules import crontab  # noqa: E402

CELERY_BROKER_URL = env("REDIS_URL", default="redis://redis:6379/0")
CELERY_RESULT_BACKEND = env("REDIS_URL", default="redis://redis:6379/0")
CELERY_TIMEZONE = TIME_ZONE
CELERY_BEAT_SCHEDULE = {
    "nightly-snapshot": {
        "task": "core.tasks.nightly_snapshot",
        "schedule": crontab(hour=0, minute=5),  # 00:05 chaque jour
    },
    "poll-42": {
        "task": "core.tasks.poll_42",
        "schedule": crontab(minute="*/10"),  # toutes les 10 min (no-op sans clés)
    },
    "sync-campus-users": {
        "task": "core.tasks.sync_campus_users",
        "schedule": crontab(hour=6, minute=0),  # 1×/jour : actualise la liste des étudiants
    },
    "weekly-designations": {
        "task": "core.tasks.weekly_designations",
        "schedule": crontab(day_of_week=1, hour=0, minute=10),  # lundi 00:10
    },
    "daily-derived": {
        "task": "core.tasks.daily_derived",
        "schedule": crontab(hour=0, minute=6),  # aura / ancienneté / week-end
    },
}

# ─── API 42 (étape 4) ───
FT_API_BASE = env("FT_API_BASE", default="https://api.intra.42.fr")


def _ft_credentials():
    """
    Pool de clés (UID/secret) qui se relaient. Sources, dans l'ordre :
      1. FT_API_UID_1/FT_API_SECRET_1 … FT_API_UID_20/FT_API_SECRET_20
      2. FT_API_UID / FT_API_SECRET (clé unique, rétro-compat)
      3. FT_API_KEYS = "uid1:secret1,uid2:secret2,…" (alternative 1 ligne)
    """
    creds = []
    for i in range(1, 21):
        u = env(f"FT_API_UID_{i}", default="")
        s = env(f"FT_API_SECRET_{i}", default="")
        if u and s:
            creds.append((u, s))
    u, s = env("FT_API_UID", default=""), env("FT_API_SECRET", default="")
    if u and s:
        creds.append((u, s))
    for pair in env("FT_API_KEYS", default="").split(","):
        pair = pair.strip()
        if ":" in pair:
            a, b = pair.split(":", 1)
            creds.append((a.strip(), b.strip()))
    seen, out = set(), []
    for c in creds:
        if c[0] not in seen:
            seen.add(c[0])
            out.append(c)
    return out


FT_API_CREDENTIALS = _ft_credentials()

# Cible : piscine du Havre, session Juillet 2026
_campus = env("FT_CAMPUS_ID", default="").strip()        # vide tant que non défini
FT_CAMPUS_ID = int(_campus) if _campus else 0            # ID campus Le Havre (voir `ft_campuses`)
_cursus = env("FT_CURSUS_ID", default="9").strip()       # 9 = "C Piscine" → isole la piscine
FT_CURSUS_ID = int(_cursus) if _cursus else 9
FT_POOL_YEAR = env("FT_POOL_YEAR", default="2026")
FT_POOL_MONTH = env("FT_POOL_MONTH", default="july")     # nom anglais minuscule

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
}
