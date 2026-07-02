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

from django.conf import settings
from django.utils import timezone
from django.utils.text import slugify

from .ft_api import FtRateLimit
from .models import Pool
from .services import snapshot_day
from .sync import (
    fetch_campus_users, fetch_locations_range, fetch_scale_teams_range,
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
                  skip_users=False, on_log=None, on_progress=None):
    """
    Rejoue/ingère `pool` jour par jour depuis l'API 42.
    Retourne un résumé {d_from, d_to, total_days, total_events, users}.
    """
    log = on_log or (lambda *_: None)
    prog = on_progress or (lambda **_: None)
    tz = timezone.get_current_timezone()

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

    # 3) rejeu jour par jour
    total_days = (d_to - d_from).days + 1
    log(f"Rejeu {d_from} → {d_to} · campus {campus} · {len(users)} étudiants")
    day, idx, total_events = d_from, 0, 0
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    while day <= d_to:
        idx += 1
        start = dt.datetime.combine(day, dt.time.min, tzinfo=tz).astimezone(dt.timezone.utc)
        end = start + dt.timedelta(days=1)
        try:
            s, e = start.strftime(fmt), end.strftime(fmt)
            locs = fetch_locations_range(client, campus, s, e)
            scale = fetch_scale_teams_range(client, campus, s, e, cursus_id=cursus)
            n = (sync_locations(pool, locs, users)
                 + sync_feedbacks(pool, scale["feedbacks"], users)
                 + sync_evaluations(pool, scale["evaluations"], users)
                 + sync_flags(pool, scale["flags"], users))
            snapshot_day(pool, day)  # fige le jour, comme le cron de minuit
            total_events += n
            log(f"{day} · {len(locs)} loc / {len(scale['feedbacks'])} fb / "
                f"{len(scale['evaluations'])} év → {n} events")
        except FtRateLimit as ex:
            log(f"{day} · rate-limit ({ex}) — jour ignoré")
        prog(day=day, index=idx, total=total_days, events=total_events)
        day += dt.timedelta(days=1)

    return {"d_from": d_from, "d_to": d_to, "total_days": total_days,
            "total_events": total_events, "users": len(users)}
