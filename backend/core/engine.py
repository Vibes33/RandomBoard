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
import time as _time
from datetime import time
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

from .models import EventLog, Rule, RuleVersion

Q = Decimal("0.01")

# Cache court des (Rule, RuleVersion) par clé : record_event est appelé des
# milliers de fois par sync — sans cache c'est 2 SELECT par event. TTL court
# pour que les éditions du staff (autre process) soient reprises vite.
_RULE_CACHE = {}
_RULE_TTL = 30.0


def _cached_rule(rule_key):
    now = _time.monotonic()
    hit = _RULE_CACHE.get(rule_key)
    if hit and now - hit[0] < _RULE_TTL:
        return hit[1]
    rule = Rule.objects.filter(key=rule_key, is_active=True).first()
    rv = rule.current_version if rule else None
    _RULE_CACHE[rule_key] = (now, (rule, rv))
    return rule, rv


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
    """
    Points fixes, ou tirés dans [min,max] si la range est fournie. On stocke la
    « chance » sous forme de fraction [0,1] (`luck`) : elle permet de rééchelonner
    la valeur sur une NOUVELLE range lors d'une édition, sans relancer les dés.
    """
    if params.get("min") is not None and params.get("max") is not None:
        lo, hi = float(params["min"]), float(params["max"])
        frac = rng.random()
        v = lo + frac * (hi - lo)
        return _d(v), {"rolled": round(v, 2), "luck": round(frac, 6)}
    return _d(params.get("points", 0)), None


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
    """base + modificateur aléatoire figé (ex: projets, BSQ). `luck` = fraction
    [0,1] du tirage → rééchelonnable si la range d'aléa est éditée."""
    base = float(params.get("base", 0))
    lo, hi = float(params["rand_min"]), float(params["rand_max"])
    frac = rng.random()
    delta = lo + frac * (hi - lo)
    return _d(base + delta), {"delta": round(delta, 2), "luck": round(frac, 6)}


def ev_threshold_window(params, ctx, rng):
    """Dans [lo,hi] → in_points, sinon out_points (ex: durée éval BSQ 30–60 min)."""
    v = _num(ctx, params)
    inside = float(params["lo"]) <= v <= float(params["hi"])
    return _d(params.get("in_points", 0) if inside else params.get("out_points", 0)), None


def ev_multiplier(params, ctx, rng):
    """
    Multiplie une valeur du contexte (ex: streak × 10). `cap` optionnel :
    plafonne la valeur AVANT multiplication (ex: streak capé à 7 j → max 70)
    pour éviter les rentes à intérêts composés.
    """
    v = _num(ctx, params)
    if params.get("cap") is not None:
        v = min(v, float(params["cap"]))
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


def ev_tiers(params, ctx, rng):
    """
    Paliers par seuil : params['tiers'] = {seuil: points}. On prend le PLUS
    GRAND seuil ≤ valeur (floor lookup). Ex rush (note) : {"50": 50, "1": -50,
    "0": -150} → note 84 = +50, note 30 = −50, note 0 = −150.
    Configurable dans le panel comme une map seuil → points.
    """
    v = _num(ctx, params)
    best_thr, pts = None, params.get("default", 0)
    for thr, p in (params.get("tiers") or {}).items():
        t = float(thr)
        if v >= t and (best_thr is None or t > best_thr):
            best_thr, pts = t, p
    roll = {"matched": f"palier {best_thr:g}"} if best_thr is not None else None
    return _d(pts), roll


def ev_map_lookup(params, ctx, rng):
    """Cherche une clé du contexte dans une table (ex: flag de correction → points)."""
    key = str(ctx.get(params.get("key_field", "key"), "")).lower()
    for k, v in (params.get("map") or {}).items():
        if k.lower() == key:
            return _d(v), {"matched": k}
    return _d(params.get("default", 0)), None


def ev_level_week(params, ctx, rng):
    """
    Points par NIVEAU (map 'levels': niveau → points) multipliés par le
    multiplicateur de SEMAINE de piscine (map 'week_mult': semaine 1..4 → ×).
    Ex trolls : levels {"1": -10, "2": -20, "3": -30}, week_mult {"2": 1.5}.
    Le niveau et la semaine viennent du contexte ; maps éditables au panel.
    """
    lvl = str(int(float(ctx.get(params.get("level_key", "level"), 1) or 1)))
    base = float((params.get("levels") or {}).get(lvl, params.get("default", 0)))
    week = str(int(float(ctx.get("week", 1) or 1)))
    mult = float((params.get("week_mult") or {}).get(week, 1))
    return _d(base * mult), {"level": lvl, "week": week, "mult": mult}


EVALUATORS = {
    "fixed": ev_fixed,
    "linear_decay": ev_linear_decay,
    "linear_growth": ev_linear_growth,
    "random_modifier": ev_random_modifier,
    "threshold_window": ev_threshold_window,
    "multiplier": ev_multiplier,
    "time_window_bonus": ev_time_window_bonus,
    "from_context": ev_from_context,
    "weighted": ev_weighted,
    "keywords": ev_keywords,
    "map_lookup": ev_map_lookup,
    "tiers": ev_tiers,
    "level_week": ev_level_week,
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

    rule, rv = _cached_rule(rule_key)
    result = evaluate(rv, context, rng=rng)

    # Banni : peu importe gain ou perte, le mouvement devient une PERTE de la
    # même amplitude (marqué dans le payload pour la traçabilité des logs).
    points = result["points"]
    if getattr(user, "is_banned", False) and points > 0:
        points = -points
        context = {**(context or {}), "banned": True}

    event = EventLog.objects.create(
        user=user, pool=pool, event_type=rule_key, source=source,
        occurred_at=occurred_at, event_date=timezone.localdate(occurred_at),
        rule=rule, rule_version=rv.version if rv else None,
        raw_payload=context, raw_points=points, random_roll=result["roll"],
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
    _RULE_CACHE.pop(rule.key, None)  # invalide le cache local (autres process : TTL)
    return RuleVersion.objects.create(
        rule=rule, version=next_no, params=params, valid_from=now, created_by=user,
    )
