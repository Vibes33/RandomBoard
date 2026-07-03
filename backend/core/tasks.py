"""Tâches Celery — exécutées par worker/beat (design §3)."""
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from .ft_api import FtClient
from .models import DailyCoefficient, Pool, SyncRun
from .services import snapshot_day
from .sync import fetch_live, sync_all


@shared_task
def run_sync(sync_run_id):
    """
    Lance une synchronisation API 42 (rejeu/ingestion) pilotée depuis le panel.
    Met à jour le SyncRun en direct (statut, progression, journal) → l'onglet
    « Synchronisation » l'affiche en temps réel via polling.
    """
    from .sync_runner import get_or_create_pool, resolve_target, run_full_sync

    run = SyncRun.objects.filter(id=sync_run_id).first()
    if not run:
        return {"error": "SyncRun introuvable"}

    # Annulation demandée avant même le démarrage du worker.
    if run.cancel_requested:
        run.status = SyncRun.Status.CANCELLED
        run.finished_at = timezone.now()
        run.append_log("Annulé avant démarrage.")
        run.save()
        return {"cancelled": True}

    run.status = SyncRun.Status.RUNNING
    run.started_at = timezone.now()
    run.append_log("Démarrage de la synchronisation…")
    run.save()

    try:
        client = FtClient()
        if not client.configured:
            raise RuntimeError("Aucune clé API configurée (voir .env / ft_doctor).")
        campus, cursus = resolve_target()
        if not campus:
            raise RuntimeError("FT_CAMPUS_ID non défini.")

        pool = run.pool
        if pool is None:
            pool, _ = get_or_create_pool(campus)
            run.pool = pool
        run.append_log(f"Piscine cible : {pool.name}")
        run.save()

        def on_log(line):
            run.append_log(line)
            run.save(update_fields=["log"])

        def on_progress(day, index, total, events):
            run.current_day = day
            run.days_done = index
            run.days_total = total
            run.events_ingested = events
            run.save(update_fields=["current_day", "days_done", "days_total",
                                    "events_ingested"])

        def should_cancel():
            return SyncRun.objects.filter(id=run.id, cancel_requested=True).exists()

        summary = run_full_sync(
            client=client, pool=pool, campus=campus, cursus=cursus,
            d_from=run.date_from, d_to=run.date_to,
            on_log=on_log, on_progress=on_progress, should_cancel=should_cancel,
        )
        run.finished_at = timezone.now()
        run.events_ingested = summary["total_events"]
        run.date_from, run.date_to = summary["d_from"], summary["d_to"]
        if summary["cancelled"]:
            run.status = SyncRun.Status.CANCELLED
            run.append_log(f"Annulé · {summary['total_events']} events déjà ingérés.")
        else:
            run.status = SyncRun.Status.DONE
            run.days_done = run.days_total = summary["total_days"]
            run.append_log(f"Terminé · {summary['total_events']} events ingérés.")
        run.save()
        return {"ok": True, "cancelled": summary["cancelled"], "events": summary["total_events"]}
    except Exception as ex:  # noqa: BLE001
        run.status = SyncRun.Status.ERROR
        run.finished_at = timezone.now()
        run.error = str(ex)
        run.append_log(f"ERREUR : {ex}")
        run.save()
        return {"error": str(ex)}


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
        apply_aura_penalty, apply_seniority, ensure_weekend_coefficients,
        randomize_daily_coefficient, randomize_daily_hosts,
    )
    out = {}
    for pool in Pool.objects.filter(is_active=True):
        coef = randomize_daily_coefficient(pool)   # random du jour courant
        ensure_weekend_coefficients(pool)          # écrase si le jour est un week-end
        hosts = randomize_daily_hosts(pool)        # 5 Bénites + 5 Maudites du jour
        weeks, _ = apply_seniority(pool)
        auras = apply_aura_penalty(pool)
        out[pool.slug] = {"coef": str(coef), "weeks": weeks,
                          "hosts": len(hosts["shiny"]) + len(hosts["cursed"]), "auras": auras}
    return out


@shared_task
def poll_42():
    """
    Polling périodique (cron */10) → actualise UNIQUEMENT les données du JOUR
    COURANT de la piscine active, sans jamais recalculer l'historique :
      - fetch_live ne récupère que today (logtimes + corrections, en parallèle) ;
      - sync_all ré-écrit l'agrégat du jour et dédupe → idempotent ;
      - aucun recompute_from / backfill n'est appelé ici.
    Les jours passés restent figés (snapshots) ; nightly_snapshot clôt la veille.
    No-op sans clés API.
    """
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
