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
from .models import AppUser, Doctor, EventLog, Infection, PoolConfig, StackLedger
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
    # les docteurs sont immunisés : jamais patients zéro
    doctors = set(Doctor.objects.filter(pool=pool).values_list("user_id", flat=True))
    users = [u for u in AppUser.objects.filter(pool=pool, is_active=True)
             if u.id not in doctors]
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


def _spread(state, pairs, doctors=frozenset()):
    """
    PUR, une seule passe (chaque correction = un événement) :
      - DOCTEUR + infecté   → l'infecté est GUÉRI (le docteur est immunisé) ;
      - infecté + sain      → le sain prend la maladie (propagation) ;
      - Peste + Choléra     → les deux ÉCHANGENT de maladie ;
      - même maladie / sains → rien.
    Un docteur ne peut ni être infecté ni échanger : tout couple qui l'implique
    ne produit que des soins.
    state : {user_id: disease|None} (muté au fil des couples).
    pairs : [(a, b)] ou [(a, b, ref)] — ref = id scale_team (dédup des points).
    Renvoie [(user_id, disease, source_id, kind, ref)], kind ∈ {infect,swap,heal}.
    """
    changes = []
    for a, b, *rest in pairs:
        ref = rest[0] if rest else None
        da, db = state.get(a), state.get(b)
        if a in doctors or b in doctors:
            # immunité + soin au contact (deux docteurs ensemble : rien)
            if a in doctors and db:
                state[b] = None
                changes.append((b, None, a, "heal", ref))
            if b in doctors and da:
                state[a] = None
                changes.append((a, None, b, "heal", ref))
            continue
        if da and db:
            if da != db:  # choléra rencontre peste → échange
                state[a], state[b] = db, da
                changes.append((a, db, b, "swap", ref))
                changes.append((b, da, a, "swap", ref))
        elif da and not db:
            state[b] = da
            changes.append((b, da, a, "infect", ref))
        elif db and not da:
            state[a] = db
            changes.append((a, db, b, "infect", ref))
    return changes


def spread_plague(pool, pairs, day=None):
    """
    Applique propagation + échanges + SOINS sur des couples
    (correcteur_login, corrigé_login, scale_team_id).

    Les soins créent deux events de points (docteur + soigné), dédupliqués sur
    l'id de la correction : un rejeu ne verse jamais deux fois les mêmes points
    (la propagation, elle, reste non idempotente — cf. note projet).
    """
    if not Infection.objects.filter(pool=pool).exists():
        return 0
    users = {u.login: u for u in AppUser.objects.filter(pool=pool)}
    ids = {lg: u.id for lg, u in users.items()}
    id_pairs = [(ids[a], ids[b], (rest[0] if rest else None)) for a, b, *rest in pairs
                if a in ids and b in ids and ids[a] != ids[b]]
    doctors = set(Doctor.objects.filter(pool=pool).values_list("user_id", flat=True))
    by_id = {u.id: u for u in users.values()}
    state = dict(Infection.objects.filter(pool=pool).values_list("user_id", "disease"))
    day = day or timezone.localdate()
    for uid, disease, src, kind, ref in _spread(state, id_pairs, doctors):
        if kind == "infect":
            Infection.objects.get_or_create(
                pool=pool, user_id=uid, defaults={"disease": disease, "source_id": src})
        elif kind == "swap":
            Infection.objects.filter(pool=pool, user_id=uid).update(disease=disease)
        else:  # heal : guérison + points au soigné ET au docteur
            Infection.objects.filter(pool=pool, user_id=uid).delete()
            tag = ref if ref is not None else f"{src}-{uid}-{day}"
            patient, doc = by_id.get(uid), by_id.get(src)
            if patient:
                record_event(user=patient, pool=pool, rule_key="patient_healed",
                             occurred_at=_noon(day), source=SYSTEM,
                             context={"healed_by": doc.login if doc else None},
                             dedup_key=f"healed:{tag}:{uid}")
            if doc:
                record_event(user=doc, pool=pool, rule_key="doctor_heal",
                             occurred_at=_noon(day), source=SYSTEM,
                             context={"patient": patient.login if patient else None},
                             dedup_key=f"heal:{tag}:{uid}")
    return len(id_pairs)


# ─────────────────────────── 4. Docteurs ───────────────────────────
def appoint_doctor(pool, user):
    """
    Nomme un docteur (idempotent). Immunité immédiate : s'il était infecté, sa
    nomination le GUÉRIT (sans points — ce n'est pas un soin par correction).
    """
    doc, created = Doctor.objects.get_or_create(pool=pool, user=user)
    cured = Infection.objects.filter(pool=pool, user=user).delete()[0]
    return {"login": user.login, "created": created, "cured_on_appoint": bool(cured)}


def remove_doctor(pool, user):
    """Retire le rôle de docteur (l'étudiant redevient contaminable)."""
    n = Doctor.objects.filter(pool=pool, user=user).delete()[0]
    return {"login": user.login, "removed": bool(n)}


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
    docs = list(Doctor.objects.filter(pool=pool).select_related("user"))
    healed = EventLog.objects.filter(pool=pool, event_type="patient_healed",
                                     is_voided=False).count()
    return {
        "total": total, "infected": infected,
        "healthy": max(0, total - infected),
        "pct": round(100 * infected / total) if total else 0,
        "peste": p, "cholera": c, "leader": leader,
        "payout_day": str(pool.ends_on),
        "healed": healed,
        "doctors": [{"id": d.user_id, "login": d.user.login} for d in docs],
        "recent": [{"login": i.user.login, "disease": i.disease,
                    "source": i.source.login if i.source else None,
                    "zero": i.is_patient_zero}
                   for i in infections[:12]],
    }


def plague_endgame(pool):
    """
    Verdict de fin de piscine (versé le DERNIER jour, pool.ends_on) — JAMAIS de
    manière journalière. La coalition la PLUS NOMBREUSE l'emporte ; règlement
    ZÉRO-SOMME : chaque membre de la coalition PERDANTE perd `plague_payout`
    points, et ce total est redistribué à parts égales à la coalition GAGNANTE.

    → Non inflationniste (net nul), et forte variance pour la minorité malchanceuse
      (gros malus individuel) : l'aléatoire de l'épidémie décide qui plonge.
    Idempotent (void + ré-écriture + recompute) → rejouable après réglage.
    """
    from .services import recompute_from
    P, C = Infection.Disease.PESTE, Infection.Disease.CHOLERA
    peste = Infection.objects.filter(pool=pool, disease=P).count()
    cholera = Infection.objects.filter(pool=pool, disease=C).count()
    _void_all(pool, "plague_payout")  # nettoie tout verdict précédent (réglage/épidémie changés)
    # GARDE-FOU : le verdict ne tombe QUE le dernier jour (ou après, pour un
    # rejeu de piscine finie). Un full-sync / réglage panel en COURS de piscine
    # ne doit jamais l'émettre — l'épidémie n'est pas terminée.
    if timezone.localdate() < pool.ends_on:
        return {"ready": False, "too_early": True, "verdict_day": str(pool.ends_on)}
    if peste == 0 or cholera == 0:
        return {"ready": False}  # pas de duel s'il ne reste qu'une maladie (ou aucune)
    cfg = get_config(pool)
    winner, loser = (P, C) if peste >= cholera else (C, P)
    pay = pool.ends_on
    winners = list(Infection.objects.filter(pool=pool, disease=winner).select_related("user"))
    losers = list(Infection.objects.filter(pool=pool, disease=loser).select_related("user"))
    per_loser = float(cfg.plague_payout)
    pot = per_loser * len(losers)
    share = round(pot / len(winners), 2) if winners else 0.0
    for inf in losers:  # la coalition perdante paie
        record_event(user=inf.user, pool=pool, rule_key="plague_payout", occurred_at=_noon(pay),
                     context={"points": -per_loser}, source=SYSTEM)
    for inf in winners:  # redistribué aux gagnants (à parts égales)
        record_event(user=inf.user, pool=pool, rule_key="plague_payout", occurred_at=_noon(pay),
                     context={"points": share}, source=SYSTEM)
    recompute_from(pool, pay)
    return {"ready": True, "winner": winner, "winners": len(winners),
            "losers": len(losers), "per_loser": per_loser, "share": share, "day": str(pay)}
