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


def _draw_multiplier(rng, lo, hi):
    """
    Tirage « lisse » : distribution TRIANGULAIRE centrée sur le neutre (1.0),
    bornée à [lo, hi]. Résultat : la plupart des jours restent proches de 1,
    les multiplicateurs extrêmes sont rares → variance plus douce et pertinente
    qu'un uniforme plat. Si 1.0 est hors range, on centre sur la borne la plus
    proche.
    """
    if hi <= lo:
        return Decimal(str(round(lo, 2)))
    mode = min(max(1.0, lo), hi)
    return Decimal(str(round(rng.triangular(lo, hi, mode), 2)))


def randomize_day_multipliers(pool, day, rule_ids=None, reseed=True, only_missing=False):
    """
    Tire le multiplicateur de chaque event ACTIF pour ce jour dans sa range
    [mult_min, mult_max] (distribution triangulaire centrée, cf. _draw_multiplier).
    - only_missing=True : ne touche pas aux règles déjà réglées ce jour-là
      (respecte un réglage manuel du staff, idempotent pour la tâche de nuit).
    """
    rules = Rule.objects.filter(is_active=True)
    if rule_ids:
        rules = rules.filter(id__in=rule_ids)
    already = set()
    if only_missing:
        already = set(DailyEventMultiplier.objects.filter(pool=pool, day=day)
                      .values_list("rule_id", flat=True))
    n = 0
    for rule in rules:
        if rule.id in already:
            continue
        rng = random.Random(None if reseed else f"{pool.slug}:{day}:{rule.key}")
        val = _draw_multiplier(rng, float(rule.mult_min), float(rule.mult_max))
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

    # Cumuls J-1 de TOUS les users en 1 requête (avant : 1 requête par user).
    prev_map = dict(
        DailySnapshot.objects.filter(pool=pool, day__lt=day)
        .order_by("user_id", "-day").distinct("user_id")
        .values_list("user_id", "cumulative_total")
    )

    rows = []
    for uid in set(day_final) | existing:
        raw = day_raw.get(uid, ZERO)
        final = day_final.get(uid, ZERO)
        rows.append((uid, raw, final, prev_map.get(uid, ZERO) + final))

    rows.sort(key=lambda t: t[3], reverse=True)  # rang du jour par cumul
    DailySnapshot.objects.bulk_create(
        [DailySnapshot(pool=pool, user_id=uid, day=day,
                       day_raw_points=raw, day_coefficient=ONE, day_final_points=final,
                       cumulative_total=cum, rank=rank, rules_fingerprint=fp)
         for rank, (uid, raw, final, cum) in enumerate(rows, start=1)],
        update_conflicts=True, unique_fields=["user", "day"],
        update_fields=["day_raw_points", "day_coefficient", "day_final_points",
                       "cumulative_total", "rank", "rules_fingerprint", "pool"],
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


def reapply_rule_points(rule, upto=None):
    """
    Réapplique les paramètres ACTUELS d'une règle à TOUS ses logs (events non
    annulés, TOUTES piscines confondues), puis recalcule les snapshots +
    classement de chaque piscine touchée. Sert quand le staff édite les points
    d'une règle : chaque log historique est revu et corrigé, plus seulement les
    futurs events.

    Le hasard reste FIGÉ : on ne relance jamais les dés. Pour les règles
    aléatoires on réutilise le tirage stocké (random_roll) —
      - random_modifier : nouveau_base + delta figé ;
      - fixed [min,max]  : on conserve la valeur tirée (la range n'affecte que
        les futurs events).
    Les règles déterministes (fixed points, tiers, map, seuils, facteurs…) sont
    ré-évaluées avec les nouveaux paramètres.
    """
    from .engine import _d, evaluate
    from .models import Pool
    version = rule.current_version
    events = list(EventLog.objects.filter(rule=rule, is_voided=False))
    if not version or not events:
        return {"updated": 0, "days": 0, "pools": 0}

    params = version.params or {}
    etype = params.get("type", "fixed")
    is_rand_mod = etype == "random_modifier"
    is_fixed_range = etype == "fixed" and params.get("min") is not None

    def _luck(e):
        """
        Fraction de chance [0,1] figée de l'event. Stockée depuis peu (`luck`) ;
        pour les anciens events sans `luck`, on la dérive de façon DÉTERMINISTE
        de l'id (stable dans le temps) — on ne relance donc jamais aléatoirement.
        """
        r = e.random_roll or {}
        if r.get("luck") is not None:
            return float(r["luck"])
        return random.Random(f"{rule.key}:{e.id}").random()

    updated = []
    first_day = {}  # pool_id → 1er jour touché (borne du recompute)
    for e in events:
        new_roll = None
        if is_fixed_range:
            frac = _luck(e)
            lo, hi = float(params["min"]), float(params["max"])
            val = lo + frac * (hi - lo)
            new_pts = _d(val)
            new_roll = {"rolled": round(val, 2), "luck": round(frac, 6)}
        elif is_rand_mod:
            frac = _luck(e)
            lo, hi = float(params.get("rand_min", 0)), float(params.get("rand_max", 0))
            delta = lo + frac * (hi - lo)
            new_pts = _d(float(params.get("base", 0)) + delta)
            new_roll = {"delta": round(delta, 2), "luck": round(frac, 6)}
        else:
            new_pts = evaluate(version, e.raw_payload or {})["points"]

        changed = False
        if new_pts != e.raw_points:
            e.raw_points = new_pts
            changed = True
        if e.rule_version != version.version:
            e.rule_version = version.version
            changed = True
        if new_roll is not None and new_roll != (e.random_roll or {}):
            e.random_roll = new_roll
            changed = True
        if changed:
            updated.append(e)
            d = first_day.get(e.pool_id)
            if d is None or e.event_date < d:
                first_day[e.pool_id] = e.event_date

    if updated:
        EventLog.objects.bulk_update(
            updated, ["raw_points", "rule_version", "random_roll"], batch_size=500)
    total_days = 0
    for pool in Pool.objects.filter(id__in=first_day):
        total_days += len(recompute_from(pool, first_day[pool.id], upto=upto))
    return {"updated": len(updated), "days": total_days, "pools": len(first_day)}


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
