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

        pool = run.pool
        if pool is None:
            campus, _ = resolve_target()
            pool, _ = get_or_create_pool(campus)
            run.pool = pool
        # Cible : les IDs PROPRES à la piscine priment sur les valeurs globales.
        settings_campus, settings_cursus = resolve_target()
        campus = pool.campus_id or settings_campus
        cursus = pool.cursus_id or settings_cursus
        if not campus:
            raise RuntimeError("Campus non défini (ni sur la piscine, ni dans settings).")
        run.append_log(f"Piscine cible : {pool.name} · campus {campus} · cursus {cursus}")
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
    Job de minuit (00:05) : FINALISE la veille (jour désormais terminé) pour
    chaque piscine active, puis la fige.
      1. re-fetch de la veille + scoring de la famille LOGTIME (score_cumulative) :
         c'est ICI que les points logtime tombent — jamais en cours de journée.
         Les autres syncs (feedbacks/évals/flags/places/exam) sont rejoués par
         sécurité (idempotents). `spread_plague` N'EST PAS rejoué (non idempotent,
         cf. mémoire projet — la propagation a déjà eu lieu en direct via poll_42) ;
      2. clôture (stacking, effets des élus, taxe du podium) sur les gains complets ;
      3. snapshot + verrouillage du coefficient.
    Idempotent & rejouable. Sans clés API : on fige simplement l'existant.
    """
    import logging

    from .chaos import apply_stacking
    from .derived import (apply_designation_effects, apply_exam_regression,
                          apply_podium_tax, _exam_days)
    from .sync import (_users, fetch_exam_windows, sync_evaluations, sync_exam_time,
                       sync_feedbacks, sync_flags, sync_host_effects, sync_last_day,
                       sync_locations)
    from .sync import fetch_day
    logger = logging.getLogger(__name__)
    yesterday = timezone.localdate() - timedelta(days=1)
    client = FtClient()
    summary = {}
    for pool in Pool.objects.filter(is_active=True):
        windows = []
        # 1) finalisation de la veille sur données complètes (logtime + discrets)
        if client.configured and pool.starts_on <= yesterday <= pool.ends_on:
            campus = pool.campus_id or settings.FT_CAMPUS_ID
            cursus = pool.cursus_id or settings.FT_CURSUS_ID
            try:
                p = fetch_day(client, campus, yesterday, cursus_id=cursus, parallel=True)
                users = _users(pool)
                sync_locations(pool, p["locations"], users, score_cumulative=True)
                sync_feedbacks(pool, p["feedbacks"], users)
                sync_evaluations(pool, p["evaluations"], users)
                sync_flags(pool, p["flags"], users)
                sync_host_effects(pool, yesterday, p["locations"], p.get("pairs", []), users)
                try:
                    windows = fetch_exam_windows(client, campus, cursus)
                except Exception:  # noqa: BLE001 — exam_time ignoré si l'endpoint échoue
                    windows = []
                sync_exam_time(pool, yesterday, p["locations"], windows, users)
                sync_last_day(pool, yesterday, p.get("pairs", []), users)
            except Exception as ex:  # noqa: BLE001 — fetch KO : on fige l'existant
                logger.warning("nightly finalize %s %s: %s", pool.slug, yesterday, ex)

        # 2) clôture (idempotente) sur les gains désormais complets
        apply_stacking(pool, yesterday)  # malus stacking sur le jour clôturé (opt-in)
        apply_designation_effects(pool, yesterday)  # ± % des gains des élus du jour
        apply_podium_tax(pool, yesterday)  # rubber-banding top 3 → bottom 10
        # 3) fige + verrouille
        count = snapshot_day(pool, yesterday)
        # régression aux examens (score < exam précédent) — après le snapshot
        apply_exam_regression(pool, yesterday, _exam_days(windows))
        DailyCoefficient.objects.filter(pool=pool, day=yesterday).update(locked=True)
        summary[pool.slug] = count
    return {"day": str(yesterday), "snapshots": summary}


@shared_task
def daily_derived():
    """
    Tirages AUTOMATIQUES du jour (cron 00:06) pour la piscine active, dans sa
    période uniquement (évite de polluer un pool de test) :
      - multiplicateurs par event (DailyEventMultiplier, only_missing = respecte
        un réglage manuel),
      - places Bénites/Maudites du jour,
      puis aura.
    """
    from .chaos import plague_endgame, seed_plague
    from .derived import (apply_aura_penalty, assign_daily_designations,
                          randomize_daily_hosts)
    from .services import randomize_day_multipliers
    today = timezone.localdate()
    out = {}
    for pool in Pool.objects.filter(is_active=True):
        if not (pool.starts_on <= today <= pool.ends_on):
            out[pool.slug] = "hors période — ignoré"
            continue
        seed_plague(pool)  # jour 1 : sème l'épidémie une seule fois (idempotent)
        mults = randomize_day_multipliers(pool, today, reseed=True, only_missing=True)
        hosts = randomize_daily_hosts(pool, today)
        desig = assign_daily_designations(pool, today)   # Maudits/Bénis DU JOUR
        auras = apply_aura_penalty(pool)
        entry = {"multiplicateurs": mults,
                 "places": len(hosts["shiny"]) + len(hosts["cursed"]),
                 "designations": len(desig["cursed"]) + len(desig["blessed"]),
                 "auras": auras}
        # Verdict Peste & Choléra : le DERNIER jour de la piscine uniquement
        if today == pool.ends_on:
            entry["plague_endgame"] = plague_endgame(pool)
        out[pool.slug] = entry
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
    from .sync import fetch_exam_windows
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
        campus = pool.campus_id or settings.FT_CAMPUS_ID
        cursus = pool.cursus_id or settings.FT_CURSUS_ID
        data = fetch_live(client, campus_id=campus, cursus_id=cursus)
        try:
            data["exam_windows"] = fetch_exam_windows(client, campus, cursus)
        except Exception:  # noqa: BLE001 — exam_time ignoré si l'endpoint échoue
            data["exam_windows"] = []
        out[pool.slug] = sync_all(pool, data)
    return out


@shared_task
def sync_campus_users():
    """
    Actualise quotidiennement la liste des étudiants de chaque piscine active.
    La cohorte (campus + année/mois) est dérivée de CHAQUE piscine — jamais des
    valeurs globales du .env : un .env pointant sur une vieille cohorte faisait
    fusionner les users de deux piscines (cf. incident 07/2026).
    """
    import calendar

    from .sync import fetch_campus_users, sync_users
    client = FtClient()
    if not client.configured:
        return {"skipped": "clés API manquantes"}
    out = {}
    for pool in Pool.objects.filter(is_active=True):
        campus = pool.campus_id or settings.FT_CAMPUS_ID
        if not campus:
            out[pool.slug] = "campus inconnu — ignoré"
            continue
        data = fetch_campus_users(
            client, campus,
            pool_year=str(pool.starts_on.year),
            pool_month=calendar.month_name[pool.starts_on.month].lower(),
        )
        out[pool.slug] = sync_users(pool, data)
    return out
