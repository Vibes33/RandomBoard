"""
Règles avancées / dérivées (étape 5).

Deux familles :
  - CONTEXTUELLES : la malédiction des binômes & les postes spéciaux passent par
    le moteur avec un contexte enrichi (poids ×2, points du host…).
  - PÉRIODIQUES : aura (1er de coalition), malus d'ancienneté, coefficient
    week-end, désignation hebdo des maudits/bénis — calculées par des tâches.

Tout reste config-driven : les points vivent dans les RuleVersion / HostConfig.
"""
import random
from collections import defaultdict
from datetime import datetime, time as dtime, timedelta
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from .engine import record_event
from .models import (
    AppUser, DailyCoefficient, DailyDesignation, DailyHost, EventLog, Rule,
    WeeklyDesignation, Workstation,
)
from .services import standings
from .sync import _void_daily

SYSTEM = EventLog.Source.SYSTEM


def week_start(d=None):
    d = d or timezone.localdate()
    return d - timedelta(days=d.weekday())


def _noon(day):
    return timezone.make_aware(datetime.combine(day, dtime(12, 0)))


# ─────────────────────────────────────────────────────────────
# Malédiction des binômes
# ─────────────────────────────────────────────────────────────
def assign_daily_designations(pool, day=None, n_cursed=3, n_blessed=3, reseed=False):
    """
    Tire au sort CHAQUE JOUR les étudiants Maudits / Bénis du jour.
    reseed=False → déterministe par (pool, jour) donc idempotent pour la tâche
    de nuit ; reseed=True → nouveau tirage (action manuelle du panel).
    """
    day = day or timezone.localdate()
    users = list(AppUser.objects.filter(pool=pool, is_active=True))
    rng = random.Random(None if reseed else f"{pool.slug}:{day.isoformat()}:designations")
    rng.shuffle(users)
    cursed, blessed = users[:n_cursed], users[n_cursed:n_cursed + n_blessed]

    DailyDesignation.objects.filter(pool=pool, day=day).delete()
    DailyDesignation.objects.bulk_create(
        [DailyDesignation(pool=pool, user=u, day=day, status="cursed") for u in cursed]
        + [DailyDesignation(pool=pool, user=u, day=day, status="blessed") for u in blessed]
    )
    return {"cursed": [u.login for u in cursed], "blessed": [u.login for u in blessed]}


def _rule_params(key):
    r = Rule.objects.filter(key=key, is_active=True).first()
    return (r.current_version.params if r and r.current_version else {}) or {}


def apply_designation_effects(pool, day):
    """
    Donne un EFFET RÉEL aux Maudits/Bénis du jour (avant : purement cosmétique) :
    Béni → +pct % de ses gains du jour, Maudit → −pct %. Appliqué à la clôture
    du jour (nightly / rejeu), APRÈS l'ingestion des events. Idempotent
    (agrégat journalier voidé puis réécrit).
    """
    desigs = list(DailyDesignation.objects.filter(pool=pool, day=day).select_related("user"))
    if not desigs:
        return 0
    gains = {r["user_id"]: float(r["s"] or 0) for r in (
        EventLog.objects.filter(pool=pool, event_date=day, is_voided=False,
                                raw_points__gt=0)
        .exclude(event_type__in=("daily_blessed", "daily_cursed", "manual_adjust"))
        .values("user_id").annotate(s=Sum("raw_points")))}
    created = 0
    for des in desigs:
        blessed = des.status == "blessed"
        key = "daily_blessed" if blessed else "daily_cursed"
        pct = float(_rule_params(key).get("pct", 30))
        base = gains.get(des.user_id, 0.0)
        pts = round(base * pct / 100, 2)
        _void_daily(des.user, pool, key, day)
        if pts <= 0:
            continue  # rien gagné ce jour → pas d'effet
        record_event(user=des.user, pool=pool, rule_key=key, occurred_at=_noon(day),
                     context={"points": pts if blessed else -pts, "pct": pct},
                     source=SYSTEM)
        created += 1
    return created


def apply_randominette(pool, day):
    """
    Randominette (mécanisme de comeback) : pile ou face quotidien pour la
    MOITIÉ BASSE du classement (à J-1) — pile +points, face else_points.
    Dedup par (login, jour) → idempotente au re-sync (pas de re-tirage).
    """
    board = standings(pool, include_today=False)  # classement figé à J-1
    if len(board) < 4:
        return 0
    bottom = {r["login"] for r in board[len(board) // 2:]}
    users = AppUser.objects.filter(pool=pool, is_active=True, login__in=bottom)
    created = 0
    for u in users:
        if record_event(user=u, pool=pool, rule_key="randominette",
                        occurred_at=_noon(day), context={}, source=SYSTEM,
                        dedup_key=f"rando:{u.login}:{day.isoformat()}"):
            created += 1
    return created


def assign_designations(pool, n_cursed=1, n_blessed=1, when=None, seed=None):
    """[legacy] Désignait des Maudits / Bénis pour la SEMAINE (remplacé par le
    tirage quotidien assign_daily_designations)."""
    ws = week_start(when)
    users = list(AppUser.objects.filter(pool=pool, is_active=True))
    random.Random(seed).shuffle(users)
    WeeklyDesignation.objects.filter(pool=pool, week_start=ws).delete()

    chosen = []
    for u in users[:n_cursed]:
        WeeklyDesignation.objects.create(pool=pool, user=u, week_start=ws,
                                         status=WeeklyDesignation.Status.CURSED, factor=2)
        chosen.append((u.login, "cursed"))
    for u in users[n_cursed:n_cursed + n_blessed]:
        WeeklyDesignation.objects.create(pool=pool, user=u, week_start=ws,
                                         status=WeeklyDesignation.Status.BLESSED, factor=2)
        chosen.append((u.login, "blessed"))
    return chosen


def _designation(pool, user, ws):
    return WeeklyDesignation.objects.filter(pool=pool, user=user, week_start=ws).first()


def apply_binome_effects(pool, pairs, when=None):
    """
    pairs : [{id, corrector_login, corrected_login}].
    Si un participant est désigné → effet. Si LES DEUX le sont (éval croisée) → ×2.
    """
    ws = week_start(when)
    users = {u.login: u for u in AppUser.objects.filter(pool=pool)}
    created = 0
    for pr in pairs:
        a, b = users.get(pr.get("corrector_login")), users.get(pr.get("corrected_login"))
        if not a or not b:
            continue
        da, db = _designation(pool, a, ws), _designation(pool, b, ws)
        weight = 2 if (da and db) else 1
        for user, des, other in ((a, da, b), (b, db, a)):
            if not des:
                continue
            key = "binome_cursed" if des.status == WeeklyDesignation.Status.CURSED else "binome_blessed"
            if record_event(user=user, pool=pool, rule_key=key, occurred_at=timezone.now(),
                            context={"weight": weight, "counterpart": other.login}, source=SYSTEM,
                            dedup_key=f"binome:{pr.get('id')}:{user.login}"):
                created += 1
    return created


# ─────────────────────────────────────────────────────────────
# Aura : 1er de coalition → malus monumental
# ─────────────────────────────────────────────────────────────
def apply_aura_penalty(pool, day=None):
    day = day or timezone.localdate()
    board = standings(pool, include_today=True)
    users = {u.login: u for u in AppUser.objects.filter(pool=pool)}

    by_coal = defaultdict(list)
    for r in board:
        u = users.get(r["login"])
        if u and u.coalition:
            by_coal[u.coalition].append((r["total"], u))

    created = 0
    for members in by_coal.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda t: t[0], reverse=True)
        leader = members[0][1]
        _void_daily(leader, pool, "aura_first_coalition", day)
        record_event(user=leader, pool=pool, rule_key="aura_first_coalition",
                     occurred_at=_noon(day), context={}, source=SYSTEM)
        created += 1
    return created


# ─────────────────────────────────────────────────────────────
# Malus d'ancienneté (croît avec les semaines de Piscine)
# ─────────────────────────────────────────────────────────────
def apply_seniority(pool, day=None):
    day = day or timezone.localdate()
    weeks = max(0, (day - pool.starts_on).days // 7)
    created = 0
    for u in AppUser.objects.filter(pool=pool, is_active=True):
        _void_daily(u, pool, "seniority_malus", day)
        record_event(user=u, pool=pool, rule_key="seniority_malus", occurred_at=_noon(day),
                     context={"weeks": weeks}, source=SYSTEM)
        created += 1
    return weeks, created


# ─────────────────────────────────────────────────────────────
# Coefficient week-end (pénalisant, config dans la règle config_weekend)
# ─────────────────────────────────────────────────────────────
_DEFAULT_RANGES = {"gain": (1.0, 1.5), "loss": (1.0, 2.0), "event": (0.8, 1.4)}


def _daily_ranges():
    """Ranges [min,max] par catégorie, depuis la règle config_daily (éditable en admin)."""
    cfg = Rule.objects.filter(key="config_daily").first()
    params = (cfg.current_version.params if cfg and cfg.current_version else {})
    out = {}
    for cat, default in _DEFAULT_RANGES.items():
        r = params.get(cat) or {}
        out[cat] = (float(r.get("min", default[0])), float(r.get("max", default[1])))
    return out


def randomize_daily_hosts(pool, day=None, n=5, reseed=False):
    """
    Tire au sort n places Bénites (shiny) + n Maudites parmi les postes connus,
    pour CE jour. reseed=True ⇒ nouveau tirage ; False ⇒ déterministe par jour.
    """
    day = day or timezone.localdate()
    hosts = list(Workstation.objects.filter(pool=pool).values_list("hostname", flat=True))
    if len(hosts) < 2:
        return {"shiny": [], "cursed": []}
    rng = random.Random(None if reseed else f"{pool.slug}:{day.isoformat()}:hosts")
    rng.shuffle(hosts)
    take = min(n, len(hosts) // 2)
    shiny, cursed = hosts[:take], hosts[take:2 * take]

    DailyHost.objects.filter(pool=pool, day=day).delete()
    DailyHost.objects.bulk_create(
        [DailyHost(pool=pool, day=day, hostname=h, kind="shiny") for h in shiny]
        + [DailyHost(pool=pool, day=day, hostname=h, kind="cursed") for h in cursed]
    )
    return {"shiny": shiny, "cursed": cursed}


def randomize_daily_coefficient(pool, day=None, reseed=False):
    """
    Tire un multiplicateur aléatoire PAR CATÉGORIE dans les ranges configurées.
    - reseed=False : déterministe par (pool, jour, catégorie) → idempotent (tâche de nuit).
    - reseed=True  : nouveau tirage à chaque appel (action admin « re-randomiser »).
    Respecte les jours verrouillés (non modifiés).
    """
    day = day or timezone.localdate()
    obj, _ = DailyCoefficient.objects.get_or_create(pool=pool, day=day)
    if obj.locked:
        return None
    ranges = _daily_ranges()
    vals = {}
    for cat, (lo, hi) in ranges.items():
        rng = random.Random(None if reseed else f"{pool.slug}:{day.isoformat()}:{cat}")
        vals[cat] = Decimal(str(round(rng.uniform(lo, hi), 2)))
    obj.coef_gain, obj.coef_loss, obj.coef_event = vals["gain"], vals["loss"], vals["event"]
    obj.is_weekend = day.weekday() >= 5
    obj.save(update_fields=["coef_gain", "coef_loss", "coef_event", "is_weekend"])
    return vals


def ensure_weekend_coefficients(pool, upto=None):
    """Le week-end pénalise les GAINS (coef_gain = facteur), pertes/events inchangés."""
    upto = upto or timezone.localdate()
    cfg = Rule.objects.filter(key="config_weekend").first()
    factor = Decimal(str(
        (cfg.current_version.params.get("factor", 0.5)) if cfg and cfg.current_version else 0.5
    ))
    updated = 0
    day = pool.starts_on
    while day <= upto:
        if day.weekday() >= 5:
            obj, created = DailyCoefficient.objects.get_or_create(
                pool=pool, day=day,
                defaults={"coef_gain": factor, "is_weekend": True},
            )
            if not created and not obj.locked and (obj.coef_gain != factor or not obj.is_weekend):
                obj.coef_gain = factor
                obj.is_weekend = True
                obj.save(update_fields=["coef_gain", "is_weekend"])
                updated += 1
        day += timedelta(days=1)
    return factor, updated
