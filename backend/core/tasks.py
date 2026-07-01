"""Tâches Celery — exécutées par worker/beat (design §3)."""
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from .ft_api import FtClient
from .models import DailyCoefficient, Pool
from .services import snapshot_day
from .sync import fetch_live, sync_all


@shared_task
def nightly_snapshot():
    """
    Job de minuit : fige la veille (jour qui vient de se clôturer) pour chaque
    Piscine active, puis verrouille son coefficient. Idempotent & rejouable.
    """
    yesterday = timezone.localdate() - timedelta(days=1)
    summary = {}
    for pool in Pool.objects.filter(is_active=True):
        count = snapshot_day(pool, yesterday)
        DailyCoefficient.objects.filter(pool=pool, day=yesterday).update(locked=True)
        summary[pool.slug] = count
    return {"day": str(yesterday), "snapshots": summary}


@shared_task
def weekly_designations():
    """Désigne les Maudits/Bénis de la semaine pour chaque Piscine active."""
    from .derived import assign_designations
    return {p.slug: assign_designations(p) for p in Pool.objects.filter(is_active=True)}


@shared_task
def daily_derived():
    """Calculs dérivés du jour : coef aléatoire + week-end, ancienneté, aura."""
    from .derived import (
        apply_aura_penalty, apply_seniority,
        ensure_weekend_coefficients, randomize_daily_coefficient,
    )
    out = {}
    for pool in Pool.objects.filter(is_active=True):
        coef = randomize_daily_coefficient(pool)   # random du jour courant
        ensure_weekend_coefficients(pool)          # écrase si le jour est un week-end
        weeks, _ = apply_seniority(pool)
        auras = apply_aura_penalty(pool)
        out[pool.slug] = {"coef": str(coef), "weeks": weeks, "auras": auras}
    return out


@shared_task
def poll_42():
    """Polling périodique de l'API 42 → ingestion via le moteur. No-op sans clés."""
    client = FtClient()
    if not client.configured:
        return {"skipped": "aucune clé API configurée"}
    today = timezone.localdate()
    out = {}
    for pool in Pool.objects.filter(is_active=True):
        # on ne poll QUE la piscine en cours (évite de polluer un pool de test/historique)
        if not (pool.starts_on <= today <= pool.ends_on):
            out[pool.slug] = "hors période — ignoré"
            continue
        data = fetch_live(client, campus_id=settings.FT_CAMPUS_ID, cursus_id=settings.FT_CURSUS_ID)
        out[pool.slug] = sync_all(pool, data)
    return out


@shared_task
def sync_campus_users():
    """Actualise quotidiennement la liste des étudiants de la piscine ciblée."""
    from .sync import fetch_campus_users, sync_users
    client = FtClient()
    if not client.configured or not settings.FT_CAMPUS_ID:
        return {"skipped": "clés ou FT_CAMPUS_ID manquants"}
    out = {}
    for pool in Pool.objects.filter(is_active=True):
        data = fetch_campus_users(client, settings.FT_CAMPUS_ID,
                                  pool_year=settings.FT_POOL_YEAR,
                                  pool_month=settings.FT_POOL_MONTH)
        out[pool.slug] = sync_users(pool, data)
    return out
