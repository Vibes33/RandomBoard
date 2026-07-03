"""
Chaos absolu (étape 4.2).

Trois mécaniques, toutes pilotées par PoolConfig (effets score opt-in) :
  1. Multiplicateur de classement (rubber-band) : appliqué dans snapshot_day.
  2. Stacking : malus quotidien à qui ne corrige pas + buff final au plus gros
     stackeur.
  3. La Peste & le Choléra : 8 patients zéro, propagation par correction, payout
     de fin quand 100 % sont infectés.

Les fonctions « pures » (_rank_multiplier, _spread) sont isolées pour être
testables sans base.
"""
import random
from datetime import datetime, time as dtime
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


# ─────────────────────────── 1. Multiplicateur de classement ───────────────────────────
def _rank_multiplier(rank, n, first, last):
    """Interpole linéairement : rang 1 → first, rang n → last (dernier = max)."""
    first, last = float(first), float(last)
    if n <= 1:
        return first
    return first + ((rank - 1) / (n - 1)) * (last - first)


# ─────────────────────────── 2. Stacking ───────────────────────────
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
def seed_plague(pool, n_each=4, reseed=False):
    """Infecte 4 Peste + 4 Choléra (patients zéro) parmi les étudiants actifs."""
    cfg = get_config(pool)
    if cfg.plague_seeded and not reseed:
        return {"already": True}
    Infection.objects.filter(pool=pool).delete()
    users = list(AppUser.objects.filter(pool=pool, is_active=True))
    random.shuffle(users)
    picks = users[:2 * n_each]
    rows = []
    for i, u in enumerate(picks):
        disease = Infection.Disease.PESTE if i < n_each else Infection.Disease.CHOLERA
        rows.append(Infection(pool=pool, user=u, disease=disease, is_patient_zero=True))
    Infection.objects.bulk_create(rows)
    cfg.plague_seeded = True
    cfg.save(update_fields=["plague_seeded"])
    return {"peste": n_each, "cholera": min(n_each, max(0, len(picks) - n_each))}


def _spread(infected, pairs):
    """
    PUR : propage la maladie sur des couples de correction.
    infected : {user_id: disease}. pairs : [(a_id, b_id), …].
    Un couple où l'un est infecté et l'autre sain → le sain prend la maladie.
    Renvoie la liste des nouvelles infections [(user_id, disease, source_id)].
    Itère jusqu'à stabilité (une correction peut en déclencher une autre).
    """
    infected = dict(infected)
    new = []
    changed = True
    while changed:
        changed = False
        for a, b in pairs:
            ai, bi = infected.get(a), infected.get(b)
            if ai and not bi:
                infected[b] = ai
                new.append((b, ai, a))
                changed = True
            elif bi and not ai:
                infected[a] = bi
                new.append((a, bi, b))
                changed = True
    return new


def spread_plague(pool, pairs):
    """Applique la propagation sur des couples (corrector_login, corrected_login)."""
    if not Infection.objects.filter(pool=pool).exists():
        return 0
    users = {u.login: u.id for u in AppUser.objects.filter(pool=pool)}
    id_pairs = [(users[a], users[b]) for a, b in pairs
                if a in users and b in users and users[a] != users[b]]
    infected = dict(Infection.objects.filter(pool=pool).values_list("user_id", "disease"))
    new = _spread(infected, id_pairs)
    Infection.objects.bulk_create(
        [Infection(pool=pool, user_id=uid, disease=dis, source_id=src) for uid, dis, src in new],
        ignore_conflicts=True)
    return len(new)


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
    all_infected = total > 0 and infected >= total
    # meneur = groupe avec le plus d'infectés ; payout = groupe avec le plus de points
    leader = max(groups, key=lambda d: groups[d]["count"]) if infected else None
    points_leader = max(groups, key=lambda d: groups[d]["points"]) if infected else None
    return {
        "total": total, "infected": infected,
        "healthy": max(0, total - infected),
        "pct": round(100 * infected / total) if total else 0,
        "all_infected": all_infected,
        "peste": groups[Infection.Disease.PESTE],
        "cholera": groups[Infection.Disease.CHOLERA],
        "leader": leader, "points_leader": points_leader,
        "recent": [{"login": i.user.login, "disease": i.disease,
                    "source": i.source.login if i.source else None,
                    "zero": i.is_patient_zero}
                   for i in infections[:12]],
    }


def plague_endgame(pool):
    """
    Quand TOUS les étudiants sont infectés : le groupe qui a le plus de points
    fait gagner plague_payout à chacun de ses membres.
    """
    stats = plague_stats(pool)
    if not stats["all_infected"]:
        return {"ready": False}
    cfg = get_config(pool)
    winner = stats["points_leader"]
    members = (Infection.objects.filter(pool=pool, disease=winner).select_related("user"))
    for inf in members:
        _void_daily(inf.user_id, pool, "plague_payout", pool.ends_on)
        record_event(user=inf.user, pool=pool, rule_key="plague_payout",
                     occurred_at=_noon(pool.ends_on),
                     context={"points": float(cfg.plague_payout)}, source=SYSTEM)
    return {"ready": True, "winner": winner, "paid": members.count()}
