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
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from .models import AppUser, DailyCoefficient, DailySnapshot, EventLog

ZERO = Decimal("0")


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
    (Re)calcule les snapshots du jour, avec un multiplicateur PAR CATÉGORIE :
        day_final = Σgains×coef_gain + Σpertes×coef_loss + Σevents×coef_event (+ autres ×1)
    Idempotent ; suppose les jours antérieurs déjà à jour (cumul lu en base).
    """
    coef = get_coefficient(pool, day)
    cg, cl, ce = coef.coef_gain, coef.coef_loss, coef.coef_event

    agg = {
        r["user_id"]: r
        for r in EventLog.objects.filter(pool=pool, event_date=day, is_voided=False)
        .values("user_id").annotate(
            g=Sum("raw_points", filter=Q(rule__category="gain")),
            l=Sum("raw_points", filter=Q(rule__category="loss")),
            e=Sum("raw_points", filter=Q(rule__category="event")),
            o=Sum("raw_points", filter=Q(rule__isnull=True)),  # sans règle → coef 1
        )
    }
    existing = set(
        DailySnapshot.objects.filter(pool=pool, day=day).values_list("user_id", flat=True)
    )
    fp = _fingerprint(pool, day)

    rows = []
    for uid in set(agg) | existing:
        r = agg.get(uid)
        g = (r and r["g"]) or ZERO
        l = (r and r["l"]) or ZERO
        e = (r and r["e"]) or ZERO
        o = (r and r["o"]) or ZERO
        raw = g + l + e + o
        day_final = g * cg + l * cl + e * ce + o
        prev = (
            DailySnapshot.objects.filter(pool=pool, user_id=uid, day__lt=day)
            .order_by("-day").values_list("cumulative_total", flat=True).first()
        ) or ZERO
        rows.append((uid, raw, day_final, prev + day_final))

    rows.sort(key=lambda t: t[3], reverse=True)  # rang du jour par cumul
    for rank, (uid, raw, day_final, cum) in enumerate(rows, start=1):
        DailySnapshot.objects.update_or_create(
            pool=pool, user_id=uid, day=day,
            defaults=dict(
                day_raw_points=raw, day_coefficient=cg, day_final_points=day_final,
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
        coef = get_coefficient(pool, today)
        cg, cl, ce = coef.coef_gain, coef.coef_loss, coef.coef_event
        live = (
            EventLog.objects.filter(pool=pool, event_date=today, is_voided=False)
            .values("user_id").annotate(
                g=Sum("raw_points", filter=Q(rule__category="gain")),
                l=Sum("raw_points", filter=Q(rule__category="loss")),
                e=Sum("raw_points", filter=Q(rule__category="event")),
                o=Sum("raw_points", filter=Q(rule__isnull=True)),
            )
        )
        for r in live:
            day_final = (r["g"] or ZERO) * cg + (r["l"] or ZERO) * cl + (r["e"] or ZERO) * ce + (r["o"] or ZERO)
            latest[r["user_id"]] = latest.get(r["user_id"], ZERO) + day_final

    logins = dict(AppUser.objects.filter(id__in=latest).values_list("id", "login"))
    rows = [
        {"login": logins.get(uid, str(uid)), "total": float(total)}
        for uid, total in latest.items()
    ]
    rows.sort(key=lambda r: r["total"], reverse=True)
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
    return rows[:limit] if limit else rows
