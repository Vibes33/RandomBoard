"""
Moteur de règles (étape 2).

Principe : AUCUNE valeur n'est codée en dur. Chaque RuleVersion.params contient
un champ "type" qui désigne un évaluateur, plus ses paramètres. L'évaluateur
calcule les points à partir du contexte de l'événement.

Le random est FIGÉ à l'écriture (principe A) : evaluate() tire le hasard une
seule fois, renvoie le résultat ET le `roll`, qu'on stocke dans EventLog.
On ne ré-évalue JAMAIS au moment du calcul des scores — on lit raw_points.

Ajouter une règle = ajouter une ligne de config (params), pas du code.
Ajouter une *famille* de calcul = ajouter un évaluateur dans EVALUATORS.
"""
import random
from datetime import time
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

from .models import EventLog, Rule, RuleVersion

Q = Decimal("0.01")


def _d(x):
    return Decimal(str(x)).quantize(Q, rounding=ROUND_HALF_UP)


def _clamp01(x):
    return max(0.0, min(1.0, x))


def _num(context, params, default=0.0):
    """Lit la valeur numérique pointée par params['value_key'] dans le contexte."""
    return float(context.get(params.get("value_key", "value"), default))


# ─────────────────────────────────────────────────────────────
# Évaluateurs — chacun renvoie (points: Decimal, roll: dict|None)
# ─────────────────────────────────────────────────────────────
def ev_fixed(params, ctx, rng):
    return _d(params["points"]), None


def ev_linear_decay(params, ctx, rng):
    """Plus la valeur est BASSE, plus on gagne (ex: logtime bas)."""
    v = _num(ctx, params)
    lo, hi = float(params["min"]), float(params["max"])
    pmax, pmin = float(params.get("max_points", 100)), float(params.get("min_points", 0))
    if v < lo:
        return _d(pmax), None
    f = _clamp01((v - lo) / (hi - lo)) if hi > lo else 0.0
    return _d(pmax - f * (pmax - pmin)), None


def ev_linear_growth(params, ctx, rng):
    """Malus (ou bonus) croissant avec la valeur (ex: logtime haut)."""
    v = _num(ctx, params)
    lo, hi = float(params["min"]), float(params["max"])
    target = float(params.get("max_malus", params.get("max_points", -100)))
    if v < lo:
        return _d(0), None
    f = _clamp01((v - lo) / (hi - lo)) if hi > lo else 1.0
    return _d(f * target), None


def ev_random_modifier(params, ctx, rng):
    """base + modificateur aléatoire figé (ex: projets, BSQ)."""
    base = float(params.get("base", 0))
    delta = rng.uniform(float(params["rand_min"]), float(params["rand_max"]))
    return _d(base + delta), {"delta": round(delta, 2)}


def ev_probability(params, ctx, rng):
    """Avec proba p : points ; sinon else_points (ex: Randominette)."""
    roll = rng.random()
    hit = roll < float(params["proba"])
    pts = params["points"] if hit else params.get("else_points", 0)
    return _d(pts), {"roll": round(roll, 4), "hit": hit}


def ev_threshold_window(params, ctx, rng):
    """Dans [lo,hi] → in_points, sinon out_points (ex: durée éval BSQ 30–60 min)."""
    v = _num(ctx, params)
    inside = float(params["lo"]) <= v <= float(params["hi"])
    return _d(params.get("in_points", 0) if inside else params.get("out_points", 0)), None


def ev_multiplier(params, ctx, rng):
    """Multiplie une valeur du contexte (ex: dernier jour: corrections × 1000)."""
    v = _num(ctx, params)
    return _d(v * float(params["factor"])), None


def ev_time_window_bonus(params, ctx, rng):
    """Bonus si l'heure (HH:MM) est dans la fenêtre (ex: connexion 23:50–23:59)."""
    raw = ctx.get(params.get("value_key", "time"), "")
    try:
        hh, mm = map(int, str(raw).split(":")[:2])
        t = time(hh, mm)
    except (ValueError, TypeError):
        return _d(0), None
    s = time(*map(int, params["start"].split(":")))
    e = time(*map(int, params["end"].split(":")))
    return (_d(params["points"]) if s <= t <= e else _d(0)), None


def ev_from_context(params, ctx, rng):
    """Points fournis par le contexte (ex: bonus/malus d'un poste shiny/maudit)."""
    return _d(ctx.get(params.get("value_key", "points"), 0)), None


def ev_weighted(params, ctx, rng):
    """points × poids du contexte (ex: malédiction binôme, ×2 si éval croisée)."""
    weight = float(ctx.get("weight", 1))
    return _d(float(params["points"]) * weight), {"weight": weight}


def ev_keywords(params, ctx, rng):
    """
    Scanne un feedback et somme les points des mots-clés trouvés (config 'map').
    Cas spéciaux : 'quoi' SANS 'feur' et 'coubeh' = malus (+ perte de place si listé).
    Tout est dans params → ajouter un mot = éditer la règle, zéro code.
    """
    text = (ctx.get("comment", "") or "").lower()
    total = 0.0
    matched = []
    rank_penalty = 0
    penalty_on = params.get("rank_penalty_on", [])

    for kw, pts in (params.get("map") or {}).items():
        if kw.lower() in text:
            total += float(pts)
            matched.append(kw)
    if "quoi" in text and "feur" not in text:
        total += float(params.get("quoi_alone_points", 0))
        matched.append("quoi(seul)")
        if "quoi_alone" in penalty_on:
            rank_penalty = 1
    if "coubeh" in text:
        total += float(params.get("coubeh_points", 0))
        matched.append("coubeh")
        if "coubeh" in penalty_on:
            rank_penalty = 1

    roll = {"matched": matched}
    if rank_penalty:
        roll["rank_penalty"] = rank_penalty
    return _d(total), roll


def ev_map_lookup(params, ctx, rng):
    """Cherche une clé du contexte dans une table (ex: flag de correction → points)."""
    key = str(ctx.get(params.get("key_field", "key"), "")).lower()
    for k, v in (params.get("map") or {}).items():
        if k.lower() == key:
            return _d(v), {"matched": k}
    return _d(params.get("default", 0)), None


EVALUATORS = {
    "fixed": ev_fixed,
    "linear_decay": ev_linear_decay,
    "linear_growth": ev_linear_growth,
    "random_modifier": ev_random_modifier,
    "probability": ev_probability,
    "threshold_window": ev_threshold_window,
    "multiplier": ev_multiplier,
    "time_window_bonus": ev_time_window_bonus,
    "from_context": ev_from_context,
    "weighted": ev_weighted,
    "keywords": ev_keywords,
    "map_lookup": ev_map_lookup,
}


def evaluate(rule_version, context, rng=None):
    """
    Calcule les points d'un événement à partir d'une RuleVersion et d'un contexte.
    Renvoie {"points": Decimal, "roll": dict|None, "rank_penalty": int}.
    """
    if rule_version is None:
        return {"points": _d(0), "roll": None, "rank_penalty": 0}
    params = rule_version.params or {}
    etype = params.get("type", "fixed")
    fn = EVALUATORS.get(etype)
    if fn is None:
        raise ValueError(f"Type d'évaluateur inconnu: '{etype}' (règle v{rule_version.version})")
    rng = rng or random.Random()
    points, roll = fn(params, context, rng)
    rank_penalty = int(params.get("rank_penalty", 0))
    if roll and "rank_penalty" in roll:  # certains évaluateurs décident dynamiquement
        rank_penalty += int(roll["rank_penalty"])
    return {"points": points, "roll": roll, "rank_penalty": rank_penalty}


# ─────────────────────────────────────────────────────────────
# Ingestion : le pont que l'API 42 (étape 4) appellera
# ─────────────────────────────────────────────────────────────
def record_event(*, user, pool, rule_key, occurred_at, context,
                 source=EventLog.Source.API_42, dedup_key=None, rng=None):
    """
    Évalue une observation via la règle active et crée l'EventLog correspondant
    (random figé). Applique aussi les effets de bord (pénalité de rang).
    Idempotent si dedup_key est fourni.
    """
    if dedup_key and EventLog.objects.filter(dedup_key=dedup_key).exists():
        return None

    rule = Rule.objects.filter(key=rule_key, is_active=True).first()
    rv = rule.current_version if rule else None
    result = evaluate(rv, context, rng=rng)

    event = EventLog.objects.create(
        user=user, pool=pool, event_type=rule_key, source=source,
        occurred_at=occurred_at, event_date=timezone.localdate(occurred_at),
        rule=rule, rule_version=rv.version if rv else None,
        raw_payload=context, raw_points=result["points"], random_roll=result["roll"],
        dedup_key=dedup_key,
    )
    if result["rank_penalty"]:
        user.rank_modifier = (user.rank_modifier or 0) + result["rank_penalty"]
        user.save(update_fields=["rank_modifier"])
    return event


def new_rule_version(rule, params, user=None):
    """
    Modifie une règle = clôturer la version courante et en créer une nouvelle
    (versioning temporel, design §4). Le passé figé n'est PAS impacté.
    """
    now = timezone.now()
    current = rule.current_version
    next_no = (current.version + 1) if current else 1
    if current:
        current.valid_to = now
        current.save(update_fields=["valid_to"])
    return RuleVersion.objects.create(
        rule=rule, version=next_no, params=params, valid_from=now, created_by=user,
    )
