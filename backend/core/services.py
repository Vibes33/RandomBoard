"""
Cœur de l'optimisation (design §3) — calcul des scores par snapshots.

Identité : Total(user) = Σ_jours [ Σ_actions(raw_points) × coefficient(jour) ]
Un jour clôturé est immuable → cumulative_total est une SOMME COURANTE :
    cumulative(J) = cumulative(J-1) + day_final_points(J)
On ne recalcule donc jamais depuis le début ; au pire on rejoue les jours "dirty".

Convention : on ne snapshot QUE les jours clôturés (strictement < aujourd'hui).
Le jour courant est toujours ajouté "en live" par standings().
"""
import hashlib
import random
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from .models import (
    AppUser, DailyCoefficient, DailyEventMultiplier, DailySnapshot, EventLog, Rule,
)

ZERO = Decimal("0")
ONE = Decimal("1")


def day_multipliers(pool, day):
    """Dict {rule_id: multiplicateur} pour un jour. Absent ⇒ 1."""
    return {
        rid: m for rid, m in
        DailyEventMultiplier.objects.filter(pool=pool, day=day).values_list("rule_id", "multiplier")
    }


def adjust_user_score(user, pool, points, reason="", staff=None):
    """Ajustement manuel de score par le staff → EventLog daté du jour (visible immédiatement)."""
    rule = Rule.objects.filter(key="manual_adjust").first()
    return EventLog.objects.create(
        user=user, pool=pool, event_type="manual_adjust", source=EventLog.Source.MANUAL,
        occurred_at=timezone.now(), event_date=timezone.localdate(),
        rule=rule, rule_version=(rule.current_version.version if rule and rule.current_version else None),
        raw_points=Decimal(str(points)),
        raw_payload={"reason": reason, "by": getattr(staff, "username", "")},
    )


def randomize_day_multipliers(pool, day, rule_ids=None, reseed=True):
    """Tire le multiplicateur de chaque event pour ce jour dans sa range [mult_min, mult_max]."""
    rules = Rule.objects.filter(is_active=True)
    if rule_ids:
        rules = rules.filter(id__in=rule_ids)
    n = 0
    for rule in rules:
        lo, hi = float(rule.mult_min), float(rule.mult_max)
        rng = random.Random(None if reseed else f"{pool.slug}:{day}:{rule.key}")
        val = Decimal(str(round(rng.uniform(lo, hi), 2))) if hi > lo else Decimal(str(lo))
        DailyEventMultiplier.objects.update_or_create(
            pool=pool, day=day, rule=rule, defaults={"multiplier": val})
        n += 1
    return n


def get_coefficient(pool, day):
    """Récupère (ou crée à 1.0) le coefficient d'un jour. Source de vérité unique."""
    coef, _ = DailyCoefficient.objects.get_or_create(
        pool=pool, day=day,
        defaults={"coefficient": Decimal("1"), "is_weekend": day.weekday() >= 5},
    )
    return coef


def _fingerprint(pool, day):
    """Hash des versions de règles utilisées ce jour → détection des jours 'dirty'."""
    versions = (
        EventLog.objects.filter(pool=pool, event_date=day, is_voided=False)
        .values_list("rule_id", "rule_version")
    )
    key = ";".join(
        f"{r}:{v}" for r, v in sorted(set(versions), key=lambda t: (str(t[0]), str(t[1])))
    )
    return hashlib.sha1(key.encode()).hexdigest()[:16]


@transaction.atomic
def snapshot_day(pool, day):
    """
    (Re)calcule les snapshots du jour : UN multiplicateur par (jour × type d'event).
        day_final = Σ events [ raw_points × multiplier(jour, règle) ]  (absent ⇒ ×1)
    Idempotent ; suppose les jours antérieurs déjà à jour (cumul lu en base).
    """
    mults = day_multipliers(pool, day)
    day_final = defaultdict(lambda: ZERO)
    day_raw = defaultdict(lambda: ZERO)
    for r in (EventLog.objects.filter(pool=pool, event_date=day, is_voided=False)
              .values("user_id", "rule_id").annotate(s=Sum("raw_points"))):
        s = r["s"] or ZERO
        m = mults.get(r["rule_id"], ONE)
        day_final[r["user_id"]] += s * m
        day_raw[r["user_id"]] += s

    existing = set(
        DailySnapshot.objects.filter(pool=pool, day=day).values_list("user_id", flat=True)
    )
    fp = _fingerprint(pool, day)

    rows = []
    for uid in set(day_final) | existing:
        raw = day_raw.get(uid, ZERO)
        final = day_final.get(uid, ZERO)
        prev = (
            DailySnapshot.objects.filter(pool=pool, user_id=uid, day__lt=day)
            .order_by("-day").values_list("cumulative_total", flat=True).first()
        ) or ZERO
        rows.append((uid, raw, final, prev + final))

    rows.sort(key=lambda t: t[3], reverse=True)  # rang du jour par cumul
    for rank, (uid, raw, final, cum) in enumerate(rows, start=1):
        DailySnapshot.objects.update_or_create(
            pool=pool, user_id=uid, day=day,
            defaults=dict(
                day_raw_points=raw, day_coefficient=ONE, day_final_points=final,
                cumulative_total=cum, rank=rank, rules_fingerprint=fp,
            ),
        )
    return len(rows)


def recompute_from(pool, from_day, upto=None):
    """
    Rejoue uniquement les jours [from_day .. upto] (défaut: jusqu'à hier).
    Coût = nb de jours affectés, jamais tout l'historique. Sert au cas 'dirty'
    (event tardif ou édition d'un jour passé par l'admin).
    """
    today = timezone.localdate()
    upto = upto or (today - timedelta(days=1))
    candidate_days = set(
        EventLog.objects.filter(pool=pool, event_date__gte=from_day, is_voided=False)
        .values_list("event_date", flat=True)
    ) | set(
        DailySnapshot.objects.filter(pool=pool, day__gte=from_day)
        .values_list("day", flat=True)
    )
    days = sorted(d for d in candidate_days if from_day <= d <= upto)
    for d in days:
        snapshot_day(pool, d)
    return days


def backfill(pool):
    """Recalcule tous les snapshots depuis le 1er event jusqu'à hier."""
    first = (
        EventLog.objects.filter(pool=pool, is_voided=False)
        .order_by("event_date").values_list("event_date", flat=True).first()
    )
    if not first:
        return []
    return recompute_from(pool, first)


def standings(pool, include_today=True, limit=None):
    """
    Classement courant = dernier cumul figé de chaque user (+ jour courant en live).
    Lecture rapide : on ne touche jamais à l'historique.
    """
    today = timezone.localdate()

    latest = {}
    for uid, cum in (
        DailySnapshot.objects.filter(pool=pool)
        .order_by("user_id", "-day")
        .values_list("user_id", "cumulative_total")
    ):
        latest.setdefault(uid, cum)  # 1re occurrence = jour le plus récent

    if include_today:
        mults = day_multipliers(pool, today)
        live = (
            EventLog.objects.filter(pool=pool, event_date=today, is_voided=False)
            .values("user_id", "rule_id").annotate(s=Sum("raw_points"))
        )
        add = defaultdict(lambda: ZERO)
        for r in live:
            add[r["user_id"]] += (r["s"] or ZERO) * mults.get(r["rule_id"], ONE)
        for uid, v in add.items():
            latest[uid] = latest.get(uid, ZERO) + v

    logins = dict(AppUser.objects.filter(id__in=latest).values_list("id", "login"))
    rows = [
        {"login": logins.get(uid, str(uid)), "total": float(total)}
        for uid, total in latest.items()
    ]
    rows.sort(key=lambda r: r["total"], reverse=True)
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
    return rows[:limit] if limit else rows
