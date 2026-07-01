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

from django.utils import timezone

from .engine import record_event
from .models import (
    AppUser, DailyCoefficient, DailyHost, EventLog, Rule, WeeklyDesignation, Workstation,
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
def assign_designations(pool, n_cursed=1, n_blessed=1, when=None, seed=None):
    """Désigne aléatoirement des Maudits / Bénis pour la semaine."""
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
