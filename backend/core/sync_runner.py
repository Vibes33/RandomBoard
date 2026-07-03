"""
Logique partagée de synchronisation/rejeu d'une piscine depuis l'API 42.

Source unique utilisée par :
  - la commande CLI `ft_replay` (sortie terminal),
  - la tâche Celery `run_sync` (suivi live via SyncRun dans le panel).

Le rejeu se fait jour par jour : locations + scale_teams → events datés →
snapshot du jour (comme le cron de minuit le ferait en live). Deux callbacks
optionnels remontent l'avancement :
  - on_log(str)                       → une ligne de journal
  - on_progress(day, index, total, events) → progression chiffrée
"""
import calendar
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.utils.text import slugify

from .ft_api import NETWORK_ERRORS, FtRateLimit, FtServerError
from .models import Pool
from .services import snapshot_day
from .sync import (
    fetch_campus_users, fetch_day,
    sync_evaluations, sync_feedbacks, sync_flags, sync_locations, sync_users,
)

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}


def resolve_target(campus=None, cursus=None):
    """Complète campus/cursus avec les valeurs de settings si non fournis."""
    campus = campus if campus is not None else settings.FT_CAMPUS_ID
    cursus = cursus if cursus is not None else settings.FT_CURSUS_ID
    return campus, cursus


def get_or_create_pool(campus, *, year=None, month=None, name=None, activate=True):
    """Récupère (ou crée) la piscine cible et l'active si demandé."""
    year = int(year or settings.FT_POOL_YEAR)
    month_name = month or settings.FT_POOL_MONTH
    m = MONTHS.get(str(month_name).lower(), 7)
    pool_name = name or f"Piscine {str(month_name).capitalize()} {year} (campus {campus})"
    pool, created = Pool.objects.get_or_create(
        slug=slugify(pool_name)[:50],
        defaults=dict(name=pool_name, starts_on=dt.date(year, m, 1),
                      ends_on=dt.date(year, m, 28), last_day=dt.date(year, m, 28),
                      is_active=True),
    )
    if activate and not pool.is_active:
        Pool.objects.exclude(pk=pool.pk).update(is_active=False)
        Pool.objects.filter(pk=pool.pk).update(is_active=True)
        pool.is_active = True
    return pool, created


def detect_dates(client, logins, cursus_id):
    """Récupère begin_at/end_at du cursus piscine depuis un inscrit."""
    for login in logins[:5]:
        try:
            u = client.get(f"/v2/users/{login}").json()
            for cu in (u.get("cursus_users") or []):
                if (cu.get("cursus") or {}).get("id") == cursus_id and cu.get("begin_at"):
                    b = dt.date.fromisoformat(cu["begin_at"][:10])
                    e = dt.date.fromisoformat((cu.get("end_at") or cu["begin_at"])[:10])
                    return b, e
        except Exception:  # noqa: BLE001
            continue
    return None, None


def run_full_sync(*, client, pool, campus, cursus, d_from=None, d_to=None,
                  skip_users=False, on_log=None, on_progress=None, should_cancel=None):
    """
    Rejoue/ingère `pool` jour par jour depuis l'API 42.
    `should_cancel()` (optionnel) est consulté entre chaque jour : renvoyer True
    interrompt proprement le rejeu. Retourne un résumé
    {d_from, d_to, total_days, total_events, users, cancelled}.
    """
    log = on_log or (lambda *_: None)
    prog = on_progress or (lambda **_: None)
    cancel = should_cancel or (lambda: False)

    # 1) étudiants de la session
    if not skip_users:
        data = fetch_campus_users(client, campus, pool_year=str(settings.FT_POOL_YEAR),
                                  pool_month=settings.FT_POOL_MONTH)
        res = sync_users(pool, data)
        log(f"Étudiants : {len(data)} ({res['created']} créés, {res['updated']} maj)")

    users = {u.login: u for u in pool.users.all()}

    # 2) dates : fournies, sinon détectées via le cursus, sinon celles du pool
    if not (d_from and d_to):
        d_from, d_to = detect_dates(client, list(users), cursus)
        if not d_from:
            d_from, d_to = pool.starts_on, pool.ends_on
        log(f"Dates piscine (cursus {cursus}) : {d_from} → {d_to}")
    if d_to < d_from:
        raise ValueError("date_to est avant date_from.")
    Pool.objects.filter(pk=pool.pk).update(starts_on=d_from, ends_on=d_to, last_day=d_to)
    pool.starts_on, pool.ends_on, pool.last_day = d_from, d_to, d_to

    # 3) rejeu par CHUNKS de jours : le FETCH (I/O, lent) est parallélisé, mais
    #    l'INGESTION reste séquentielle et ordonnée pour préserver la somme
    #    cumulée des snapshots (cumulative(J) = cumulative(J-1) + jour_J).
    total_days = (d_to - d_from).days + 1
    days = [d_from + dt.timedelta(days=i) for i in range(total_days)]
    workers = max(2, min(8, len(client.keys) * 2))  # calé sur le pool de clés
    log(f"Rejeu {d_from} → {d_to} · campus {campus} · {len(users)} étudiants "
        f"· fetch parallèle ×{workers}")
    fetch_errors = (FtRateLimit, FtServerError) + NETWORK_ERRORS
    idx, total_events, cancelled = 0, 0, False

    for c0 in range(0, total_days, workers):
        if cancel():
            log("Annulation demandée — arrêt du rejeu.")
            cancelled = True
            break
        chunk = days[c0:c0 + workers]
        # (a) FETCH parallèle du chunk (chaque jour = 1 thread ; les clés tournent)
        payloads = {}
        with ThreadPoolExecutor(max_workers=workers) as pool_ex:
            futs = {pool_ex.submit(fetch_day, client, campus, d, cursus): d for d in chunk}
            for fut in futs:
                d = futs[fut]
                try:
                    payloads[d] = fut.result()
                except fetch_errors as exf:
                    payloads[d] = exf  # jour en erreur → ignoré à l'ingestion
        # (b) INGESTION dans l'ordre chronologique
        for d in chunk:
            if cancel():
                cancelled = True
                break
            idx += 1
            p = payloads.get(d)
            if isinstance(p, Exception):
                log(f"{d} · API indisponible ({p}) — jour ignoré")
                prog(day=d, index=idx, total=total_days, events=total_events)
                continue
            n = (sync_locations(pool, p["locations"], users)
                 + sync_feedbacks(pool, p["feedbacks"], users)
                 + sync_evaluations(pool, p["evaluations"], users)
                 + sync_flags(pool, p["flags"], users))
            from .chaos import spread_plague
            spread_plague(pool, p.get("pairs", []))  # propagation Peste & Choléra
            snapshot_day(pool, d)  # fige le jour, comme le cron de minuit
            total_events += n
            log(f"{d} · {len(p['locations'])} loc / {len(p['feedbacks'])} fb / "
                f"{len(p['evaluations'])} év → {n} events")
            prog(day=d, index=idx, total=total_days, events=total_events)
        if cancelled:
            break

    return {"d_from": d_from, "d_to": d_to, "total_days": total_days,
            "total_events": total_events, "users": len(users), "cancelled": cancelled}
