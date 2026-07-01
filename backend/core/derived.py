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
    AppUser, DailyCoefficient, EventLog, Rule, WeeklyDesignation,
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
def randomize_daily_coefficient(pool, day=None, seed=None):
    """
    Donne au jour un multiplicateur ALÉATOIRE dans la plage configurée
    (règle config_daily : coef_min/coef_max). Ne touche pas un jour déjà verrouillé
    ni un week-end (géré par ensure_weekend_coefficients). C'est ce random journalier
    qui fait qu'un même projet rendu un autre jour vaut plus ou moins de points.
    """
    day = day or timezone.localdate()
    if day.weekday() >= 5:
        return None  # le week-end a son propre facteur
    cfg = Rule.objects.filter(key="config_daily").first()
    params = (cfg.current_version.params if cfg and cfg.current_version else {})
    lo = float(params.get("coef_min", 0.8))
    hi = float(params.get("coef_max", 1.6))
    rng = random.Random(seed if seed is not None else f"{pool.slug}:{day.isoformat()}")
    value = Decimal(str(round(rng.uniform(lo, hi), 2)))

    obj, created = DailyCoefficient.objects.get_or_create(
        pool=pool, day=day, defaults={"coefficient": value, "is_weekend": False}
    )
    if not created and not obj.locked:
        obj.coefficient = value
        obj.save(update_fields=["coefficient"])
    return value


def ensure_weekend_coefficients(pool, upto=None):
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
                pool=pool, day=day, defaults={"coefficient": factor, "is_weekend": True}
            )
            if not created and not obj.locked and (obj.coefficient != factor or not obj.is_weekend):
                obj.coefficient = factor
                obj.is_weekend = True
                obj.save(update_fields=["coefficient", "is_weekend"])
                updated += 1
        day += timedelta(days=1)
    return factor, updated
