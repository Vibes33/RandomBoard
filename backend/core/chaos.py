"""
Chaos absolu (étape 4.2).

  - La Peste & le Choléra : 8 patients zéro (4+4) tirés au jour 1, propagation
    par correction (échange si Peste rencontre Choléra). Boost versé 1 jour avant
    l'exam final à la coalition la PLUS NOMBREUSE ; seul réglage = points par
    personne (PoolConfig.plague_payout).
  - Stacking (opt-in, conservé) : malus quotidien à qui ne corrige pas + buff
    final au plus gros stackeur.

La fonction pure `_spread` est isolée pour être testable sans base.
"""
import random
from datetime import datetime, time as dtime, timedelta
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from .engine import record_event
from .models import AppUser, EventLog, Infection, PoolConfig, StackLedger
from .services import standings

SYSTEM = EventLog.Source.SYSTEM


def get_config(pool):
    cfg, _ = PoolConfig.objects.get_or_create(pool=pool)
    return cfg


def _noon(day):
    return timezone.make_aware(datetime.combine(day, dtime(12, 0)))


def _void_daily(user_id, pool, event_type, day):
    EventLog.objects.filter(user_id=user_id, pool=pool, event_type=event_type,
                            event_date=day, is_voided=False).update(is_voided=True)


def _void_all(pool, event_type):
    """Annule tous les events d'un type pour la piscine (avant ré-écriture globale)."""
    EventLog.objects.filter(pool=pool, event_type=event_type, is_voided=False).update(is_voided=True)


# ─────────────────────────── 1. Stacking (opt-in) ───────────────────────────
def apply_stacking(pool, day):
    """
    Retire stacking_penalty_pct % des points GAGNÉS le jour aux étudiants qui
    n'ont PAS corrigé (aucun feedback émis ce jour). Le montant retenu s'ajoute
    à leur stack (StackLedger). Opt-in (cfg.stacking_active).
    """
    cfg = get_config(pool)
    if not cfg.stacking_active or cfg.stacking_penalty_pct <= 0:
        return 0
    pct = float(cfg.stacking_penalty_pct) / 100.0
    corrected = set(EventLog.objects.filter(
        pool=pool, event_date=day, event_type="feedback_keywords", is_voided=False
    ).values_list("user_id", flat=True))
    gains = (EventLog.objects.filter(pool=pool, event_date=day, is_voided=False,
                                     raw_points__gt=0)
             .exclude(event_type__in=["stacking_penalty", "manual_adjust"])
             .values("user_id").annotate(s=Sum("raw_points")))
    users = {u.id: u for u in AppUser.objects.filter(pool=pool)}
    n = 0
    for g in gains:
        uid = g["user_id"]
        if uid in corrected or uid not in users:
            continue
        withheld = round(float(g["s"]) * pct, 2)
        if withheld <= 0:
            continue
        _void_daily(uid, pool, "stacking_penalty", day)
        record_event(user=users[uid], pool=pool, rule_key="stacking_penalty",
                     occurred_at=_noon(day), context={"points": -withheld}, source=SYSTEM)
        led, _ = StackLedger.objects.get_or_create(pool=pool, user_id=uid)
        led.total_stacked = led.total_stacked + Decimal(str(withheld))
        led.save(update_fields=["total_stacked"])
        n += 1
    return n


def stacking_endgame(pool):
    """Fin de piscine : le plus gros stackeur reçoit le buff (stacking_endgame_buff)."""
    cfg = get_config(pool)
    if not cfg.stacking_active:
        return None
    top = (StackLedger.objects.filter(pool=pool, total_stacked__gt=0)
           .order_by("-total_stacked").select_related("user").first())
    if not top:
        return None
    _void_daily(top.user_id, pool, "stacking_buff", pool.ends_on)
    record_event(user=top.user, pool=pool, rule_key="stacking_buff",
                 occurred_at=_noon(pool.ends_on),
                 context={"points": float(cfg.stacking_endgame_buff)}, source=SYSTEM)
    return top.user.login


# ─────────────────────────── 3. La Peste & le Choléra ───────────────────────────
def seed_plague(pool, n_each=4):
    """
    Infecte 4 Peste + 4 Choléra (patients zéro) tirés au hasard. Appelé
    automatiquement au jour 1 ; ne fait rien si déjà semé (pas de reset).
    """
    cfg = get_config(pool)
    if cfg.plague_seeded:
        return {"already": True}
    users = list(AppUser.objects.filter(pool=pool, is_active=True))
    random.shuffle(users)
    picks = users[:2 * n_each]
    rows = [
        Infection(pool=pool, user=u, is_patient_zero=True,
                  disease=Infection.Disease.PESTE if i < n_each else Infection.Disease.CHOLERA)
        for i, u in enumerate(picks)
    ]
    Infection.objects.bulk_create(rows, ignore_conflicts=True)
    cfg.plague_seeded = True
    cfg.save(update_fields=["plague_seeded"])
    return {"peste": min(n_each, len(picks)), "cholera": max(0, len(picks) - n_each)}


def _spread(state, pairs):
    """
    PUR, une seule passe (chaque correction = un événement) :
      - infecté + sain      → le sain prend la maladie (propagation) ;
      - Peste + Choléra     → les deux ÉCHANGENT de maladie ;
      - même maladie / sains → rien.
    state : {user_id: disease|None} (muté au fil des couples). pairs : [(a,b), …].
    Renvoie [(user_id, disease, source_id, kind)] où kind ∈ {"infect","swap"}.
    """
    changes = []
    for a, b in pairs:
        da, db = state.get(a), state.get(b)
        if da and db:
            if da != db:  # choléra rencontre peste → échange
                state[a], state[b] = db, da
                changes.append((a, db, b, "swap"))
                changes.append((b, da, a, "swap"))
        elif da and not db:
            state[b] = da
            changes.append((b, da, a, "infect"))
        elif db and not da:
            state[a] = db
            changes.append((a, db, b, "infect"))
    return changes


def spread_plague(pool, pairs):
    """Applique propagation + échanges sur des couples (correcteur_login, corrigé_login)."""
    if not Infection.objects.filter(pool=pool).exists():
        return 0
    users = {u.login: u.id for u in AppUser.objects.filter(pool=pool)}
    id_pairs = [(users[a], users[b]) for a, b, *_ in pairs
                if a in users and b in users and users[a] != users[b]]
    state = dict(Infection.objects.filter(pool=pool).values_list("user_id", "disease"))
    for uid, disease, src, kind in _spread(state, id_pairs):
        if kind == "infect":
            Infection.objects.get_or_create(
                pool=pool, user_id=uid, defaults={"disease": disease, "source_id": src})
        else:  # swap : on met simplement à jour la maladie
            Infection.objects.filter(pool=pool, user_id=uid).update(disease=disease)
    return len(id_pairs)


def plague_stats(pool):
    """Stats temps réel pour l'onglet panel."""
    total = AppUser.objects.filter(pool=pool, is_active=True).count()
    infections = list(Infection.objects.filter(pool=pool).select_related("user"))
    board = {r["login"]: r["total"] for r in standings(pool, include_today=True)}
    groups = {}
    for dis in (Infection.Disease.PESTE, Infection.Disease.CHOLERA):
        members = [i for i in infections if i.disease == dis]
        groups[dis] = {
            "count": len(members),
            "points": round(sum(board.get(i.user.login, 0) for i in members)),
            "patient_zero": sorted(i.user.login for i in members if i.is_patient_zero),
        }
    infected = len(infections)
    # coalition en tête = celle qui a le PLUS DE PERSONNES (c'est elle qui gagne)
    p, c = groups[Infection.Disease.PESTE], groups[Infection.Disease.CHOLERA]
    leader = None
    if infected:
        leader = (Infection.Disease.PESTE if p["count"] > c["count"]
                  else Infection.Disease.CHOLERA if c["count"] > p["count"] else "égalité")
    return {
        "total": total, "infected": infected,
        "healthy": max(0, total - infected),
        "pct": round(100 * infected / total) if total else 0,
        "peste": p, "cholera": c, "leader": leader,
        "payout_day": str(pool.ends_on - timedelta(days=1)),
        "recent": [{"login": i.user.login, "disease": i.disease,
                    "source": i.source.login if i.source else None,
                    "zero": i.is_patient_zero}
                   for i in infections[:12]],
    }


def plague_endgame(pool):
    """
    Boost de fin, versé 1 JOUR AVANT l'exam final : la coalition la PLUS
    NOMBREUSE fait gagner plague_payout points À CHACUN de ses membres.
    Idempotent (ré-écrit les events) → rejouable après un changement de réglage.
    """
    from .services import recompute_from
    peste = Infection.objects.filter(pool=pool, disease=Infection.Disease.PESTE).count()
    cholera = Infection.objects.filter(pool=pool, disease=Infection.Disease.CHOLERA).count()
    if peste == 0 and cholera == 0:
        return {"ready": False}
    cfg = get_config(pool)
    winner = Infection.Disease.PESTE if peste >= cholera else Infection.Disease.CHOLERA
    payout_day = pool.ends_on - timedelta(days=1)
    members = Infection.objects.filter(pool=pool, disease=winner).select_related("user")
    # on nettoie tout ancien payout (l'autre coalition a pu passer devant / réglage changé)
    _void_all(pool, "plague_payout")
    for inf in members:
        record_event(user=inf.user, pool=pool, rule_key="plague_payout",
                     occurred_at=_noon(payout_day),
                     context={"points": float(cfg.plague_payout)}, source=SYSTEM)
    recompute_from(pool, payout_day)
    return {"ready": True, "winner": winner, "paid": members.count(), "day": str(payout_day)}
